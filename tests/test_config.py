from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lg_cli.config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def test_deepseek_credentials_are_provider_scoped(self):
        with tempfile.TemporaryDirectory() as raw:
            with patch.dict(os.environ, {"HOME": raw, "LG_PROVIDER": "deepseek-anthropic", "OPENAI_API_KEY": "wrong-provider"}, clear=True):
                config = load_config(Path(raw))
                self.assertIsNone(config.api_key)
                self.assertEqual(config.default_model, "deepseek-flash")
                self.assertEqual(config.base_url, "https://api.deepseek.com/anthropic")
                with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "deepseek-secret"}):
                    config = load_config(Path(raw))
                    self.assertEqual(config.api_key_source, "env:DEEPSEEK_API_KEY")
                    self.assertNotIn("deepseek-secret", repr(config))
                with patch.dict(os.environ, {"LG_BASE_URL": "https://secret@example.com"}):
                    with self.assertRaises(ConfigError):
                        load_config(Path(raw))

    def test_standard_toml_env_priority_and_model_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "home"
            workspace = root / "project"
            (home / ".literarygiant").mkdir(parents=True)
            (workspace / ".literarygiant").mkdir(parents=True)
            (workspace / ".literarygiant" / "config.toml").write_text(
                '[model]\nprovider = "project"\ndefault = "project-default"\nwriter = "project-writer"\n'
                '[agent]\nexecution_strategy = "staged"\ntimeout_seconds = 120\n',
                encoding="utf-8",
            )
            (home / ".literarygiant" / "config.toml").write_text(
                '[model]\nprovider = "user"\ncritic = "user-critic"\n',
                encoding="utf-8",
            )
            env = {
                "HOME": str(home),
                "LG_MODEL": "env-model",
                "LG_PROVIDER": "env-provider",
                "LG_API_KEY": "secret",
                "LITERARYGIANT_API_KEY": "",
                "CODEX_API_KEY": "",
                "OPENAI_API_KEY": "",
            }
            with patch.dict(os.environ, env, clear=False):
                config = load_config(workspace)
            self.assertEqual(config.provider, "env-provider")
            self.assertEqual(config.default_model, "env-model")
            self.assertEqual(config.writer_model, "project-writer")
            self.assertEqual(config.critic_model, "user-critic")
            self.assertEqual(config.model_for_profile("writer"), "project-writer")
            self.assertEqual(config.model_for_profile("critic"), "user-critic")
            self.assertEqual(config.api_key_source, "env:LG_API_KEY")

    def test_invalid_typed_value_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config_root = root / ".literarygiant"
            config_root.mkdir()
            (config_root / "config.toml").write_text(
                '[agent]\ntimeout_seconds = "forever"\n', encoding="utf-8"
            )
            with patch.dict(os.environ, {"HOME": raw}, clear=False):
                with self.assertRaises(ConfigError):
                    load_config(root)


if __name__ == "__main__":
    unittest.main()
