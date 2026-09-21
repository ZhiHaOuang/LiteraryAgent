from __future__ import annotations

import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from lg_cli.config import load_config
from lg_cli.core_adapter import _adapter_env, _exec_args
from lg_cli.credentials import (
    environment_dir,
    read_profiles,
    save_profile,
    save_subscription,
)
from lg_cli.providers import normalize_profile
from lg_cli.subscription_auth import (
    login_subscription,
    pinned_command,
    subscription_status,
)

from tests.helpers import make_config


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = patch.dict(os.environ, {"HOME": tmp.name}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_profile_has_no_api_key_or_exported_tokens(self):
        save_subscription("test", "gpt")
        config = load_config(self.root, environment="test")
        self.assertEqual((config.provider, config.auth_mode), ("codex", "chatgpt"))
        self.assertIsNone(config.api_key)
        self.assertNotIn("key", read_profiles("test")["profiles"]["gpt"])
        self.assertFalse(config.credentials_configured)
        with self.assertRaises(ValueError):
            normalize_profile(
                {"provider": "codex", "auth_mode": "chatgpt", "key": "should-not-copy"}
            )
        with self.assertRaises(ValueError):
            save_profile("test", "gpt", "api-key", replace=True)

    def test_api_name_cannot_be_replaced_by_subscription(self):
        save_profile("test", "gpt-api", "key")
        with self.assertRaises(ValueError):
            save_subscription("test", "gpt-api")
        self.assertEqual(read_profiles("test")["profiles"]["gpt-api"]["key"], "key")

    def test_subscription_uses_isolated_home_and_forces_chatgpt(self):
        save_subscription("test", "gpt", "account-model")
        config = load_config(self.root, environment="test")
        with patch.dict(
            os.environ,
            {
                "CODEX_HOME": "/production/codex",
                "CODEX_API_KEY": "production-key",
                "CODEX_ACCESS_TOKEN": "production-token",
                "OPENAI_API_KEY": "production-key",
            },
        ):
            env = _adapter_env(config)
        self.assertEqual(
            env["CODEX_HOME"], str(environment_dir("test") / "codex-auth" / "gpt")
        )
        self.assertNotIn("CODEX_API_KEY", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("CODEX_ACCESS_TOKEN", env)
        args = _exec_args(
            config=config,
            mode="chat",
            model_profile=None,
            final_path=self.root / "final",
            output_schema=None,
        )
        self.assertIn('forced_login_method="chatgpt"', args)
        self.assertIn('model_provider="openai"', args)
        self.assertNotIn('model_provider="codex"', args)

    def test_login_requires_pinned_runtime_without_installing_anything(self):
        with self.assertRaises(ValueError):
            pinned_command(make_config(self.root))

    def test_status_checks_only_official_cache_and_suppresses_other_auth_details(self):
        save_subscription("test", "gpt")
        config = load_config(self.root, environment="test")
        env = _adapter_env(config)
        (Path(env["CODEX_HOME"]) / "auth.json").write_text("{}")
        for output, expected in [
            ("Logged in using ChatGPT\n", True),
            ("Logged in using an API key - private-key", False),
        ]:
            result = subprocess.CompletedProcess(["codex"], 0, "", output)
            with patch("lg_cli.subscription_auth.subprocess.run", return_value=result):
                ok, message = subscription_status(config, command=("test-codex",))
            self.assertEqual(ok, expected)
            self.assertNotIn("private-key", message)

    def test_login_only_activates_after_successful_official_status(self):
        save_profile("test", "old", "key")
        process = Mock()
        process.wait.return_value = 0
        directory = environment_dir("test") / "codex-auth" / "gpt"

        def start(*args, **kwargs):
            self.assertIn("--device-auth", args[0])
            self.assertEqual(kwargs["env"]["CODEX_HOME"], str(directory))
            self.assertNotIn("CODEX_API_KEY", kwargs["env"])
            (directory / "auth.json").write_text("{}")
            return process

        with (
            redirect_stdout(io.StringIO()),
            patch("sys.stdin.isatty", return_value=True),
            patch("sys.stdout.isatty", return_value=True),
            patch(
                "lg_cli.subscription_auth.pinned_command",
                return_value=("verified-codex",),
            ),
            patch("lg_cli.subscription_auth.subprocess.Popen", side_effect=start),
            patch(
                "lg_cli.subscription_auth.subscription_status",
                return_value=(True, "cached"),
            ),
        ):
            self.assertEqual(login_subscription("test", "gpt", self.root), 0)
        self.assertEqual(read_profiles("test")["active"], "gpt")
        self.assertEqual((directory / "auth.json").stat().st_mode & 0o777, 0o600)

    def test_failed_login_does_not_change_active_profile(self):
        save_profile("test", "old", "key")
        process = Mock()
        process.wait.return_value = 9
        with (
            redirect_stdout(io.StringIO()),
            patch("sys.stdin.isatty", return_value=True),
            patch("sys.stdout.isatty", return_value=True),
            patch(
                "lg_cli.subscription_auth.pinned_command",
                return_value=("verified-codex",),
            ),
            patch("lg_cli.subscription_auth.subprocess.Popen", return_value=process),
            patch("lg_cli.subscription_auth.subscription_status") as status,
        ):
            self.assertEqual(login_subscription("test", "gpt", self.root), 9)
        status.assert_not_called()
        self.assertEqual(read_profiles("test")["active"], "old")

    def test_cancelled_login_terminates_owned_process(self):
        process = Mock()
        process.wait.side_effect = [KeyboardInterrupt, 0]
        with (
            redirect_stdout(io.StringIO()),
            patch("sys.stdin.isatty", return_value=True),
            patch("sys.stdout.isatty", return_value=True),
            patch(
                "lg_cli.subscription_auth.pinned_command",
                return_value=("verified-codex",),
            ),
            patch("lg_cli.subscription_auth.subprocess.Popen", return_value=process),
        ):
            self.assertEqual(login_subscription("test", "gpt", self.root), 130)
        process.terminate.assert_called_once()
        self.assertFalse(read_profiles("test")["profiles"])
