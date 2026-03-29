#!/usr/bin/env python3
"""
OpenPlawd — Plaud polling + transcription for OpenClaw.

- < 90 min  → Groq Whisper (free tier)
- >= 90 min → OpenAI Whisper (paid, no hourly quota)
- Chunk-based transcription with resume on interruption
- Structured JSON output for OpenClaw agent processing
"""

import glob
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# --- Configuration via environment variables ---
BASE_DIR = os.environ.get("OPENPLAWD_BASE_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROCESSED_FILE = os.path.join(BASE_DIR, "data", "processed.json")
TMP_DIR = os.path.join(BASE_DIR, "tmp")

PLAUD_API_BASE = "https://api.plaud.ai"
GROQ_API_BASE = "https://api.groq.com/openai/v1/audio/transcriptions"
OPENAI_API_BASE = "https://api.openai.com/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3-turbo"
OPENAI_MODEL = "whisper-1"

MAX_FILE_SIZE_MB = 24
MAX_FAILURES = 3
MAX_RETRIES = 3
LARGE_DURATION_THRESHOLD = 90 * 60  # 90 min in seconds → switch to OpenAI
PARALLEL_WORKERS_OPENAI = 4
PARALLEL_WORKERS_GROQ = 1

# Statuses in processed.json
STATUS_NOTIFIED = "notified"
STATUS_TRANSCRIBING = "transcribing"
STATUS_TRANSCRIBED = "transcribed"
STATUS_DONE = "done"


def log(msg):
    print(f"[openplawd] {msg}", file=sys.stderr)


def load_env_key(name, required=True):
    """Load a key from environment variables."""
    val = os.environ.get(name)
    if not val and required:
        log(f"ERROR: {name} environment variable is required")
        sys.exit(1)
    return val


def plaud_headers(token):
    return {
        "Authorization": f"Bearer {token}" if not token.lower().startswith("bearer") else token,
        "Content-Type": "application/json",
    }


def retry_get(url, hdrs):
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers=hdrs, timeout=30)
            data = resp.json()
            if data.get("status") == 0:
                return data
        except (requests.RequestException, json.JSONDecodeError) as e:
            log(f"Attempt {attempt + 1}/{MAX_RETRIES} failed: {e}")
        time.sleep(3 * (attempt + 1))
    return None


def check_connection(token):
    data = retry_get(f"{PLAUD_API_BASE}/device/list", plaud_headers(token))
    if data is None:
        log("ERROR: Plaud API unreachable — token may be invalid or expired")
        sys.exit(1)
    log("Plaud API connection OK")


def list_recordings(token):
    data = retry_get(
        f"{PLAUD_API_BASE}/file/simple/web?is_trash=0&sort_by=edit_time&is_desc=true",
        plaud_headers(token),
    )
    if data is None:
        log("ERROR: Could not list recordings")
        sys.exit(1)
    return data.get("data_file_list", [])


def load_processed():
    if not os.path.exists(PROCESSED_FILE):
        return {}
    with open(PROCESSED_FILE) as f:
        return json.load(f)


def save_processed(processed):
    os.makedirs(os.path.dirname(PROCESSED_FILE), exist_ok=True)
    with open(PROCESSED_FILE, "w") as f:
        json.dump(processed, f, indent=2)


def should_process(rec, processed):
    """Determine if this recording needs processing this run."""
    rec_id = rec["id"]
    version = rec.get("version_ms", 0)
    entry = processed.get(rec_id, {})

    if entry.get("processed_at"):
        if entry.get("version_ms", 0) < version:
            log(f"New version of {rec_id}, re-processing")
            return True
        return False

    if entry.get("status") == STATUS_TRANSCRIBED:
        log(f"SKIP {rec_id}: transcription ready, waiting for CR generation")
        return False

    if entry.get("fail_count", 0) >= MAX_FAILURES:
        log(f"SKIP {rec_id}: {entry.get('fail_count')} consecutive failures")
        return False

    return True


def is_first_detection(rec_id, processed):
    """True if this is the first time we see this recording."""
    entry = processed.get(rec_id, {})
    return not entry.get("status") and not entry.get("processed_at")


