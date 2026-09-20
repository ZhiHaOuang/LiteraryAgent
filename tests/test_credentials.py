from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lg_cli.config import load_config
from lg_cli.credentials import environment_dir, read_profiles, save_profile
from lg_cli.main import main


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"HOME": self.temp.name}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_persistence_switch_permissions_and_replacement(self):
        save_profile("sandbox", "first", "test-secret-1")
        save_profile("sandbox", "second", "test-secret-2")
        save_profile("sandbox", "first")
        config = load_config(Path(self.temp.name))
        self.assertEqual(config.api_key, "test-secret-1")
        self.assertNotIn("test-secret-1", repr(config))
        directory = environment_dir("sandbox")
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((directory / "credentials.json").stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            save_profile("sandbox", "first", "replacement")
        save_profile("sandbox", "first", "replacement", replace=True)
        self.assertEqual(load_config(Path(self.temp.name)).api_key, "replacement")

    def test_isolation_and_no_inherited_production_credentials(self):
        save_profile("sandbox", "test", "sandbox-key")
        save_profile("production", "live", "production-key")
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "inherited", "LG_BASE_URL": "https://unwanted.example"}):
            config = load_config(Path(self.temp.name), environment="sandbox")
            self.assertEqual(config.api_key, "sandbox-key")
            self.assertEqual(config.base_url, "https://api.deepseek.com/anthropic")
            self.assertIsNone(load_config(Path(self.temp.name), environment="empty").api_key)
        self.assertEqual(read_profiles("production")["active"], "live")

    def test_failed_atomic_replace_preserves_previous_key(self):
        save_profile("sandbox", "first", "original")
        with patch("lg_cli.credentials.os.replace", side_effect=OSError("simulated write failure")):
            with self.assertRaises(OSError):
                save_profile("sandbox", "first", "changed", replace=True)
        self.assertEqual(read_profiles("sandbox")["profiles"]["first"]["key"], "original")

    def test_cli_does_not_print_secret_and_supports_environment(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"TEST_KEY": "never-display-me"}), contextlib.redirect_stdout(output):
            self.assertEqual(main(["--environment", "test", "auth", "add", "one", "--key-env", "TEST_KEY"]), 0)
            self.assertEqual(main(["--environment=test", "auth", "list"]), 0)
            self.assertEqual(main(["--environment", "test", "auth", "use", "one"]), 0)
        self.assertNotIn("never-display-me", output.getvalue())
        self.assertIn("* one", output.getvalue())

    def test_bad_names_corrupt_store_and_symlink_fail_closed(self):
        with self.assertRaises(ValueError):
            save_profile("../escape", "name", "key")
        with self.assertRaises(ValueError):
            save_profile("sandbox", "missing")
        directory = environment_dir("sandbox")
        path = directory / "credentials.json"
        path.write_text('{"secret": "do-not-echo"}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "contents suppressed"):
            read_profiles("sandbox")
        other = environment_dir("linked")
        other.mkdir()
        (other / "credentials.json").symlink_to(path)
        with self.assertRaises(OSError):
            read_profiles("linked")
