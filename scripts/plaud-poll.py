#!/usr/bin/env python3
"""
OpenPlawd — Plaud polling + transcription Groq Whisper.

- Transcription : Groq Whisper API (large-v3, gratuit)
- Chunking automatique pour fichiers > 24 MB
- Resumable : chunks sauvegardes individuellement, reprise au prochain run
- Un seul recording par run (evite surcharge contexte agent)
- Pause entre chunks pour eviter le rate limit
"""

import json
import os
import subprocess
import sys
import time

import requests

# --- Configuration ---
BASE_DIR = os.environ.get("OPENPLAWD_BASE_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROCESSED_FILE = os.path.join(BASE_DIR, "data", "processed.json")
TMP_DIR = os.path.join(BASE_DIR, "tmp")

PLAUD_API_BASE = "https://api.plaud.ai"
GROQ_API_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3"
WHISPER_LANGUAGE = "fr"
CHUNK_MAX_MB = 24
CHUNK_DURATION_MIN = 15
CHUNK_OVERLAP_SEC = 5
CHUNK_PAUSE_SEC = 8  # pause between chunks to avoid rate limit

MAX_RETRIES = 3
MAX_FAILURES = 3

STATUS_NOTIFIED = "notified"
STATUS_TRANSCRIBING = "transcribing"
STATUS_TRANSCRIBED = "transcribed"
STATUS_DONE = "done"

TOKENS_ENV = os.path.expanduser("~/.claude/env/tokens.env")


def log(msg):
    print(f"[openplawd] {msg}", file=sys.stderr)


def load_env_key(name, required=True):
    """Load from env var, fallback to ~/.claude/env/tokens.env."""
    val = os.environ.get(name)
    if not val and os.path.exists(TOKENS_ENV):
        with open(TOKENS_ENV) as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{name}="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not val and required:
        log(f"ERROR: {name} not found in env or {TOKENS_ENV}")
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

    if entry.get("status") == STATUS_DONE:
        return False

    if entry.get("fail_count", 0) >= MAX_FAILURES:
        log(f"SKIP {rec_id}: {entry.get('fail_count')} consecutive failures")
        return False

    return True


def is_first_detection(rec_id, processed):
    entry = processed.get(rec_id, {})
    return not entry.get("status") and not entry.get("processed_at")


def download_recording(rec, token):
    file_id = rec["id"]
    out_path = os.path.join(TMP_DIR, f"{file_id}.mp3")

    # Skip download if file already exists (resume)
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        size_mb = os.path.getsize(out_path) / 1024 / 1024
        log(f"Audio already downloaded: {file_id}.mp3 ({size_mb:.1f} MB)")
        return out_path

    data = retry_get(
        f"{PLAUD_API_BASE}/file/temp-url/{file_id}?is_opus=1",
        plaud_headers(token),
    )
    if data is None or not data.get("temp_url"):
        raise RuntimeError(f"Could not get download URL for {file_id}")

    url = data["temp_url"]
    resp = requests.get(url, timeout=300)
    resp.raise_for_status()
    with open(out_path, "wb") as f:
        f.write(resp.content)

    size_mb = os.path.getsize(out_path) / 1024 / 1024
    log(f"Downloaded {file_id}.mp3 ({size_mb:.1f} MB)")
    return out_path


def get_audio_duration(audio_path):
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", audio_path],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0 and result.stdout.strip():
        return float(result.stdout.strip())
    return None


def chunk_audio(audio_path, rec_id):
    """Split audio into chunks if > CHUNK_MAX_MB. Returns list of chunk paths."""
    size_mb = os.path.getsize(audio_path) / 1024 / 1024

    if size_mb <= CHUNK_MAX_MB:
        return [audio_path], False

    duration = get_audio_duration(audio_path)
    if duration is None:
        raise RuntimeError(f"Cannot determine duration of {audio_path}")

    chunk_seconds = CHUNK_DURATION_MIN * 60
    chunks = []
    start = 0
    idx = 0

    while start < duration:
        chunk_path = os.path.join(TMP_DIR, f"{rec_id}_chunk{idx:03d}.mp3")

        # Skip if chunk already exists (resume)
        if os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 0:
            chunks.append(chunk_path)
            start += chunk_seconds
            idx += 1
            continue

        end = min(start + chunk_seconds + CHUNK_OVERLAP_SEC, duration)
        actual_duration = end - start

        subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path,
             "-ss", str(start), "-t", str(actual_duration),
             "-ar", "16000", "-ac", "1", "-b:a", "64k",
             chunk_path],
            capture_output=True, timeout=120,
        )

        if os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 0:
            chunk_mb = os.path.getsize(chunk_path) / 1024 / 1024
            log(f"  Chunk {idx}: {start/60:.0f}-{end/60:.0f}min ({chunk_mb:.1f} MB)")
            chunks.append(chunk_path)

        start += chunk_seconds
        idx += 1

    log(f"Split into {len(chunks)} chunks")
    return chunks, True


def transcribe_groq(audio_path, groq_key):
    """Transcribe a single file via Groq. Returns text or None on rate limit."""
    for attempt in range(MAX_RETRIES):
        try:
            with open(audio_path, "rb") as f:
                resp = requests.post(
                    GROQ_API_URL,
                    headers={"Authorization": f"Bearer {groq_key}"},
                    files={"file": (os.path.basename(audio_path), f)},
                    data={"model": GROQ_MODEL, "language": WHISPER_LANGUAGE},
                    timeout=300,
                )

            if resp.status_code == 200:
                return resp.json().get("text", "")

            if resp.status_code == 429:
                # Return None to signal rate limit — caller decides what to do
                return None

            resp.raise_for_status()

        except requests.Timeout:
            log(f"  Timeout on attempt {attempt + 1}")
            time.sleep(10)
        except requests.RequestException as e:
            log(f"  Request error on attempt {attempt + 1}: {e}")
            time.sleep(5 * (attempt + 1))

    raise RuntimeError(f"Groq transcription failed after {MAX_RETRIES} attempts")