def download_recording(rec, token):
    file_id = rec["id"]
    data = retry_get(
        f"{PLAUD_API_BASE}/file/temp-url/{file_id}?is_opus=1",
        plaud_headers(token),
    )
    if data is None or not data.get("temp_url"):
        raise RuntimeError(f"Could not get download URL for {file_id}")

    url = data["temp_url"]
    ext = "opus" if "opus" in url.lower() else "mp3"
    out_path = os.path.join(TMP_DIR, f"{file_id}.{ext}")

    resp = requests.get(url, timeout=300)
    resp.raise_for_status()
    with open(out_path, "wb") as f:
        f.write(resp.content)

    size_mb = os.path.getsize(out_path) / 1024 / 1024
    log(f"Downloaded {file_id}.{ext} ({size_mb:.1f} MB)")
    return out_path


def split_audio(audio_path):
    """Split audio file into chunks < MAX_FILE_SIZE_MB using ffmpeg."""
    size_mb = os.path.getsize(audio_path) / 1024 / 1024
    if size_mb <= MAX_FILE_SIZE_MB:
        return [audio_path]

    n_chunks = int(size_mb / MAX_FILE_SIZE_MB) + 1
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", audio_path],
        capture_output=True, text=True
    )
    total_duration = float(result.stdout.strip())
    chunk_duration = total_duration / n_chunks

    base = os.path.splitext(audio_path)[0]
    ext = os.path.splitext(audio_path)[1]
    chunk_pattern = f"{base}_chunk%03d{ext}"

    subprocess.run(
        ["ffmpeg", "-y", "-i", audio_path, "-f", "segment",
         "-segment_time", str(int(chunk_duration)),
         "-c", "copy", chunk_pattern],
        capture_output=True, text=True
    )

    chunks = sorted(glob.glob(f"{base}_chunk*{ext}"))
    if not chunks:
        log("ERROR: ffmpeg produced no chunks")
        return [audio_path]

    log(f"Audio split into {len(chunks)} chunks ({size_mb:.1f} MB total)")
    return chunks


