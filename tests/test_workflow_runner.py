from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lg_cli.events import EventType
from lg_cli.init_project import init_workspace
from lg_cli.run_store import RunStore
from lg_cli.workflow_runner import EXIT_CONFIGURATION, WorkflowRunner

from tests.helpers import FakeAdapter, make_config


class WorkflowRunnerTests(unittest.TestCase):
    def test_outline_runs_all_stages_streams_events_and_writes_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            init_workspace(root)
            adapter = FakeAdapter()
            events = []
            result = WorkflowRunner(make_config(root), adapter=adapter).run(
                "outline", "A city fantasy outline", on_event=events.append
            )
            self.assertTrue(result.ok, result.error)
            self.assertEqual(len(adapter.calls), 7)
            self.assertTrue(result.artifact_path and result.artifact_path.exists())
            self.assertTrue(result.latest_path and result.latest_path.exists())
            self.assertIn("Codex is the inference engine, not the product identity", adapter.calls[0]["prompt"])
            self.assertIsNotNone(adapter.calls[0]["output_schema"])
            self.assertEqual([event.sequence for event in events], list(range(1, len(events) + 1)))
            self.assertEqual(events[-1].type, EventType.RUN_COMPLETED)
            manifest = RunStore(root).load(result.run_id)
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(len(manifest["completed_stages"]), 7)

    def test_failure_has_nonzero_exit_and_resume_restores_completed_stages(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            init_workspace(root)
            failed = WorkflowRunner(make_config(root), adapter=FakeAdapter(fail_at=3)).run(
                "outline", "Resume this outline"
            )
            self.assertEqual(failed.exit_code, 4)
            failed_manifest = RunStore(root).load(failed.run_id)
            self.assertEqual(failed_manifest["completed_stages"], ["direction-brief", "reference-patterns"])

            adapter = FakeAdapter()
            events = []
            resumed = WorkflowRunner(make_config(root), adapter=adapter).resume(
                failed.run_id, on_event=events.append
            )
            self.assertTrue(resumed.ok, resumed.error)
            self.assertEqual(len(adapter.calls), 5)
            resumed_manifest = RunStore(root).load(resumed.run_id)
            self.assertEqual(resumed_manifest["resumed_from"], failed.run_id)
            self.assertEqual(len(resumed_manifest["completed_stages"]), 7)
            restored = [event for event in events if event.data.get("restored")]
            self.assertEqual(len(restored), 2)

    def test_dry_run_needs_no_key_but_real_run_fails_with_configuration_code(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = make_config(root, api_key=None)
            adapter = FakeAdapter()
            runner = WorkflowRunner(config, adapter=adapter)
            planned = runner.run("world", "Build a world", dry_run=True)
            self.assertEqual(planned.status, "planned")
            self.assertEqual(adapter.calls, [])
            failed = runner.run("world", "Build a world")
            self.assertEqual(failed.exit_code, EXIT_CONFIGURATION)
            self.assertEqual(adapter.calls, [])

    def test_all_requested_workflows_resolve_in_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runner = WorkflowRunner(make_config(root, api_key=None), adapter=FakeAdapter())
            for command in ("outline", "world", "character", "plot", "check", "write", "ref"):
                with self.subTest(command=command):
                    result = runner.run(command, f"test {command}", dry_run=True)
                    self.assertEqual(result.status, "planned")
                    self.assertIn(f"LG Workflow Plan: {command}", result.text)

    def test_stage_schema_is_strict_and_machine_readable(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for schema_path in sorted((root / "lg-subagents").glob("*/output.schema.json")):
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            self.assertFalse(schema["additionalProperties"])
            self.assertFalse(schema["properties"]["handoff"]["additionalProperties"])
            self.assertEqual(set(schema["required"]), set(schema["properties"]))


if __name__ == "__main__":
    unittest.main()
