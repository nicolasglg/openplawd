#!/usr/bin/env python3
"""Read-only adapter for the official Plaud CLI.

The official CLI provides supported OAuth access to recording metadata and
24-hour audio URLs. Transcription deliberately stays in OpenPlawd/Groq so a
Plaud transcription plan is not consumed.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

CLI = os.environ.get("PLAUD_OFFICIAL_CLI", "")
CLI_SHA256 = os.environ.get("PLAUD_OFFICIAL_CLI_SHA256", "")
BUNDLE_SHA256 = os.environ.get("PLAUD_OFFICIAL_BUNDLE_SHA256", "")
TOKEN_PATH = Path(os.environ.get("PLAUD_OFFICIAL_TOKEN_FILE", "~/.plaud/tokens.json")).expanduser()
CLI_CONFIG_PATH = Path("~/.plaud/cli.yaml").expanduser()
REQUIRED_CLI_VERSION = "0.3.7"
BUNDLE_RELATIVE_PATH = Path("node_modules/@plaud-ai/cli/dist/index.js")
MAX_PAGES = 100
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ROW_RE = re.compile(
    r"^\s{2}(?P<id>\S+)\s{2,}(?P<name>.{1,36}?)\s{2,}"
    r"(?P<date>\d{4}-\d{2}-\d{2})\s{2,}(?P<duration>.+?)\s*$"
)
DETAIL_RE = re.compile(
    r"^\s*(?P<key>id|name|created_at|start_at|duration|serial_number|audio|transcript|summary):\s*(?P<value>.*?)\s*$"
)
URL_RE = re.compile(r"https://[^\s]+")
AUDIO_DOWNLOAD_HOSTS = frozenset(
    {
        "euc1-prod-plaud-bucket.s3.amazonaws.com",
        "euc1-prod-plaud-bucket.s3-accelerate.amazonaws.com",
    }
)


class PlaudOfficialError(RuntimeError):
    pass


def _clean(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\r", "")


def _env() -> dict[str, str]:
    """Return the deliberately small environment allowed to reach the CLI."""
    return {
        "HOME": os.path.expanduser("~"),
        "LANG": "C",
        "PATH": os.defpath,
        "DO_NOT_TRACK": "1",
        "PLAUD_TELEMETRY_DISABLED": "1",
        "PLAUD_NO_UPDATE_NOTIFIER": "1",
        "NO_COLOR": "1",
        "TERM": "dumb",
    }


def _bundle_path(cli_path: Path) -> Path:
    return cli_path.parent / BUNDLE_RELATIVE_PATH


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _trusted_file(path: Path, expected_sha256: str, description: str, executable: bool = False) -> None:
    if not expected_sha256:
        raise PlaudOfficialError(f"Expected SHA256 is required for official Plaud {description}")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        raise PlaudOfficialError(f"Expected SHA256 for official Plaud {description} is invalid")
    if not path.is_absolute():
        raise PlaudOfficialError(f"Official Plaud {description} must have an absolute path")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PlaudOfficialError(f"Official Plaud {description} is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise PlaudOfficialError(f"Official Plaud {description} must be a regular non-symlink file")
    if metadata.st_uid != os.geteuid():
        raise PlaudOfficialError(f"Official Plaud {description} must be owned by the current user")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise PlaudOfficialError(f"Official Plaud {description} must not be writable by group or others")
    if executable and not os.access(path, os.X_OK):
        raise PlaudOfficialError("Official Plaud CLI is not executable")
    try:
        actual_sha256 = _sha256(path)
    except OSError as exc:
        raise PlaudOfficialError(f"Could not hash official Plaud {description}") from exc
    if not hmac.compare_digest(actual_sha256, expected_sha256.lower()):
        raise PlaudOfficialError(f"Official Plaud {description} SHA256 does not match")


def _trusted_parent_directories(path: Path, description: str) -> None:
    """Reject replaceable parent directories up to the filesystem root."""
    current_uid = os.geteuid()
    for parent in path.parents:
        metadata = parent.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise PlaudOfficialError(f"Official Plaud {description} parent must be a real directory")
        if metadata.st_uid not in {0, current_uid}:
            raise PlaudOfficialError(f"Official Plaud {description} parent has an untrusted owner")
        writable = metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        protected_sticky_root = metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX
        if writable and not protected_sticky_root:
            raise PlaudOfficialError(f"Official Plaud {description} parent must not be replaceable")


def _cli_path() -> str:
    if not CLI or not os.path.isabs(CLI):
        raise PlaudOfficialError("Official Plaud CLI must be configured with an absolute path")
    cli_path = Path(CLI)
    _trusted_parent_directories(cli_path, "CLI wrapper")
    _trusted_parent_directories(_bundle_path(cli_path), "CLI bundle")
    _trusted_file(cli_path, CLI_SHA256, "CLI wrapper", executable=True)
    _trusted_file(_bundle_path(cli_path), BUNDLE_SHA256, "CLI bundle")
    return str(cli_path)


def _verified_cli_path() -> str:
    if os.path.lexists(CLI_CONFIG_PATH):
        raise PlaudOfficialError("Official Plaud CLI config file must not exist")
    cli_path = _cli_path()
    try:
        proc = subprocess.run(
            [cli_path, "version"],
            capture_output=True,
            text=True,
            env=_env(),
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PlaudOfficialError("Could not verify official Plaud CLI version") from exc
    version_lines = _clean(proc.stdout).strip().splitlines()
    if proc.returncode != 0 or not version_lines or version_lines[0] != f"plaud {REQUIRED_CLI_VERSION}":
        raise PlaudOfficialError(f"Official Plaud CLI must be version {REQUIRED_CLI_VERSION}")
    return cli_path


def available() -> bool:
    if not TOKEN_PATH.is_file():
        return False
    try:
        _verified_cli_path()
    except PlaudOfficialError:
        return False
    return True


def _run(*args: str, timeout: int = 60) -> str:
    try:
        proc = subprocess.run(
            [_verified_cli_path(), *args],
            capture_output=True,
            text=True,
            env=_env(),
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PlaudOfficialError("Official Plaud CLI invocation failed") from exc
    stdout = _clean(proc.stdout)
    if proc.returncode != 0:
        # CLI output can contain OAuth details, signed audio URLs, or server errors.
        raise PlaudOfficialError(f"Official Plaud CLI {args[0]} command failed")
    return stdout


def parse_duration_ms(value: str) -> int:
    value = value.strip().lower()
    total = 0.0
    for amount, unit in re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*(ms|h|m|s)", value):
        n = float(amount)
        total += n * {"h": 3_600_000, "m": 60_000, "s": 1_000, "ms": 1}[unit]
    return int(total)


def parse_files_output(text: str) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for line in _clean(text).splitlines():
        match = ROW_RE.match(line)
        if not match or match.group("id") == "ID":
            continue
        item = match.groupdict()
        files.append(
            {
                "id": item["id"],
                "name": item["name"].strip().rstrip("…"),
                "filename": item["name"].strip().rstrip("…"),
                "created_at": item["date"],
                "duration": parse_duration_ms(item["duration"]),
                "version_ms": 0,
                "source": "official_cli",
            }
        )
    return files


def parse_file_output(text: str) -> dict[str, str]:
    details: dict[str, str] = {}
    for line in _clean(text).splitlines():
        match = DETAIL_RE.match(line)
        if match:
            details[match.group("key")] = match.group("value")
    return details


def check_connection() -> None:
    """Verify OAuth access without paginating the full recording history."""
    # The official CLI enforces a minimum page size of 10.
    text = _run("files", "--page", "1", "--page-size", "10")
    if not parse_files_output(text) and "Files on this page: 0" not in text:
        raise PlaudOfficialError("Could not parse official Plaud files output")


def list_recordings(page_size: int = 100, max_pages: int = MAX_PAGES) -> list[dict[str, Any]]:
    if max_pages < 1:
        raise ValueError("max_pages must be positive")
    recordings: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        text = _run("files", "--page", str(page), "--page-size", str(page_size))
        files = parse_files_output(text)
        if not files:
            if "Files on this page: 0" in text:
                return recordings
            raise PlaudOfficialError("Could not parse official Plaud files output")
        recordings.extend(files)
    raise PlaudOfficialError("Official Plaud CLI pagination limit reached")


def get_file(recording_id: str) -> dict[str, Any]:
    text = _run("file", recording_id)
    parsed = parse_file_output(text)
    if parsed.get("id") != recording_id:
        raise PlaudOfficialError("Could not parse official Plaud file details")
    details: dict[str, Any] = dict(parsed)
    details["duration_ms"] = parse_duration_ms(str(details.get("duration", "")))
    return details


def get_audio_url(recording_id: str) -> str:
    text = _run("audio", recording_id)
    urls = [url.rstrip(".,)") for url in URL_RE.findall(text)]
    urls = [url for url in urls if "plaud.ai" not in url or "developer/api" not in url]
    if not urls:
        raise PlaudOfficialError("Official Plaud CLI returned no audio URL")
    validate_audio_url(urls[0])
    return urls[0]


def validate_audio_url(url: str) -> None:
    """Allow downloads only from the verified Plaud EU storage origin."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in AUDIO_DOWNLOAD_HOSTS:
        raise PlaudOfficialError("Plaud returned an untrusted audio URL")


def enrich_recording(recording: dict[str, Any]) -> dict[str, Any]:
    details = get_file(str(recording["id"]))
    enriched = dict(recording)
    enriched.update(
        {
            "name": details.get("name") or recording.get("name") or "",
            "filename": details.get("name") or recording.get("filename") or "",
            "created_at": details.get("created_at") or recording.get("created_at"),
            "start_at": details.get("start_at"),
            "duration": details.get("duration_ms") or recording.get("duration", 0),
            "source": "official_cli",
        }
    )
    return enriched
