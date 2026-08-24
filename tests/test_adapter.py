from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lg_cli.core_adapter import CodexExecAdapter, discover_core_commands

from tests.helpers import make_config


FAKE_CODEX = r'''#!/usr/bin/env python3
import json
import os
import pathlib
import sys

if "--version" in sys.argv:
    print("fake-codex 1.0.0")
    raise SystemExit(0)

prompt = sys.stdin.read()
pathlib.Path(os.environ["FAKE_PROMPT_PATH"]).write_text(prompt, encoding="utf-8")
pathlib.Path(os.environ["FAKE_ARGV_PATH"]).write_text(json.dumps(sys.argv), encoding="utf-8")
pathlib.Path(os.environ["FAKE_ENV_PATH"]).write_text(
    json.dumps({"CODEX_API_KEY": os.environ.get("CODEX_API_KEY"), "CODEX_HOME": os.environ.get("CODEX_HOME")}),
    encoding="utf-8",
)
final_index = sys.argv.index("--output-last-message") + 1
pathlib.Path(sys.argv[final_index]).write_text("adapter result", encoding="utf-8")
print(json.dumps({"type": "turn.started"}))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "adapter result"}}))
'''


class AdapterTests(unittest.TestCase):
    def test_prompt_uses_stdin_and_candidate_is_health_checked(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            script = root / "fake-codex"
            script.write_text(FAKE_CODEX, encoding="utf-8")
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
            prompt_path = root / "prompt.txt"
            argv_path = root / "argv.json"
            env_path = root / "env.json"
            env = {
                "LG_CODEX_COMMAND": f"{sys.executable} {script}",
                "FAKE_PROMPT_PATH": str(prompt_path),
                "FAKE_ARGV_PATH": str(argv_path),
                "FAKE_ENV_PATH": str(env_path),
            }
            config = make_config(root)
            events = []
            with patch.dict(os.environ, env, clear=False):
                result = CodexExecAdapter(root / "missing-core").run(
                    prompt="private prompt via stdin",
                    config=config,
                    mode="outline",
                    on_event=events.append,
                )
            self.assertTrue(result.ok, result.error)
            self.assertEqual(prompt_path.read_text(encoding="utf-8"), "private prompt via stdin\n")
            argv = json.loads(argv_path.read_text(encoding="utf-8"))
            self.assertNotIn("private prompt via stdin", argv)
            self.assertEqual(argv[-1], "-")
            child_env = json.loads(env_path.read_text(encoding="utf-8"))
            self.assertEqual(child_env["CODEX_API_KEY"], "test-key")
            self.assertEqual(stat.S_IMODE(Path(child_env["CODEX_HOME"]).stat().st_mode), 0o700)
            self.assertEqual(len(events), 2)

    def test_failed_version_probe_marks_env_candidate_unhealthy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            script = root / "bad-codex"
            script.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
            with patch.dict(os.environ, {"LG_CODEX_COMMAND": str(script)}, clear=False):
                candidates = discover_core_commands(root / "missing", probe=True)
            candidate = next(item for item in candidates if item.name == "env:LG_CODEX_COMMAND")
            self.assertFalse(candidate.available)
            self.assertIn("version probe exited 7", candidate.reason)

    def test_missing_key_returns_configuration_failure_without_launch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = make_config(Path(raw), api_key=None)
            result = CodexExecAdapter(Path(raw) / "core").run(
                prompt="test",
                config=config,
                mode="outline",
            )
            self.assertFalse(result.ok)
            self.assertTrue(result.used_stub)
            self.assertIn("no API key", result.error or "")


if __name__ == "__main__":
    unittest.main()
