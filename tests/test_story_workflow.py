from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lg_cli.core_adapter import CoreExecutionResult
from lg_cli.project_store import ProjectStore
from lg_cli.story_workflow import StoryWorkflowService

from .helpers import make_config


class StoryAdapter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, *, prompt, config, mode, model_profile=None, output_schema=None, on_event=None):
        schema = Path(output_schema).name
        self.calls.append(schema)
        if schema == "scene-plan.schema.json":
            payload = {
                "summary": "A concrete plan is ready.",
                "plan_markdown": "# Plan\n\n1. Lin enters.\n2. The guard refuses him.",
                "card_updates": {"location": "North Gate"},
                "memory_proposals": [
                    {
                        "category": "character",
                        "key": "lin-fear",
                        "value": "Lin fears enclosed gates.",
                        "tags": ["lin"],
                        "rationale": "The plan introduces a durable trait.",
                    }
                ],
                "risks": [],
            }
        else:
            payload = {
                "summary": "The scene draft is ready for review.",
                "draft_markdown": "Lin stopped beneath the North Gate. The guard lowered his spear.",
                "continuity_notes": ["Lin does not enter the city yet."],
                "memory_proposals": [
                    {
                        "category": "world",
                        "key": "north-gate-curfew",
                        "value": "The North Gate closes at dusk.",
                        "tags": ["north-gate"],
                        "rationale": "The generated scene mentions a curfew.",
                    }
                ],
                "risks": [],
            }
        return CoreExecutionResult(
            ok=True,
            used_stub=False,
            output_text=json.dumps(payload),
            error=None,
            command_name="story-test",
            command=["story-test"],
            returncode=0,
            stdout="",
            stderr="",
            duration_seconds=0.01,
        )


class StoryWorkflowTests(unittest.TestCase):
    def test_plan_approval_draft_and_memory_gates(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = ProjectStore(root)
            store.initialize(name="Workflow Test")
            chapter = store.create_document(kind="chapter", slug="chapter-1", title="Chapter 1")
            scene = store.create_scene(
                slug="gate-refusal",
                title="The Refusal",
                chapter=chapter.id,
                goal="Enter the city",
                conflict="The guard refuses entry",
                characters=["Lin", "Guard"],
            )
            adapter = StoryAdapter()
            service = StoryWorkflowService(make_config(root), project=store, adapter=adapter)

            blocked = service.draft_scene(scene.document.id)
            self.assertNotEqual(blocked.exit_code, 0)
            self.assertEqual(adapter.calls, [])

            planned = service.plan_scene(scene.document.id)
            self.assertTrue(planned.ok)
            self.assertEqual(store.get_scene(scene.document.id).plan_state, "candidate")
            self.assertEqual(store.get_document(scene.document.id).content, "")
            self.assertEqual(len(store.list_proposals()), 1)

            store.approve_scene(scene.document.id)
            drafted = service.draft_scene(scene.document.id)
            self.assertTrue(drafted.ok)
            self.assertIsNotNone(drafted.version)
            self.assertEqual(drafted.version.state, "candidate")
            self.assertEqual(store.get_document(scene.document.id).content, "")
            proposals = store.list_proposals()
            self.assertEqual(len(proposals), 2)
            self.assertEqual(proposals[0].source_version_id, drafted.version.id)


if __name__ == "__main__":
    unittest.main()
