from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lg_cli.project_store import ProjectStore
from lg_cli.story_review import chapter_quality_report, run_consistency_check


class StoryReviewTests(unittest.TestCase):
    def test_consistency_check_reports_without_writing_and_supersedes_old_runs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = ProjectStore(Path(raw))
            store.initialize(name="Review Test")
            store.add_fact(category="world", key="moon", value="silver", state="canonical")
            store.add_fact(category="world", key="moon", value="red", state="canonical")
            store.add_timeline_entry(
                label="First event",
                event="Lin enters the city.",
                sort_key="day-1",
                state="canonical",
            )
            store.add_timeline_entry(
                label="Conflicting event",
                event="Lin remains outside the city.",
                sort_key="day-1",
                state="canonical",
            )

            transient = run_consistency_check(store, persist=False)
            self.assertGreaterEqual(transient.errors, 1)
            self.assertTrue(any(issue.category == "timeline" for issue in transient.issues))
            self.assertEqual(store.list_review_issues(), [])

            first = run_consistency_check(store)
            second = run_consistency_check(store)
            self.assertEqual(len(first.issues), len(second.issues))
            self.assertEqual(len(store.list_review_issues()), len(second.issues))
            self.assertEqual(
                len(store.list_review_issues(status="superseded")),
                len(first.issues),
            )

    def test_chapter_quality_records_doubts_without_changing_text(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = ProjectStore(Path(raw))
            store.initialize(name="Quality Test")
            chapter = store.create_document(
                kind="chapter",
                slug="chapter-1",
                title="Chapter One",
                content="Lin waited by the gate.",
                state="accepted",
            )

            report = chapter_quality_report(store, chapter.id)
            self.assertGreaterEqual(len(report.issues), 1)
            self.assertEqual(store.get_document(chapter.id).content, "Lin waited by the gate.")
            self.assertEqual(len(store.list_review_issues(document=chapter.id)), len(report.issues))


if __name__ == "__main__":
    unittest.main()
