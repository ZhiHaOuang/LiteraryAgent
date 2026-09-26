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
    def __init__(self, *, revision_text="Revised candidate.", card_updates=None) -> None:
        self.calls: list[str] = []
        self.prompts: list[str] = []
        self.revision_text = revision_text
        self.card_updates = card_updates if card_updates is not None else {"location": "North Gate"}

    def run(self, *, prompt, config, mode, model_profile=None, output_schema=None, on_event=None):
        schema = Path(output_schema).name
        self.calls.append(schema)
        self.prompts.append(prompt)
        if schema == "memory-proposals.schema.json":
            payload = {"summary": "Extracted one rule.", "memory_proposals": [{
                "category": "world", "key": "gate-curfew", "value": "The gate closes at dusk.",
                "tags": ["gate"], "rationale": "The source explicitly states the closing time."
            }], "risks": []}
        elif schema == "revision.schema.json":
            payload = {"summary": "Revised.", "revision_markdown": self.revision_text,
                       "change_log": ["Applied request."], "memory_proposals": [], "risks": []}
        elif schema == "scene-plan.schema.json":
            payload = {
                "summary": "A concrete plan is ready.",
                "plan_markdown": "# Plan\n\n1. Lin enters.\n2. The guard refuses him.",
                "card_updates": self.card_updates,
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
    def test_revision_rejects_another_context_document_without_saving(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = ProjectStore(root)
            store.initialize(name="Wrong revision target")
            store.create_document(kind="character", slug="cast", title="Cast", content="Character reference")
            unrelated = "A completely different manuscript chapter, not a character reference. " * 4
            store.create_document(kind="chapter", slug="ending", title="Ending", content=unrelated)
            adapter = StoryAdapter(revision_text=unrelated)
            result = StoryWorkflowService(make_config(root), project=store, adapter=adapter).revise_document(
                "cast", mode="light")
            self.assertFalse(result.ok)
            self.assertIn("unrelated context document 'ending'", result.error)
            self.assertEqual(len(store.list_versions("cast")), 1)
            self.assertEqual(store.get_document("cast").content, "Character reference")
            self.assertEqual(store.list_proposals(), [])

    def test_revision_allows_unchanged_source_shared_by_another_document(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = ProjectStore(root)
            store.initialize(name="Shared source")
            content = "This source is intentionally shared by two project documents. " * 4
            store.create_document(kind="note", slug="source", title="Source", content=content)
            store.create_document(kind="note", slug="reference", title="Reference", content=content)
            adapter = StoryAdapter(revision_text=content)
            result = StoryWorkflowService(make_config(root), project=store, adapter=adapter).revise_document(
                "source", mode="light")
            self.assertTrue(result.ok, result.error)

    def test_passage_revision_rejects_copy_beyond_short_adjacent_paragraph(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = ProjectStore(root)
            store.initialize(name="Unselected context copy")
            later = "This later unchanged paragraph is longer than forty characters and is not selected."
            store.create_document(kind="chapter", slug="chapter", title="Chapter",
                                  content="SELECTED\n\nA clock ticks.\n\n" + later)
            adapter = StoryAdapter(revision_text="A clock ticks.\n\n" + later)
            result = StoryWorkflowService(make_config(root), project=store, adapter=adapter).revise_document(
                "chapter", mode="light", passage="SELECTED")
            self.assertFalse(result.ok)
            self.assertIn("outside the selection", result.error)
            self.assertEqual(len(store.list_versions("chapter")), 1)

    def test_scene_revision_keeps_explicit_narrative_constraints(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = ProjectStore(root)
            store.initialize(name="Revision viewpoint")
            scene = store.create_scene(slug="gate", title="Gate", pov="First-person Lin",
                                       narrative_tense="past", time_label="Day 2 evening")
            store.create_version(scene.document.id, content="Before\nSELECTED\nAfter")
            adapter = StoryAdapter()
            result = StoryWorkflowService(make_config(root), project=store, adapter=adapter).revise_document(
                scene.document.id, mode="light", source_version=2, passage="SELECTED")
            self.assertTrue(result.ok, result.error)
            self.assertIn("POV: First-person Lin", adapter.prompts[0])
            self.assertIn("Narrative tense: past", adapter.prompts[0])
            self.assertIn("Scene time: Day 2 evening", adapter.prompts[0])
            self.assertEqual(store.get_document(scene.document.id).content, "")

    def test_planner_preserves_card_edits_made_during_generation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = ProjectStore(root)
            store.initialize(name="Concurrent scene edit")
            scene = store.create_scene(slug="ending", title="Ending")

            class EditingAdapter(StoryAdapter):
                def run(self, **kwargs):
                    store.update_scene_card(scene.document.id, location="Author location")
                    return super().run(**kwargs)

            result = StoryWorkflowService(make_config(root), project=store, adapter=EditingAdapter()).plan_scene(scene.document.id)
            self.assertTrue(result.ok, result.error)
            self.assertEqual(store.get_scene(scene.document.id).location, "Author location")

    def test_planner_fills_blanks_without_overwriting_author_constraints(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = ProjectStore(root)
            store.initialize(name="Protected scene card")
            scene = store.create_scene(slug="ending", title="Ending", goal="An earned ending",
                                       characters=["Lin"], word_min=2400)
            adapter = StoryAdapter(card_updates={"goal": "Different ending", "characters": [],
                                                "word_min": 500, "word_max": 600, "location": "North Gate"})
            result = StoryWorkflowService(make_config(root), project=store, adapter=adapter).plan_scene(scene.document.id)
            self.assertTrue(result.ok, result.error)
            updated = store.get_scene(scene.document.id)
            self.assertEqual(updated.goal, "An earned ending")
            self.assertEqual(updated.characters, scene.characters)
            self.assertEqual((updated.word_min, updated.word_max), (2400, None))
            self.assertEqual(updated.location, "North Gate")
            self.assertIn("Preserved existing scene card fields", result.text)
            self.assertIn("literary scene set", result.text)

    def test_passage_revision_rejects_copied_adjacent_prose(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = ProjectStore(root)
            store.initialize(name="Boundary validation")
            neighbor = "This neighboring paragraph must remain outside the selected passage."
            store.create_document(kind="chapter", slug="chapter", title="Chapter",
                                  content="SELECTED\n\n" + neighbor)
            adapter = StoryAdapter(revision_text="New sentence.\n\n" + neighbor)
            result = StoryWorkflowService(make_config(root), project=store, adapter=adapter).revise_document(
                "chapter", mode="light", passage="SELECTED")
            self.assertFalse(result.ok)
            self.assertIn("unchanged paragraph", result.error)
            self.assertEqual(len(store.list_versions("chapter")), 1)

    def test_passage_revision_preserves_unselected_candidate_text(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = ProjectStore(root)
            store.initialize(name="Passage revision")
            document = store.create_document(kind="chapter", slug="chapter", title="Chapter", content="Old active text")
            store.create_version(document.id, content="Prefix\nSELECTED\nSuffix")
            adapter = StoryAdapter()
            result = StoryWorkflowService(make_config(root), project=store, adapter=adapter).revise_document(
                "chapter", mode="light", source_version=2, passage="SELECTED")
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.version.content, "Prefix\nRevised candidate.\nSuffix")
            self.assertEqual(result.artifact_path.read_text().strip(), result.version.content)
            self.assertEqual(result.version.metadata["source_version"], 2)
            self.assertEqual(result.version.metadata["passage"], "SELECTED")
            self.assertEqual(store.get_document("chapter").content, "Old active text")
            self.assertIn("only the replacement passage", adapter.prompts[0])
            self.assertLess(adapter.prompts[0].index("## Assigned Skill:"),
                            adapter.prompts[0].index("## Author Request"))
            self.assertLess(adapter.prompts[0].index("## Project Writing Instructions"),
                            adapter.prompts[0].index("only the replacement passage"))
            self.assertNotIn("[SELECTED PASSAGE]", adapter.prompts[0])
            self.assertIn("<selected_passage>\nSELECTED\n</selected_passage>", adapter.prompts[0])
            self.assertLess(adapter.prompts[0].index("</unchanged_after>"),
                            adapter.prompts[0].index("<selected_passage>"))

    def test_passage_revision_rejects_empty_missing_and_ambiguous_matches(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = ProjectStore(root)
            store.initialize(name="Passage validation")
            store.create_document(kind="chapter", slug="chapter", title="Chapter", content="same same aaa")
            adapter = StoryAdapter()
            service = StoryWorkflowService(make_config(root), project=store, adapter=adapter)
            for passage in ("", " ", "missing", "same", "aa"):
                with self.subTest(passage=passage):
                    result = service.revise_document("chapter", mode="light", passage=passage)
                    self.assertFalse(result.ok)
                    self.assertIn("exactly once", result.error)
            self.assertEqual(adapter.calls, [])
            self.assertEqual(len(store.list_versions("chapter")), 1)

    def test_revision_of_candidate_does_not_inject_old_active_text(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = ProjectStore(root)
            store.initialize(name="Revision context")
            source = store.create_document(kind="world", slug="world", title="World",
                                           content="STALE ACTIVE VERSION")
            store.create_version(source.id, content="SELECTED CANDIDATE VERSION")
            store.create_document(kind="character", slug="cast", title="Cast", content="OTHER PROJECT CONTEXT")
            adapter = StoryAdapter()
            result = StoryWorkflowService(make_config(root), project=store, adapter=adapter).revise_document(
                "world", mode="light", source_version=2)
            self.assertTrue(result.ok, result.error)
            self.assertIn("SELECTED CANDIDATE VERSION", adapter.prompts[-1])
            self.assertIn("OTHER PROJECT CONTEXT", adapter.prompts[-1])
            self.assertNotIn("STALE ACTIVE VERSION", adapter.prompts[-1])
            self.assertGreater(adapter.prompts[-1].index("## Author Request"),
                               adapter.prompts[-1].index("OTHER PROJECT CONTEXT"))
            self.assertIn("not only the summary or change_log", adapter.prompts[-1])
            self.assertEqual(store.get_document("world").active_version_number, 1)
            self.assertEqual(result.version.metadata["source_version"], 2)

    def test_bible_curation_loads_skill_and_creates_only_source_linked_proposals(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = ProjectStore(root)
            store.initialize(name="Bible")
            document = store.create_document(kind="world", slug="world", title="World",
                                             content="The gate closes at dusk.")
            adapter = StoryAdapter()
            service = StoryWorkflowService(make_config(root), project=store, adapter=adapter)
            planned = service.curate_document("world", dry_run=True)
            self.assertIn("## Assigned Skill: story-bible-curator", planned.text)
            self.assertEqual(adapter.calls, [])
            result = service.curate_document("world")
            self.assertTrue(result.ok, result.error)
            self.assertIn("## Assigned Skill: story-bible-curator", adapter.prompts[-1])
            self.assertEqual(store.list_facts(state="canonical"), [])
            proposals = store.list_proposals()
            self.assertEqual(len(proposals), 1)
            self.assertEqual(proposals[0].source_document_id, document.id)
            self.assertEqual(proposals[0].source_version_id, document.active_version_id)

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
            self.assertIn("## Assigned Skill: scene-planner", adapter.prompts[-1])
            self.assertIn("## Assigned Subagent: scene-planner", adapter.prompts[-1])
            self.assertIn("length target is for later prose", adapter.prompts[-1])
            self.assertGreater(adapter.prompts[-1].index("## Author Request"),
                               adapter.prompts[-1].index("# Project Context"))
            self.assertIn("An existing plan is a draft to revise", adapter.prompts[-1])
            self.assertIn(
                "The only top-level JSON keys are summary, plan_markdown, card_updates, memory_proposals, risks.",
                adapter.prompts[-1],
            )
            self.assertIn("Map the skill's scene_plan output to plan_markdown", adapter.prompts[-1])
            self.assertEqual(store.get_scene(scene.document.id).plan_state, "candidate")
            self.assertEqual(store.get_document(scene.document.id).content, "")
            self.assertEqual(len(store.list_proposals()), 1)

            store.approve_scene(scene.document.id)
            drafted = service.draft_scene(scene.document.id)
            self.assertTrue(drafted.ok)
            self.assertIn("## Assigned Skill: chapter-writer", adapter.prompts[-1])
            self.assertGreater(adapter.prompts[-1].index("## Author Request"),
                               adapter.prompts[-1].index("## Approved Scene Card"))
            self.assertIn("do not copy them into narration or character dialogue", adapter.prompts[-1])
            self.assertIsNotNone(drafted.version)
            self.assertEqual(drafted.version.state, "candidate")
            self.assertEqual(store.get_document(scene.document.id).content, "")
            proposals = store.list_proposals()
            self.assertEqual(len(proposals), 2)
            self.assertEqual(proposals[0].source_version_id, drafted.version.id)


if __name__ == "__main__":
    unittest.main()