def transcribe_chunk_groq(audio_path, groq_key):
    with open(audio_path, "rb") as f:
        resp = requests.post(
            GROQ_API_BASE,
            headers={"Authorization": f"Bearer {groq_key}"},
            files={"file": (os.path.basename(audio_path), f)},
            data={"model": GROQ_MODEL, "language": "fr", "response_format": "text"},
            timeout=300,
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Groq API error {resp.status_code}: {resp.text[:200]}")
    return resp.text.strip()


def transcribe_chunk_openai(audio_path, openai_key):
    with open(audio_path, "rb") as f:
        resp = requests.post(
            OPENAI_API_BASE,
            headers={"Authorization": f"Bearer {openai_key}"},
            files={"file": (os.path.basename(audio_path), f)},
            data={"model": OPENAI_MODEL, "language": "fr", "response_format": "text"},
            timeout=300,
        )
    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI Whisper error {resp.status_code}: {resp.text[:200]}")
    return resp.text.strip()


def _transcribe_chunk_worker(args):
    """Worker for ThreadPoolExecutor: transcribe a chunk and return (index, text)."""
    i, chunk, use_openai, groq_key, openai_key, n_total = args
    log(f"  Chunk {i + 1}/{n_total}...")
    if use_openai:
        text = transcribe_chunk_openai(chunk, openai_key)
    else:
        text = transcribe_chunk_groq(chunk, groq_key)
    return i, text


def transcribe_with_resume(audio_path, duration_secs, rec_id, processed, groq_key, openai_key):
    """
    Transcribe with resume on interruption and parallelism for OpenAI.
    - OpenAI (>= 90 min): parallel chunks (PARALLEL_WORKERS_OPENAI)
    - Groq (< 90 min): sequential with resume (hourly quota)
    Returns (txt_path, word_count) if complete, (None, None) if partial.
    """
    use_openai = duration_secs >= LARGE_DURATION_THRESHOLD
    provider = "OpenAI Whisper" if use_openai else "Groq"
    log(f"Transcribing {os.path.basename(audio_path)} ({duration_secs // 60}min) via {provider}...")

    if use_openai and not openai_key:
        raise RuntimeError("OpenAI key required for recordings >= 90 min")

    chunks = split_audio(audio_path)
    n_total = len(chunks)
    entry = processed.get(rec_id, {})

    try:
        if use_openai:
            workers = min(PARALLEL_WORKERS_OPENAI, n_total)
            log(f"  Launching {workers} parallel workers on {n_total} chunks...")

            results = {}
            failed = False

            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(
                        _transcribe_chunk_worker,
                        (i, chunk, True, groq_key, openai_key, n_total)
                    ): i
                    for i, chunk in enumerate(chunks)
                }
                for future in as_completed(futures):
                    try:
                        idx, text = future.result()
                        results[idx] = text
                        entry["status"] = STATUS_TRANSCRIBING
                        entry["chunks_done"] = len(results)
                        entry["chunks_total"] = n_total
                        entry["last_attempt"] = time.strftime("%Y-%m-%d %H:%M:%S")
                        processed[rec_id] = entry
                        save_processed(processed)
                    except Exception as e:
                        log(f"Chunk error: {e}")
                        failed = True
                        entry["fail_count"] = entry.get("fail_count", 0) + 1
                        entry["last_error"] = str(e)[:200]
                        entry["last_attempt"] = time.strftime("%Y-%m-%d %H:%M:%S")
                        processed[rec_id] = entry
                        save_processed(processed)

            if failed or len(results) < n_total:
                log(f"Incomplete transcription: {len(results)}/{n_total} chunks")
                return None, None

            text_parts = [results[i] for i in sorted(results.keys())]

        else:
            chunks_done = entry.get("chunks_done", 0)
            text_parts = entry.get("partial_text_parts", [])

            if chunks_done > 0:
                log(f"Resuming from chunk {chunks_done + 1}/{n_total}")

            for i, chunk in enumerate(chunks):
                if i < chunks_done:
                    if chunk != audio_path:
                        try:
                            os.remove(chunk)
                        except OSError:
                            pass
                    continue

                try:
                    _, text = _transcribe_chunk_worker(
                        (i, chunk, False, groq_key, openai_key, n_total)
                    )
                    text_parts.append(text)
                    chunks_done = i + 1

                    entry["status"] = STATUS_TRANSCRIBING
                    entry["chunks_done"] = chunks_done
                    entry["chunks_total"] = n_total
                    entry["partial_text_parts"] = text_parts
                    entry["last_attempt"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    processed[rec_id] = entry
                    save_processed(processed)

                except Exception as e:
                    log(f"Chunk {i + 1}/{n_total} error: {e}")
                    entry["fail_count"] = entry.get("fail_count", 0) + 1
                    entry["last_error"] = str(e)[:200]
                    entry["last_attempt"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    processed[rec_id] = entry
                    save_processed(processed)
                    return None, None

                finally:
                    if chunk != audio_path:
                        try:
                            os.remove(chunk)
                        except OSError:
                            pass

    finally:
        for chunk in chunks:
            if chunk != audio_path:
                try:
                    os.remove(chunk)
                except OSError:
                    pass

    full_text = " ".join(text_parts).strip()
    txt_path = os.path.join(TMP_DIR, f"{rec_id}.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(full_text)

    word_count = len(full_text.split())
    log(f"Transcription complete: {word_count} words -> {txt_path}")
    return txt_path, word_count


def rename_recording(rec_id, new_name, token):
    time.sleep(1)
    resp = requests.patch(
        f"{PLAUD_API_BASE}/file/{rec_id}",
        headers=plaud_headers(token),
        json={"filename": new_name},
        timeout=30,
    )
    if resp.status_code == 200:
        log(f"Renamed {rec_id} -> {new_name}")
        return True
    else:
        log(f"Rename error {rec_id}: {resp.status_code}")
        return False


def trash_recording(rec_id, token):
    time.sleep(1)
    resp = requests.post(
        f"{PLAUD_API_BASE}/file/trash/",
        headers=plaud_headers(token),
        json=[rec_id],
        timeout=30,
    )
    if resp.status_code == 200:
        log(f"Trashed: {rec_id}")
        return True
    else:
        log(f"Trash error {rec_id}: {resp.status_code}")
        return False


def main():
    os.makedirs(TMP_DIR, exist_ok=True)

    plaud_token = load_env_key("PLAUD_TOKEN")
    groq_key = load_env_key("GROQ_API_KEY")
    openai_key = load_env_key("OPENAI_API_KEY", required=False)

    check_connection(plaud_token)

    recordings = list_recordings(plaud_token)
    processed = load_processed()

    to_process = [r for r in recordings if should_process(r, processed)]

    if not to_process:
        log(f"Nothing to process ({len(recordings)} recordings)")
        print(json.dumps({
            "new_detected": [],
            "transcriptions_complete": [],
            "transcriptions_in_progress": [],
        }))
        return

    new_detected = []
    transcriptions_complete = []
    transcriptions_in_progress = []

    for rec in to_process:
        rec_id = rec["id"]
        filename = rec.get("filename", "")
        duration = rec.get("duration", 0) // 1000

        first_time = is_first_detection(rec_id, processed)

        if first_time:
            entry = processed.get(rec_id, {})
            entry["status"] = STATUS_NOTIFIED
            entry["notified_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            processed[rec_id] = entry
            save_processed(processed)
            new_detected.append({
                "rec_id": rec_id,
                "filename": filename,
                "duration": duration,
                "version_ms": rec.get("version_ms", 0),
            })
            log(f"New: {filename} ({duration // 60}min) via {'OpenAI' if duration >= LARGE_DURATION_THRESHOLD else 'Groq'}")

        audio_path = None
        try:
            audio_path = download_recording(rec, plaud_token)
            txt_path, word_count = transcribe_with_resume(
                audio_path, duration, rec_id, processed, groq_key, openai_key
            )

            if txt_path is not None:
                entry = processed.get(rec_id, {})
                entry["status"] = STATUS_TRANSCRIBED
                entry["transcript_path"] = txt_path
                entry["word_count"] = word_count
                entry["version_ms"] = rec.get("version_ms", 0)
                entry.pop("partial_text_parts", None)
                entry.pop("chunks_done", None)
                entry.pop("chunks_total", None)
                processed[rec_id] = entry
                save_processed(processed)

                transcriptions_complete.append({
                    "rec_id": rec_id,
                    "filename": filename,
                    "duration": duration,
                    "transcript_path": txt_path,
                    "word_count": word_count,
                    "version_ms": rec.get("version_ms", 0),
                    "is_new": first_time,
                })
            else:
                entry = processed.get(rec_id, {})
                transcriptions_in_progress.append({
                    "rec_id": rec_id,
                    "filename": filename,
                    "duration": duration,
                    "chunks_done": entry.get("chunks_done", 0),
                    "chunks_total": entry.get("chunks_total", "?"),
                    "is_new": first_time,
                })

        except Exception as e:
            log(f"ERROR on {rec_id}: {e}")
            entry = processed.get(rec_id, {})
            entry["fail_count"] = entry.get("fail_count", 0) + 1
            entry["last_error"] = str(e)[:200]
            entry["last_attempt"] = time.strftime("%Y-%m-%d %H:%M:%S")
            processed[rec_id] = entry
            save_processed(processed)

        finally:
            if audio_path:
                try:
                    os.remove(audio_path)
                except OSError:
                    pass

    output = {
        "new_detected": new_detected,
        "transcriptions_complete": transcriptions_complete,
        "transcriptions_in_progress": transcriptions_in_progress,
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        plaud_token = load_env_key("PLAUD_TOKEN")
        if cmd == "rename" and len(sys.argv) >= 4:
            rec_id = sys.argv[2]
            new_name = " ".join(sys.argv[3:])
            ok = rename_recording(rec_id, new_name, plaud_token)
            print(json.dumps({"success": ok, "action": "rename", "rec_id": rec_id}))
        elif cmd == "trash" and len(sys.argv) >= 3:
            rec_id = sys.argv[2]
            ok = trash_recording(rec_id, plaud_token)
            print(json.dumps({"success": ok, "action": "trash", "rec_id": rec_id}))
        else:
            print(f"Usage: {sys.argv[0]} [rename <id> <name> | trash <id>]", file=sys.stderr)
            sys.exit(1)
    else:
        main()
