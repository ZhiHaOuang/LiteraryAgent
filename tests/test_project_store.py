from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from lg_cli.project_store import ProjectStore, ProjectStoreError


class ProjectStoreTests(unittest.TestCase):
    def test_context_includes_planned_threads_without_promoting_or_leaking_them(self):
        from lg_cli.story_context import render_story_context

        with tempfile.TemporaryDirectory() as raw:
            store = ProjectStore(Path(raw) / "book")
            store.initialize(name="Threads")
            planned = store.add_foreshadowing(title="Future payoff", setup_note="Not yet established", state="planned")
            opened = store.add_foreshadowing(title="Open thread", setup_note="Still unresolved")
            store.add_foreshadowing(title="Finished", setup_note="Already resolved", state="paid")
            store.add_foreshadowing(title="Discarded", setup_note="Not used", state="abandoned")
            other = ProjectStore(Path(raw) / "other")
            other.initialize(name="Other")
            other.add_foreshadowing(title="Foreign thread", setup_note="Other book")
            snapshot = store.context_snapshot()
            self.assertEqual(snapshot["foreshadowing"], [opened, planned])
            rendered = render_story_context(snapshot)
            self.assertIn("[planned] Future payoff", rendered)
            self.assertIn("[open] Open thread", rendered)
            self.assertIn("intentions, not established story events", rendered)
            self.assertNotIn("Foreign thread", rendered)
            self.assertEqual(store.list_foreshadowing(state="planned"), [planned])

    def test_context_thread_budget_prioritizes_open_and_stays_bounded(self):
        with tempfile.TemporaryDirectory() as raw:
            store = ProjectStore(Path(raw))
            store.initialize(name="Thread budget")
            for index in range(45):
                store.add_foreshadowing(title=f"Planned {index}", setup_note="Plan", state="planned")
            self.assertEqual(len(store.context_snapshot()["foreshadowing"]), 40)
            opened = store.add_foreshadowing(title="Open", setup_note="Current")
            items = store.context_snapshot()["foreshadowing"]
            self.assertEqual(len(items), 40)
            self.assertEqual(items[0], opened)
            for index in range(39):
                store.add_foreshadowing(title=f"Open {index}", setup_note="Current")
            self.assertTrue(all(item.state == "open" for item in store.context_snapshot()["foreshadowing"]))

    def test_timeline_update_preserves_identity_source_and_omitted_fields(self):
        with tempfile.TemporaryDirectory() as raw:
            store = ProjectStore(Path(raw))
            store.initialize(name="Timeline")
            document = store.create_document(kind="note", slug="source", title="Source")
            entry = store.add_timeline_entry(label="Day one", event="Arrival", sort_key="01",
                                             tags=["arrival"], source_document=document.id)
            updated = store.update_timeline_entry(entry.id, state="canonical", event="Arrival at noon")
            self.assertEqual((updated.id, updated.created_at), (entry.id, entry.created_at))
            self.assertEqual((updated.label, updated.sort_key, updated.tags, updated.source_document_id),
                             (entry.label, entry.sort_key, entry.tags, document.id))
            self.assertEqual((updated.state, updated.event), ("canonical", "Arrival at noon"))
            self.assertEqual(len(store.list_timeline()), 1)

    def test_invalid_timeline_updates_leave_entry_unchanged(self):
        with tempfile.TemporaryDirectory() as raw:
            store = ProjectStore(Path(raw))
            store.initialize(name="Timeline validation")
            entry = store.add_timeline_entry(label="Day one", event="Arrival")
            for changes in ({"state": "invalid"}, {"event": " "}, {"label": ""},
                            {"source_document": "missing"}):
                with self.subTest(changes=changes), self.assertRaises(ProjectStoreError):
                    store.update_timeline_entry(entry.id, **changes)
                self.assertEqual(store.list_timeline()[0], entry)
            with self.assertRaises(ProjectStoreError):
                store.update_timeline_entry(99999, state="canonical")

    def test_timeline_set_cli_confirms_existing_entry(self):
        from lg_cli.main import main

        with tempfile.TemporaryDirectory() as raw:
            store = ProjectStore(Path(raw))
            store.initialize(name="Timeline CLI")
            entry = store.add_timeline_entry(label="Day one", event="Arrival", tags=["kept"])
            output = io.StringIO()
            with redirect_stdout(output):
                result = main(["-C", raw, "timeline", "set", str(entry.id),
                               "--state", "canonical", "--order", "01-1200", "--json"])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue())["state"], "canonical")
            self.assertEqual(store.list_timeline()[0].tags, ("kept",))
            self.assertEqual(store.list_timeline()[0].sort_key, "01-1200")

    def test_previous_chapter_prose_is_prioritized_over_recent_designs(self):
        with tempfile.TemporaryDirectory() as raw:
            store = ProjectStore(Path(raw))
            store.initialize(name="Ordered context")
            chapters = [store.create_document(kind="chapter", slug=f"ch{i}", title=f"Chapter {i}",
                                              sequence=i) for i in range(1, 4)]
            previous = store.create_scene(slug="previous", title="Previous", chapter=chapters[0].id, sequence=1)
            version = store.create_version(previous.document.id, content="Accepted previous chapter", state="candidate")
            store.accept_version(previous.document.id, version.version_number)
            current = store.create_scene(slug="current", title="Current", chapter=chapters[1].id, sequence=1)
            for index in range(12):
                store.create_document(kind="note", slug=f"note-{index}", title="Recent design", content="Design")
            context = store.context_snapshot(scene=current.document.id, max_documents=2)
            self.assertEqual(context["documents"][0].id, previous.document.id)
            self.assertEqual(context["documents"][0].content, "Accepted previous chapter")

    def test_scene_context_keeps_siblings_and_unmatched_canon_and_documents(self):
        with tempfile.TemporaryDirectory() as raw:
            store = ProjectStore(Path(raw))
            store.initialize(name="Context")
            fact = store.set_fact(category="rule", key="one", value="No time travel.")
            outline = store.create_document(kind="outline", slug="outline", title="Outline", content="Ending")
            chapter = store.create_document(kind="chapter", slug="ch1", title="Chapter")
            previous = store.create_scene(slug="scene1", title="Before", chapter=chapter.id, sequence=1)
            current = store.create_scene(slug="scene2", title="After", chapter=chapter.id, sequence=2)
            for index in range(12):
                store.create_document(kind="chapter", slug=f"empty-{index}", title="Empty chapter")
            context = store.context_snapshot(scene=current.document.id, query="unmatched-language")
            self.assertIn(fact.id, [item.id for item in context["canonical_facts"]])
            self.assertIn(outline.id, [item.id for item in context["documents"]])
            self.assertIn(previous.document.id, [item.id for item in context["documents"]])
            self.assertEqual(len({item.id for item in context["documents"]}), len(context["documents"]))

    def test_versions_use_document_version_numbers_and_candidates_are_inactive(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = ProjectStore(Path(raw))
            store.initialize(name="Version Test")
            store.create_document(kind="note", slug="offset-1", title="Offset 1")
            store.create_document(kind="note", slug="offset-2", title="Offset 2")
            document = store.create_document(
                kind="chapter",
                slug="chapter-1",
                title="Chapter 1",
                content="accepted beginning",
                state="accepted",
            )
            second = store.create_version(
                document.id,
                content="candidate two",
                state="candidate",
            )
            third = store.create_version(
                document.id,
                content="candidate three",
                state="candidate",
            )

            self.assertEqual(store.get_version(document.id, 3).id, third.id)
            self.assertEqual(store.get_document(document.id).content, "accepted beginning")

            accepted = store.accept_version(document.id, second.version_number)
            self.assertEqual(accepted.content, "candidate two")
            self.assertEqual(accepted.active_version_number, 2)

    def test_fact_upsert_and_proposal_acceptance_are_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = ProjectStore(Path(raw))
            store.initialize(name="Bible Test")
            document = store.create_document(
                kind="world",
                slug="moon",
                title="Moon",
                content="The moon is pale.",
                state="accepted",
            )
            version = store.create_version(
                document.id,
                content="The moon is red.",
                state="candidate",
            )

            first = store.set_fact(category="world", key="moon-color", value="pale")
            updated = store.set_fact(category="world", key="moon-color", value="silver")
            self.assertEqual(first.id, updated.id)
            self.assertEqual(updated.value, "silver")
            self.assertEqual(len(store.fact_history(updated.id)), 2)

            proposal = store.propose_fact(
                category="world",
                key="candidate-moon-color",
                value="red",
                source_document=document.id,
                source_version=version.version_number,
            )
            accepted = store.accept_proposal(proposal.id)
            self.assertEqual(accepted.source_version_id, version.id)
            self.assertEqual(store.list_proposals(status="accepted")[0].id, proposal.id)


if __name__ == "__main__":
    unittest.main()
