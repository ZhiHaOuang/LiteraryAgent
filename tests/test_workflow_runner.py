from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from lg_cli.events import EventType
from lg_cli.init_project import init_workspace
from lg_cli.project_store import ProjectStore
from lg_cli.run_store import RunStore
from lg_cli.workflow_runner import EXIT_CONFIGURATION, WorkflowRunner

from tests.helpers import FakeAdapter, make_config


class WorkflowRunnerTests(unittest.TestCase):
    def test_adopt_manuscript_requires_container_and_preserves_scene_state(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            init_workspace(root)
            project = ProjectStore(root)
            result = WorkflowRunner(make_config(root), adapter=FakeAdapter()).run("write", "Draft prose")
            runs = RunStore(root)
            for kind in ("chapter", "scene"):
                with self.assertRaisesRegex(ValueError, "container"):
                    runs.adopt(result.run_id, slug="missing", kind=kind, title="Missing")
            chapter = project.create_document(kind="chapter", slug="chapter", title="Chapter", sequence=1)
            scene = project.create_scene(slug="scene", title="Scene", chapter=chapter.id, sequence=1)
            before = project.get_scene(scene.document.id).status
            first = runs.adopt(result.run_id, slug="scene", kind="scene", title="Scene")
            self.assertFalse(first["active"])
            self.assertEqual(project.get_scene("scene").status, before)
            self.assertEqual(project.get_document("scene").content, "")
            self.assertEqual(first, runs.adopt(result.run_id, slug="scene", kind="scene", title="Scene"))
            with self.assertRaisesRegex(ValueError, "review"):
                runs.adopt(result.run_id, slug="scene", kind="scene", title="Scene", accept=True)
            from lg_cli.book_analysis import BookAnalysisStore
            from lg_cli.supervision import DEFAULT_CATEGORIES

            records = BookAnalysisStore(project)
            version = project.get_version("scene", first["version"])
            base = records.snapshot("chapter")
            snapshot = records.candidate_snapshot("chapter", version)
            for category in DEFAULT_CATEGORIES:
                records.save(snapshot, category, {"summary": "Checked", "entries": [], "findings": []},
                             "test-review", candidate_id=version.id, base_snapshot=base)
            accepted = runs.adopt(result.run_id, slug="scene", kind="scene", title="Scene", accept=True)
            self.assertTrue(accepted["active"])
            self.assertEqual(project.get_scene("scene").status, "accepted")
            self.assertEqual(project.get_scene("scene").chapter_id, chapter.id)
            self.assertEqual(len(project.list_versions("scene")), 2)
            self.assertEqual(project.get_version("scene", 2).metadata["run_id"], result.run_id)

    def test_adopt_is_local_versioned_and_idempotent(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            init_workspace(root)
            runner = WorkflowRunner(make_config(root), adapter=FakeAdapter())
            result = runner.run("world", "World design")
            runs = RunStore(root)
            first = runs.adopt(result.run_id, slug="world", kind="world", title="World")
            again = runs.adopt(result.run_id, slug="world", kind="world", title="World")
            self.assertEqual(first, again)
            project = ProjectStore(root)
            self.assertEqual(len(project.list_versions("world")), 1)
            original = project.get_document("world").content
            second = runner.run("chat", "Changed design")
            candidate = runs.adopt(second.run_id, slug="world", kind="world", title="World")
            self.assertFalse(candidate["active"])
            self.assertEqual(project.get_document("world").content, original)
            accepted = runs.adopt(second.run_id, slug="world", kind="world", title="World", accept=True)
            self.assertTrue(accepted["active"])
            self.assertEqual(len(project.list_versions("world")), 2)
            planned = runner.run("world", "Plan only", dry_run=True)
            with self.assertRaisesRegex(ValueError, "completed"):
                runs.adopt(planned.run_id, slug="invalid", kind="note", title="Invalid")
            handle = runs.create(workflow="test", command="test", request="", provider="test", model="test")
            runs.finalize(handle, status="completed", artifact_path=root.parent / "outside.md")
            with self.assertRaisesRegex(ValueError, "inside the current book"):
                runs.adopt(handle.run_id, slug="escape", kind="note", title="Invalid")

    def test_recent_stage_survives_context_clipping(self):
        class LongBriefAdapter(FakeAdapter):
            def run(self, **kwargs):
                result = super().run(**kwargs)
                if len(self.calls) == 1:
                    payload = json.loads(result.output_text)
                    payload["artifact_markdown"] = "x" * 20000
                    return replace(result, output_text=json.dumps(payload))
                return result

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            init_workspace(root)
            adapter = LongBriefAdapter()
            result = WorkflowRunner(make_config(root), adapter=adapter).run("world", "Design world")
            self.assertTrue(result.ok)
            self.assertIn("# Stage 2", adapter.calls[2]["prompt"])

    def test_workflow_reads_book_database_without_keyword_match(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            init_workspace(root)
            project = ProjectStore(root)
            project.set_fact(category="rule", key="cost", value="Every crossing costs a memory.")
            project.create_document(kind="outline", slug="plan", title="Plan",
                                    content="The courier returns the stolen archive.", state="accepted")
            adapter = FakeAdapter()
            WorkflowRunner(make_config(root), adapter=adapter).run("world", "继续设计")
            self.assertIn("Every crossing costs a memory.", adapter.calls[0]["prompt"])
            self.assertIn("The courier returns the stolen archive.", adapter.calls[0]["prompt"])

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
            self.assertIn("Your application identity is LiteraryGiant", adapter.calls[0]["prompt"])
            self.assertNotIn("Codex", adapter.calls[0]["prompt"])
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
