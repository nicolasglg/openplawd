import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plaud_official = load_module("plaud_official_test", SCRIPTS / "plaud_official.py")


def files_page(rows: int, page: int) -> str:
    if not rows:
        return f"Files on this page: 0\n\nPage {page}\n"
    lines = [f"Files on this page: {rows}", "", "  ID  NAME  DATE  DURATION"]
    for index in range(rows):
        lines.append(f"  id-{page}-{index}  Recording {index}  2026-08-05  1s")
    return "\n".join(lines)


class OfficialParserTests(unittest.TestCase):
    def test_duration_parses_milliseconds_before_minutes(self):
        self.assertEqual(0, plaud_official.parse_duration_ms("-"))
        self.assertEqual(90_500, plaud_official.parse_duration_ms("1m 30.5s"))
        self.assertEqual(1_002, plaud_official.parse_duration_ms("1s 2ms"))

    def test_list_recordings_paginates_until_empty_page(self):
        with mock.patch.object(
            plaud_official,
            "_run",
            side_effect=[files_page(1, 1), files_page(1, 2), files_page(0, 3)],
        ) as run:
            recordings = plaud_official.list_recordings(page_size=50, max_pages=5)
        self.assertEqual(["id-1-0", "id-2-0"], [recording["id"] for recording in recordings])
        self.assertEqual(
            [
                mock.call("files", "--page", "1", "--page-size", "50"),
                mock.call("files", "--page", "2", "--page-size", "50"),
                mock.call("files", "--page", "3", "--page-size", "50"),
            ],
            run.call_args_list,
        )

    def test_list_recordings_has_defensive_pagination_limit(self):
        with mock.patch.object(plaud_official, "_run", return_value=files_page(1, 1)):
            with self.assertRaisesRegex(plaud_official.PlaudOfficialError, "pagination limit"):
                plaud_official.list_recordings(max_pages=2)

    def test_check_connection_uses_cli_minimum_page_size(self):
        with mock.patch.object(plaud_official, "_run", return_value=files_page(1, 1)) as run:
            plaud_official.check_connection()
        run.assert_called_once_with("files", "--page", "1", "--page-size", "10")

    def test_audio_url_requires_verified_eu_storage_origin(self):
        plaud_official.validate_audio_url(
            "https://euc1-prod-plaud-bucket.s3.amazonaws.com/audio.mp3?signature=redacted"
        )
        plaud_official.validate_audio_url(
            "https://euc1-prod-plaud-bucket.s3-accelerate.amazonaws.com/audio.mp3?signature=redacted"
        )
        with self.assertRaisesRegex(plaud_official.PlaudOfficialError, "untrusted audio URL"):
            plaud_official.validate_audio_url("https://storage.example/audio.mp3")
        with self.assertRaisesRegex(plaud_official.PlaudOfficialError, "untrusted audio URL"):
            plaud_official.validate_audio_url(
                "https://evil.euc1-prod-plaud-bucket.s3-accelerate.amazonaws.com/audio.mp3"
            )


class OfficialCliSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.wrapper = root / "run-cli.sh"
        self.wrapper.write_text("#!/bin/sh\nexit 0\n")
        self.bundle = root / plaud_official.BUNDLE_RELATIVE_PATH
        self.bundle.parent.mkdir(parents=True)
        self.bundle.write_text("audited CLI bundle\n")
        os.chmod(self.wrapper, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        os.chmod(self.bundle, stat.S_IRUSR | stat.S_IWUSR)
        self.config = root / "cli.yaml"

    def trusted_patches(self):
        return (
            mock.patch.object(plaud_official, "CLI", str(self.wrapper)),
            mock.patch.object(plaud_official, "CLI_SHA256", plaud_official._sha256(self.wrapper)),
            mock.patch.object(plaud_official, "BUNDLE_SHA256", plaud_official._sha256(self.bundle)),
            mock.patch.object(plaud_official, "CLI_CONFIG_PATH", self.config),
        )

    def test_cli_requires_absolute_path(self):
        with mock.patch.object(plaud_official, "CLI", "plaud"):
            with self.assertRaisesRegex(plaud_official.PlaudOfficialError, "absolute path"):
                plaud_official._run("files")

    def test_checksums_are_mandatory_before_version_execution(self):
        with (
            mock.patch.object(plaud_official, "CLI", str(self.wrapper)),
            mock.patch.object(plaud_official, "CLI_SHA256", ""),
            mock.patch.object(plaud_official, "BUNDLE_SHA256", ""),
            mock.patch.object(plaud_official, "CLI_CONFIG_PATH", self.config),
            mock.patch.object(subprocess, "run") as run,
        ):
            with self.assertRaisesRegex(plaud_official.PlaudOfficialError, "Expected SHA256"):
                plaud_official._verified_cli_path()
        run.assert_not_called()

    def test_bundle_checksum_is_verified_before_version_execution(self):
        with (
            mock.patch.object(plaud_official, "CLI", str(self.wrapper)),
            mock.patch.object(plaud_official, "CLI_SHA256", plaud_official._sha256(self.wrapper)),
            mock.patch.object(plaud_official, "BUNDLE_SHA256", "0" * 64),
            mock.patch.object(plaud_official, "CLI_CONFIG_PATH", self.config),
            mock.patch.object(subprocess, "run") as run,
        ):
            with self.assertRaisesRegex(plaud_official.PlaudOfficialError, "bundle SHA256 does not match"):
                plaud_official._verified_cli_path()
        run.assert_not_called()

    def test_cli_rejects_group_writable_wrapper(self):
        os.chmod(self.wrapper, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IWGRP)
        cli, cli_hash, bundle_hash, config = self.trusted_patches()
        with cli, cli_hash, bundle_hash, config:
            with self.assertRaisesRegex(plaud_official.PlaudOfficialError, "not be writable by group"):
                plaud_official._verified_cli_path()

    def test_cli_rejects_replaceable_parent_directory(self):
        root = self.wrapper.parent
        original_mode = stat.S_IMODE(root.stat().st_mode)
        os.chmod(root, original_mode | stat.S_IWGRP)
        try:
            cli, cli_hash, bundle_hash, config = self.trusted_patches()
            with cli, cli_hash, bundle_hash, config:
                with self.assertRaisesRegex(plaud_official.PlaudOfficialError, "parent must not be replaceable"):
                    plaud_official._verified_cli_path()
        finally:
            os.chmod(root, original_mode)

    def test_cli_rejects_wrapper_not_owned_by_current_user(self):
        cli, cli_hash, bundle_hash, config = self.trusted_patches()
        with (
            cli,
            cli_hash,
            bundle_hash,
            config,
            mock.patch.object(plaud_official, "_trusted_parent_directories"),
            mock.patch.object(plaud_official.os, "geteuid", return_value=os.geteuid() + 1),
        ):
            with self.assertRaisesRegex(plaud_official.PlaudOfficialError, "owned by the current user"):
                plaud_official._verified_cli_path()

    def test_cli_rejects_symlinked_wrapper(self):
        target = self.wrapper.with_name("real-wrapper.sh")
        self.wrapper.rename(target)
        self.wrapper.symlink_to(target.name)
        cli, cli_hash, bundle_hash, config = self.trusted_patches()
        with cli, cli_hash, bundle_hash, config:
            with self.assertRaisesRegex(plaud_official.PlaudOfficialError, "regular non-symlink"):
                plaud_official._verified_cli_path()

    def test_cli_rejects_symlinked_bundle(self):
        target = self.bundle.with_name("real-index.js")
        self.bundle.rename(target)
        self.bundle.symlink_to(target.name)
        cli, cli_hash, bundle_hash, config = self.trusted_patches()
        with cli, cli_hash, bundle_hash, config:
            with self.assertRaisesRegex(plaud_official.PlaudOfficialError, "regular non-symlink"):
                plaud_official._verified_cli_path()

    def test_cli_refuses_persistent_config_before_execution(self):
        self.config.write_text("api_base: https://attacker.invalid\n")
        cli, cli_hash, bundle_hash, config = self.trusted_patches()
        with cli, cli_hash, bundle_hash, config, mock.patch.object(subprocess, "run") as run:
            with self.assertRaisesRegex(plaud_official.PlaudOfficialError, "config file must not exist"):
                plaud_official._verified_cli_path()
        run.assert_not_called()

    def test_cli_requires_exact_pinned_version(self):
        result = subprocess.CompletedProcess([str(self.wrapper), "version"], 0, "plaud 0.3.8\n", "")
        cli, cli_hash, bundle_hash, config = self.trusted_patches()
        with cli, cli_hash, bundle_hash, config, mock.patch.object(subprocess, "run", return_value=result):
            with self.assertRaisesRegex(plaud_official.PlaudOfficialError, "0.3.7"):
                plaud_official._run("files")

    def test_cli_environment_is_minimal_and_drops_secret_and_legacy_variables(self):
        version = subprocess.CompletedProcess([str(self.wrapper), "version"], 0, "plaud 0.3.7\ncommit abc\n", "")
        command = subprocess.CompletedProcess([str(self.wrapper), "files"], 0, "ok", "")
        cli, cli_hash, bundle_hash, config = self.trusted_patches()
        with (
            cli,
            cli_hash,
            bundle_hash,
            config,
            mock.patch.dict(
                os.environ,
                {"PLAUD_API_BASE": "https://attacker.invalid", "PLAUD_TOKEN": "secret", "GROQ_API_KEY": "secret"},
                clear=False,
            ),
            mock.patch.object(subprocess, "run", side_effect=[version, command]) as run,
        ):
            self.assertEqual("ok", plaud_official._run("files"))
        for call in run.call_args_list:
            env = call.kwargs["env"]
            self.assertNotIn("PLAUD_API_BASE", env)
            self.assertNotIn("PLAUD_TOKEN", env)
            self.assertNotIn("GROQ_API_KEY", env)
            self.assertEqual("1", env["PLAUD_NO_UPDATE_NOTIFIER"])
            self.assertEqual(set(env), {"HOME", "LANG", "PATH", "DO_NOT_TRACK", "PLAUD_TELEMETRY_DISABLED", "PLAUD_NO_UPDATE_NOTIFIER", "NO_COLOR", "TERM"})

    def test_cli_error_does_not_expose_stderr_or_signed_audio_url(self):
        audio_url = "https://euc1-prod-plaud-bucket.s3.amazonaws.com/audio.mp3?X-Amz-Signature=not-for-logs"
        version = subprocess.CompletedProcess([str(self.wrapper), "version"], 0, "plaud 0.3.7\ncommit abc\n", "")
        failed = subprocess.CompletedProcess([str(self.wrapper), "audio"], 1, "", f"failed: {audio_url}")
        cli, cli_hash, bundle_hash, config = self.trusted_patches()
        with cli, cli_hash, bundle_hash, config, mock.patch.object(subprocess, "run", side_effect=[version, failed]):
            with self.assertRaises(plaud_official.PlaudOfficialError) as raised:
                plaud_official._run("audio", "recording-id")
        self.assertNotIn(audio_url, str(raised.exception))
        self.assertNotIn("failed:", str(raised.exception))


class PollConfigurationTests(unittest.TestCase):
    def test_legacy_api_base_accepts_only_official_eu_https_endpoint(self):
        with mock.patch.dict(os.environ, {"PLAUD_API_BASE": "http://api-euc1.plaud.ai"}, clear=False):
            with self.assertRaisesRegex(ValueError, "official HTTPS Plaud EU"):
                load_module("plaud_poll_bad_base", SCRIPTS / "plaud-poll.py")

    def test_plaud_source_is_validated(self):
        with mock.patch.dict(os.environ, {"PLAUD_SOURCE": "unexpected"}, clear=False):
            with self.assertRaisesRegex(ValueError, "auto, official, legacy"):
                load_module("plaud_poll_bad_source", SCRIPTS / "plaud-poll.py")

    def test_whisper_provider_rejects_untrusted_endpoint(self):
        with mock.patch.dict(
            os.environ,
            {"WHISPER_API_URL": "https://attacker.invalid/transcribe"},
            clear=False,
        ):
            with self.assertRaisesRegex(ValueError, "Unsupported Whisper provider"):
                load_module("plaud_poll_bad_whisper_url", SCRIPTS / "plaud-poll.py")

    def test_whisper_provider_requires_matching_key_and_model(self):
        with mock.patch.dict(
            os.environ,
            {
                "WHISPER_API_URL": "https://api.openai.com/v1/audio/transcriptions",
                "WHISPER_MODEL": "whisper-1",
                "WHISPER_API_KEY_ENV": "GROQ_API_KEY",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(ValueError, "Unsupported Whisper provider"):
                load_module("plaud_poll_bad_whisper_key", SCRIPTS / "plaud-poll.py")

    def test_plaud_source_accepts_only_supported_values(self):
        poll = load_module("plaud_poll_sources", SCRIPTS / "plaud-poll.py")
        for source in ("auto", "official", "legacy"):
            with mock.patch.dict(os.environ, {"PLAUD_SOURCE": source}, clear=False):
                self.assertEqual(source, poll.plaud_source())

    def test_audio_download_error_does_not_expose_signed_url_from_full_traceback(self):
        poll = load_module("plaud_poll_download", SCRIPTS / "plaud-poll.py")
        audio_url = "https://euc1-prod-plaud-bucket.s3.amazonaws.com/audio.mp3?X-Amz-Signature=not-for-logs"
        with (
            tempfile.TemporaryDirectory() as tmp_dir,
            mock.patch.object(poll, "TMP_DIR", tmp_dir),
            mock.patch.object(poll, "official_available", return_value=True),
            mock.patch.object(poll.plaud_official, "get_audio_url", return_value=audio_url),
            mock.patch.object(poll.requests, "get", side_effect=poll.requests.RequestException(audio_url)) as get,
        ):
            with self.assertRaises(RuntimeError) as raised:
                poll.download_recording({"id": "recording-id", "source": "official_cli"})
        rendered_traceback = "".join(traceback.format_exception(raised.exception))
        self.assertNotIn(audio_url, rendered_traceback)
        self.assertNotIn("During handling of the above exception", rendered_traceback)
        self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_audio_download_refuses_redirects(self):
        poll = load_module("plaud_poll_redirect", SCRIPTS / "plaud-poll.py")
        audio_url = "https://euc1-prod-plaud-bucket.s3.amazonaws.com/audio.mp3?signature=redacted"
        response = mock.Mock(status_code=302)
        with (
            tempfile.TemporaryDirectory() as tmp_dir,
            mock.patch.object(poll, "TMP_DIR", tmp_dir),
            mock.patch.object(poll, "official_available", return_value=True),
            mock.patch.object(poll.plaud_official, "get_audio_url", return_value=audio_url),
            mock.patch.object(poll.requests, "get", return_value=response) as get,
        ):
            with self.assertRaisesRegex(RuntimeError, "Audio download failed"):
                poll.download_recording({"id": "recording-id", "source": "official_cli"})
        self.assertFalse(get.call_args.kwargs["allow_redirects"])


if __name__ == "__main__":
    unittest.main()
