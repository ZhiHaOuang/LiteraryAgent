from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from lg_cli.main import main


class CLITests(unittest.TestCase):
    def test_story_ledger_review_and_export_commands(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            registry = str(Path(raw) / "registry")

            def invoke(*arguments: str):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(["-C", raw, *arguments])
                self.assertEqual(code, 0, stderr.getvalue())
                return json.loads(stdout.getvalue().splitlines()[-1])

            with patch.dict(os.environ, {"LITERARYGIANT_REGISTRY_HOME": registry}):
                invoke("project", "create", "Ledger Test", "--path", raw, "--json")
                invoke("chapter", "create", "chapter-1", "Chapter One", "--json")
                timeline = invoke(
                    "timeline",
                    "add",
                    "Day One",
                    "Lin reaches the city.",
                    "--order",
                    "001",
                    "--state",
                    "canonical",
                    "--json",
                )
                self.assertEqual(timeline["state"], "canonical")
                thread = invoke(
                    "foreshadowing",
                    "add",
                    "Red Ribbon",
                    "A ribbon hangs from the gate.",
                    "--json",
                )
                paid = invoke(
                    "foreshadowing",
                    "resolve",
                    str(thread["id"]),
                    "The ribbon identifies the hidden courier.",
                    "--json",
                )
                self.assertEqual(paid["state"], "paid")
                paid_items = invoke("foreshadowing", "list", "--state", "paid", "--json")
                self.assertEqual([item["id"] for item in paid_items], [thread["id"]])
                invoke(
                    "bible",
                    "set",
                    "world",
                    "gate-curfew",
                    "The gate closes at dusk.",
                    "--json",
                )
                review = invoke("review", "consistency", "--json")
                self.assertEqual(review["errors"], 0)
                output = Path(raw) / "story-bible.md"
                exported = invoke(
                    "export",
                    "bible",
                    "--format",
                    "md",
                    "--output",
                    str(output),
                    "--json",
                )
                self.assertEqual(Path(exported["path"]), output)
                self.assertIn("gate-curfew", output.read_text(encoding="utf-8"))

    def test_skill_descriptions_do_not_repeat_use_when(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(main(["-C", raw, "skills"]), 0)
            self.assertNotRegex(stdout.getvalue(), r"Use when when\b")

    def test_story_project_commands_run_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            registry = str(Path(raw) / "registry")
            stdout = io.StringIO()
            with patch.dict(os.environ, {"LITERARYGIANT_REGISTRY_HOME": registry}):
                with redirect_stdout(stdout):
                    self.assertEqual(
                        main(["-C", raw, "project", "create", "Test Novel", "--path", raw]),
                        0,
                    )
                    self.assertEqual(
                        main(["-C", raw, "chapter", "create", "chapter-1", "Chapter One"]),
                        0,
                    )
                    self.assertEqual(
                        main(
                            [
                                "-C",
                                raw,
                                "scene",
                                "create",
                                "arrival",
                                "Arrival",
                                "--chapter",
                                "chapter-1",
                                "--goal",
                                "Enter the city",
                            ]
                        ),
                        0,
                    )
                    self.assertEqual(main(["-C", raw, "status", "--json"]), 0)
            lines = stdout.getvalue().splitlines()
            status = json.loads(lines[-1])
            self.assertEqual(status["project"]["name"], "Test Novel")
            self.assertEqual(status["project"]["chapters"], 1)
            self.assertEqual(status["project"]["scenes"], 1)

    def test_natural_language_routes_to_world_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(["-C", raw, "--dry-run", "--quiet", "帮我设计一个世界观"])
            self.assertEqual(code, 0)
            self.assertIn("# LG Workflow Plan: world", stdout.getvalue())

    def test_json_dry_run_emits_events_and_result(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(["-C", raw, "outline", "--dry-run", "--json", "test"])
            self.assertEqual(code, 0)
            records = [json.loads(line) for line in stdout.getvalue().splitlines()]
            self.assertEqual(records[0]["type"], "run.started")
            self.assertEqual(records[-1]["type"], "run.result")
            self.assertEqual(records[-1]["status"], "planned")

    def test_missing_key_returns_nonzero_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            stdout = io.StringIO()
            stderr = io.StringIO()
            clean_env = {
                "HOME": raw,
                "LITERARYGIANT_API_KEY": "",
                "LG_API_KEY": "",
                "CODEX_API_KEY": "",
                "OPENAI_API_KEY": "",
            }
            with patch.dict(os.environ, clean_env, clear=False):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(["-C", raw, "world", "--quiet", "test"])
            self.assertEqual(code, 3)
            self.assertIn("No API key", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