def transcribe(audio_path, rec_id, processed):
    """Transcribe audio via Groq with chunking and resume support."""
    duration_min = processed.get(rec_id, {}).get("duration_min", "?")
    groq_key = load_env_key("GROQ_API_KEY")

    log(f"Transcribing {os.path.basename(audio_path)} ({duration_min}min) via Groq Whisper...")

    entry = processed.get(rec_id, {})
    entry["status"] = STATUS_TRANSCRIBING
    entry["last_attempt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    processed[rec_id] = entry
    save_processed(processed)

    # Chunk if needed
    chunks, is_chunked = chunk_audio(audio_path, rec_id)

    # Transcribe each chunk (with resume support)
    all_text = []
    rate_limited = False

    for i, chunk_path in enumerate(chunks):
        # Check if this chunk was already transcribed
        chunk_txt_path = chunk_path.rsplit(".", 1)[0] + ".txt"
        if os.path.exists(chunk_txt_path):
            with open(chunk_txt_path, encoding="utf-8") as f:
                all_text.append(f.read())
            log(f"  Chunk {i + 1}/{len(chunks)}: already done (resume)")
            continue

        if is_chunked:
            log(f"  Transcribing chunk {i + 1}/{len(chunks)}...")

        # Pause between chunks to avoid rate limit
        if i > 0:
            time.sleep(CHUNK_PAUSE_SEC)

        text = transcribe_groq(chunk_path, groq_key)

        if text is None:
            # Rate limited — save progress and stop
            log(f"  Rate limited at chunk {i + 1}/{len(chunks)}. Progress saved, retry later.")
            entry["chunks_done"] = i
            entry["chunks_total"] = len(chunks)
            processed[rec_id] = entry
            save_processed(processed)
            rate_limited = True
            break

        # Save chunk transcript immediately
        with open(chunk_txt_path, "w", encoding="utf-8") as f:
            f.write(text)
        all_text.append(text)

    if rate_limited:
        return None, 0

    # All chunks done — concatenate
    full_text = "\n".join(all_text)
    txt_path = os.path.join(TMP_DIR, f"{rec_id}.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(full_text)

    # Cleanup chunk files
    if is_chunked:
        for chunk_path in chunks:
            for ext in [".mp3", ".txt"]:
                p = chunk_path.rsplit(".", 1)[0] + ext
                try:
                    os.remove(p)
                except OSError:
                    pass

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
    check_connection(plaud_token)

    recordings = list_recordings(plaud_token)
    processed = load_processed()

    # Sort by date (oldest first) to process in order
    to_process = sorted(
        [r for r in recordings if should_process(r, processed)],
        key=lambda r: r.get("duration", 0),
    )

    if not to_process:
        log(f"Nothing to process ({len(recordings)} recordings)")
        print(json.dumps({
            "status": "idle",
            "total_recordings": len(recordings),
            "pending": 0,
        }))
        return

    # Process ONE recording only
    rec = to_process[0]
    rec_id = rec["id"]
    filename = rec.get("filename", "")
    duration = rec.get("duration", 0) // 1000

    first_time = is_first_detection(rec_id, processed)

    if first_time:
        entry = processed.get(rec_id, {})
        entry["status"] = STATUS_NOTIFIED
        entry["notified_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        entry["duration_min"] = duration // 60
        processed[rec_id] = entry
        save_processed(processed)
        log(f"New: {filename} ({duration // 60}min)")

    audio_path = None
    try:
        audio_path = download_recording(rec, plaud_token)
        txt_path, word_count = transcribe(audio_path, rec_id, processed)

        if txt_path is None:
            # Rate limited — partial progress saved
            print(json.dumps({
                "status": "rate_limited",
                "rec_id": rec_id,
                "filename": filename,
                "duration": duration,
                "chunks_done": processed.get(rec_id, {}).get("chunks_done", 0),
                "chunks_total": processed.get(rec_id, {}).get("chunks_total", 0),
                "pending": len(to_process),
                "message": "Rate limited. Run again later to resume.",
            }))
            return

        entry = processed.get(rec_id, {})
        entry["status"] = STATUS_TRANSCRIBED
        entry["transcript_path"] = txt_path
        entry["word_count"] = word_count
        entry["version_ms"] = rec.get("version_ms", 0)
        processed[rec_id] = entry
        save_processed(processed)

        print(json.dumps({
            "status": "transcribed",
            "rec_id": rec_id,
            "filename": filename,
            "duration": duration,
            "transcript_path": txt_path,
            "word_count": word_count,
            "is_new": first_time,
            "pending": len(to_process) - 1,
        }))

    except Exception as e:
        log(f"ERROR on {rec_id}: {e}")
        entry = processed.get(rec_id, {})
        entry["fail_count"] = entry.get("fail_count", 0) + 1
        entry["last_error"] = str(e)[:200]
        entry["last_attempt"] = time.strftime("%Y-%m-%d %H:%M:%S")
        processed[rec_id] = entry
        save_processed(processed)

        print(json.dumps({
            "status": "error",
            "rec_id": rec_id,
            "filename": filename,
            "error": str(e)[:200],
            "pending": len(to_process) - 1,
        }))

    finally:
        # Don't delete audio if transcription incomplete (resume)
        if audio_path and processed.get(rec_id, {}).get("status") == STATUS_TRANSCRIBED:
            try:
                os.remove(audio_path)
            except OSError:
                pass


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
