from __future__ import annotations

import io
import asyncio
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from lg_cli.auth_ui import _manage_auth as manage_auth
from lg_cli.config import load_config
from lg_cli.core_adapter import _adapter_env
from lg_cli.credentials import checked_name, environment_dir, read_profiles, save_profile
from lg_cli.main import interactive_loop, main
from lg_cli.providers import PROVIDERS, validate_endpoint


class ProviderProfileTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = patch.dict(os.environ, {"HOME": tmp.name}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_all_provider_profiles_keep_model_endpoint_and_key_together(self):
        for provider, preset in PROVIDERS.items():
            with self.subTest(provider=provider):
                save_profile(
                    "test",
                    provider,
                    f"secret-{provider}",
                    provider=provider,
                    model="account-model",
                    base_url=preset.base_url or "http://127.0.0.1:8317/v1",
                )
                config = load_config(self.root, environment="test")
                self.assertEqual(config.provider, provider)
                self.assertEqual(config.default_model, "account-model")
                self.assertEqual(config.protocol, preset.protocol)
                self.assertEqual(config.api_key, f"secret-{provider}")
                self.assertEqual(config.uses_anthropic, preset.protocol == "anthropic")
                self.assertEqual(config.uses_responses, preset.protocol == "responses")
        save_profile("test", "glm", model="other-model")
        config = load_config(self.root, environment="test")
        self.assertEqual(config.default_model, "other-model")
        self.assertEqual(config.api_key, "secret-glm")

    def test_dotted_profile_can_be_saved_switched_and_loaded(self):
        name = "step-3.7-flash"
        save_profile("sandbox", name, "test-key", provider="stepfun", model=name)
        save_profile("sandbox", "other", "other-test-key")
        save_profile("sandbox", name)
        config = load_config(self.root, environment="sandbox")
        self.assertEqual(config.profile_name, name)
        self.assertEqual(config.provider, "stepfun")
        self.assertEqual(config.default_model, name)
        self.assertEqual(read_profiles("sandbox")["active"], name)

    def test_name_validation_still_rejects_unsafe_names(self):
        for name in ("", ".", "..", "../profile", "a/b", "a\\b", ".hidden", "a b", "a\n", "a" * 65):
            with self.subTest(name=name), self.assertRaises(ValueError):
                checked_name(name)
        self.assertEqual(checked_name("a" * 64), "a" * 64)

    def test_legacy_store_is_read_without_rewrite_and_migrates_on_save(self):
        directory = environment_dir("sandbox")
        directory.mkdir(parents=True)
        path = directory / "credentials.json"
        original = json.dumps(
            {
                "active": "old",
                "profiles": {
                    "old": {"provider": "deepseek-anthropic", "key": "old-key"}
                },
            }
        )
        path.write_text(original)
        config = load_config(self.root)
        self.assertEqual(config.default_model, "deepseek-flash")
        self.assertEqual(config.api_key, "old-key")
        self.assertEqual(path.read_text(), original)
        save_profile(
            "sandbox", "glm", "glm-key", provider="glm", model="glm-account-model"
        )
        saved = json.loads(path.read_text())
        self.assertEqual(saved["version"], 2)
        self.assertEqual(saved["profiles"]["old"]["key"], "old-key")

    def test_key_rotation_preserves_model_and_url(self):
        save_profile(
            "test",
            "step",
            "old",
            provider="stepfun",
            model="custom-step",
            base_url="https://api.stepfun.com/step_plan",
        )
        save_profile("test", "step", "new", replace=True)
        config = load_config(self.root, environment="test")
        self.assertEqual(config.default_model, "custom-step")
        self.assertEqual(config.base_url, "https://api.stepfun.com/step_plan")

    def test_failed_update_preserves_existing_profile(self):
        save_profile("test", "one", "old")
        with self.assertRaises(ValueError):
            save_profile("test", "one", "new", replace=True, provider="glm")
        self.assertEqual(read_profiles("test")["profiles"]["one"]["key"], "old")

    def test_implicit_sandbox_loads_its_environment_configuration(self):
        save_profile("sandbox", "one", "key")
        (environment_dir("sandbox") / "config.toml").write_text(
            "[agent]\ntimeout_seconds=123\n"
        )
        config = load_config(self.root)
        self.assertEqual(config.timeout_seconds, 123)
        self.assertEqual(config.environment, "sandbox")

    def test_other_provider_env_keys_do_not_leak_into_child(self):
        save_profile("test", "one", "active-secret", provider="glm", model="glm-test")
        config = load_config(self.root, environment="test")
        names = [name for preset in PROVIDERS.values() for name in preset.key_envs]
        with patch.dict(
            os.environ,
            {
                **{name: "other-secret" for name in names},
                "MY_CUSTOM_KEY": "active-secret",
            },
        ):
            child = _adapter_env(config)
        for name in [*names, "MY_CUSTOM_KEY", "CODEX_API_KEY"]:
            self.assertNotIn(name, child)

    def test_provider_environment_keys_are_scoped(self):
        for provider, key_name in [("glm", "GLM_API_KEY"), ("stepfun", "STEP_API_KEY")]:
            with patch.dict(
                os.environ,
                {
                    "LG_PROVIDER": provider,
                    "LG_MODEL": "account-model",
                    "OPENAI_API_KEY": "wrong-key",
                },
            ):
                self.assertIsNone(load_config(self.root).api_key)
                with patch.dict(os.environ, {key_name: "right-key"}):
                    self.assertEqual(load_config(self.root).api_key, "right-key")

    def test_endpoint_validation_does_not_echo_credential(self):
        for url in [
            "https://secret@example.com",
            "https://example.com?key=secret",
            "http://192.168.0.1/v1",
            "https://example.com:bad",
            "https://example.com/\nsecret",
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError) as error:
                validate_endpoint(url)
            self.assertNotIn("secret", str(error.exception))
        self.assertEqual(
            validate_endpoint("http://127.0.0.1:8317/v1/"), "http://127.0.0.1:8317/v1"
        )

    def test_gpt_does_not_silently_choose_paid_api(self):
        output = io.StringIO()
        with redirect_stderr(output):
            code = main(["auth", "add", "gpt"])
        self.assertEqual(code, 1)
        self.assertIn("Subscription", output.getvalue())
        self.assertFalse(read_profiles("sandbox")["profiles"])

    def test_gpt_interactive_choice_is_explicit_and_cancellable(self):
        for choice in (None, "codex"):
            dialog = Mock()
            dialog.run.return_value = choice
            with (
                self.subTest(choice=choice),
                redirect_stdout(io.StringIO()),
                patch("sys.stdin.isatty", return_value=True),
                patch("sys.stdout.isatty", return_value=True),
                patch("lg_cli.auth_ui.radiolist_dialog", return_value=dialog),
                patch("lg_cli.subscription_auth.login_subscription", return_value=0) as login,
            ):
                self.assertEqual(main(["-C", str(self.root), "auth", "add", "gpt"]), 0)
            self.assertEqual(login.call_count, 1 if choice == "codex" else 0)
        self.assertFalse(read_profiles("sandbox")["profiles"])

    def test_cli_provider_inference_and_model_switch(self):
        with (
            patch.dict(os.environ, {"TEST_KEY": "private-test-key"}),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                main(
                    [
                        "auth",
                        "add",
                        "glm",
                        "--model",
                        "glm-account-model",
                        "--key-env",
                        "TEST_KEY",
                    ]
                ),
                0,
            )
            self.assertEqual(main(["auth", "model", "glm-other"]), 0)
            self.assertEqual(main(["auth", "list"]), 0)
            self.assertNotIn("private-test-key", output.getvalue())
        self.assertEqual(load_config(self.root).default_model, "glm-other")

    def test_interactive_switch_updates_next_request_and_preserves_environment(self):
        save_profile("experiment", "old", "old-key")
        save_profile("experiment", "glm", "glm-key", provider="glm", model="glm-test")
        save_profile("experiment", "old")
        config = load_config(self.root, environment="experiment")
        session = Mock()
        session.run_command = AsyncMock(return_value=0)
        def run(handler, model, provider):
            async def exercise():
                for value in ("/auth use glm", "continue the novel", "/exit"):
                    await handler(value)
                return 0
            return asyncio.run(exercise())
        session.run.side_effect = run
        async def terminal(function, **kwargs):
            return function()
        with (
            patch("lg_cli.main.LiteraryInput", return_value=session),
            patch("lg_cli.main.run_in_terminal", side_effect=terminal),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(interactive_loop(config), 0)
        used = load_config(self.root, environment="experiment")
        self.assertEqual(
            (used.provider, used.api_key, used.environment),
            ("glm", "glm-key", "experiment"),
        )
        self.assertFalse(read_profiles("sandbox")["profiles"])
        self.assertEqual(session.provider, "glm")
        argv = session.run_command.call_args.args[0]
        self.assertEqual(argv[argv.index("--environment") + 1], "experiment")
        self.assertEqual(argv[-1], "continue the novel")

    def test_tui_cancel_preserves_active_profile(self):
        save_profile("test", "old", "key")
        dialog = Mock()
        dialog.run.return_value = None
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("sys.stdout.isatty", return_value=True),
            patch("lg_cli.auth_ui.radiolist_dialog", return_value=dialog),
        ):
            self.assertEqual(manage_auth("test"), 0)
        self.assertEqual(read_profiles("test")["active"], "old")

    def test_tui_activates_selected_profile_without_displaying_key(self):
        save_profile("test", "one", "sensitive-value")
        save_profile("test", "two", "another-key")
        dialogs = [Mock(), Mock()]
        dialogs[0].run.return_value = "one"
        dialogs[1].run.return_value = "use"
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("sys.stdout.isatty", return_value=True),
            patch("lg_cli.auth_ui.radiolist_dialog", side_effect=dialogs) as ui,
        ):
            self.assertEqual(manage_auth("test"), 0)
        self.assertNotIn("sensitive-value", str(ui.call_args_list))
        self.assertEqual(read_profiles("test")["active"], "one")
