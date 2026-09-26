from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lg_cli.book_analysis import BookAnalysisStore
from lg_cli.init_project import init_workspace
from lg_cli.project_store import ProjectStore, ProjectStoreError
from lg_cli.supervision import (
    DEFAULT_CATEGORIES,
    adjudicate_review,
    configure_supervision,
    review_for_acceptance,
    supervision_policy,
    unresolved_conflicts,
)

from tests.helpers import make_config


class SupervisionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = ProjectStore(self.root)
        self.store.initialize(name="Supervised book")
        self.doc = self.store.create_document(kind="chapter", slug="first", title="First", content="Old prose.")
        self.version = self.store.create_version("first", content="New prose in the city.")
        self.records = BookAnalysisStore(self.store)
        self.fact = self.store.add_fact(category="world", key="city", value="The city is empty.", state="canonical")
        configure_supervision(self.store)
        self.report = {"summary": "Source checked", "entries": [], "findings": []}

    def save_reviews(self, *, conflict=False, style=False):
        base = self.records.snapshot("first")
        snapshot = self.records.candidate_snapshot("first", self.version)
        for category in DEFAULT_CATEGORIES:
            report = copy.deepcopy(self.report)
            if conflict or style:
                report["findings"] = [{"kind": "canon_conflict" if conflict else "style",
                    "explanation": "Needs discussion" if conflict else "Consider a shorter sentence",
                    "evidence": [{"document_id": self.doc.id, "version_id": self.version.id, "quote": "New prose"}],
                    "fact_id": self.fact.id if conflict else None, "fact_quote": self.fact.value if conflict else ""}]
            self.records.save(snapshot, category, report, "review-run", candidate_id=self.version.id, base_snapshot=base)

    def test_missing_reviews_and_conflicts_prevent_activation(self):
        with self.assertRaises(ProjectStoreError):
            self.store.accept_version("first", self.version.version_number)
        self.save_reviews(conflict=True)
        with self.assertRaisesRegex(ProjectStoreError, "canon conflict"):
            self.store.accept_version("first", self.version.version_number)
        self.assertEqual(self.store.get_document("first").content, "Old prose.")

    def test_style_does_not_block_and_reviews_publish_only_after_acceptance(self):
        self.save_reviews(style=True)
        self.assertEqual(self.records.list(), [])
        self.store.accept_version("first", self.version.version_number)
        self.assertEqual(self.store.get_document("first").content, self.version.content)
        self.assertEqual(len(self.records.list()), len(DEFAULT_CATEGORIES))
        self.assertTrue(all(record["status"] == "current" for record in self.records.list()))

    def test_canon_change_invalidates_candidate_approval(self):
        self.save_reviews()
        self.store.add_fact(category="world", key="gate", value="Gate closed", state="canonical")
        with self.assertRaises(ProjectStoreError):
            self.store.accept_version("first", self.version.version_number)
        self.assertEqual(self.store.get_document("first").content, "Old prose.")

    def test_candidate_analysis_does_not_replace_active_analysis(self):
        self.records.save(self.records.snapshot("first"), "Worldview", self.report, "old-review")
        self.save_reviews()
        self.assertEqual(self.records.list()[0]["run_id"], "old-review")

    def test_existing_conflict_does_not_spend_more_model_calls(self):
        self.save_reviews(conflict=True)
        with patch("lg_cli.supervision.ChapterAnalysisService") as service:
            with self.assertRaisesRegex(ProjectStoreError, "canon conflict"):
                review_for_acceptance(make_config(self.root), self.store, "first", self.version.version_number)
            service.assert_not_called()

    def test_valid_candidate_reviews_are_reused_without_model_calls(self):
        self.save_reviews()
        with patch("lg_cli.supervision.ChapterAnalysisService") as service:
            review_for_acceptance(make_config(self.root), self.store, "first", self.version.version_number)
            service.assert_not_called()

    def dismiss(self, category):
        return adjudicate_review(self.store, "first", category=category, finding_index=1,
            reviewer="author-delegate", reason="Compared the quoted source and Canonical fact.",
            confirmed=True, version_number=self.version.version_number)

    def test_adjudication_preserves_reports_and_does_not_waive_other_categories(self):
        self.save_reviews(conflict=True)
        paths = list((BookAnalysisStore(self.store).root / "candidates" / str(self.version.id)).glob("*.json"))
        originals = {p: p.read_bytes() for p in paths}
        self.dismiss(DEFAULT_CATEGORIES[0])
        with self.assertRaisesRegex(ProjectStoreError, "canon conflict"):
            self.store.accept_version("first", self.version.version_number)
        for category in DEFAULT_CATEGORIES[1:]:
            self.dismiss(category)
        with patch("lg_cli.supervision.ChapterAnalysisService") as service:
            review_for_acceptance(make_config(self.root), self.store, "first", self.version.version_number)
            self.store.accept_version("first", self.version.version_number)
            service.assert_not_called()
        self.assertEqual(originals, {p: p.read_bytes() for p in paths})
        for record in self.records.list():
            self.assertEqual(record["report"]["findings"][0]["kind"], "canon_conflict")
            self.assertEqual(record["adjudications"][0]["reviewer"], "author-delegate")
            self.assertEqual(unresolved_conflicts(self.store, record), [])
        self.assertIn("author-delegate", self.records.context())

    def test_adjudication_requires_confirmation_reason_and_current_review(self):
        self.save_reviews(conflict=True)
        for kwargs in ({"confirmed": False}, {"reason": " "}, {"reviewer": " "}, {"finding_index": 0}):
            values = dict(category="Worldview", finding_index=1, reviewer="Author", reason="Evidence checked",
                          confirmed=True, version_number=self.version.version_number)
            values.update(kwargs)
            with self.assertRaises(ProjectStoreError):
                adjudicate_review(self.store, "first", **values)
        self.store.add_fact(category="world", key="new", value="New Canonical rule", state="canonical")
        with self.assertRaises(ProjectStoreError):
            self.dismiss("Worldview")

    def test_new_report_and_new_canon_invalidate_old_adjudications(self):
        self.save_reviews(conflict=True)
        for category in DEFAULT_CATEGORIES:
            self.dismiss(category)
        path = BookAnalysisStore(self.store).root / "candidates" / str(self.version.id) / "Worldview.json"
        original = path.read_text()
        value = json.loads(original)
        value["run_id"] = "a-new-review"
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ProjectStoreError, "canon conflict"):
            self.store.accept_version("first", self.version.version_number)
        path.write_text(original)
        self.store.add_fact(category="world", key="new", value="New Canonical rule", state="canonical")
        with self.assertRaises(ProjectStoreError):
            self.store.accept_version("first", self.version.version_number)

    def test_adjudication_is_per_finding_and_receipts_fail_closed(self):
        self.save_reviews(conflict=True)
        path = BookAnalysisStore(self.store).root / "candidates" / str(self.version.id) / "Worldview.json"
        value = json.loads(path.read_text())
        value["report"]["findings"].append(copy.deepcopy(value["report"]["findings"][0]))
        path.write_text(json.dumps(value))
        receipt = self.dismiss("Worldview")
        self.assertEqual(len(unresolved_conflicts(self.store, value)), 1)
        with self.assertRaisesRegex(ProjectStoreError, "immutable"):
            self.dismiss("Worldview")
        receipt_path = Path(receipt["path"])
        bad = json.loads(receipt_path.read_text())
        bad["review_report"]["project_id"] = "another-book"
        receipt_path.write_text(json.dumps(bad))
        with self.assertRaisesRegex(ProjectStoreError, "Invalid adjudication"):
            unresolved_conflicts(self.store, value)

    def test_volume_adjudication_is_visible_and_source_changes_invalidate_it(self):
        self.save_reviews(conflict=True)
        candidate = BookAnalysisStore(self.store).root / "candidates" / str(self.version.id) / "Worldview.json"
        report = json.loads(candidate.read_text())["report"]
        report["findings"][0]["evidence"] = [{"document_id": self.doc.id,
            "version_id": self.doc.active_version_id, "quote": "Old prose"}]
        self.records.save(self.records.volume_snapshot(), "Worldview", report, "volume-run")
        adjudicate_review(self.store, "__book__", category="Worldview", finding_index=1,
            reviewer="Author", reason="Direct source comparison", confirmed=True, volume=True)
        record = self.records.list(volume=True)[0]
        self.assertEqual(unresolved_conflicts(self.store, record), [])
        self.store.create_document(kind="chapter", slug="second", title="Second", content="Other prose")
        with self.assertRaisesRegex(ProjectStoreError, "current"):
            adjudicate_review(self.store, "__book__", category="Worldview", finding_index=1,
                reviewer="Author", reason="Direct source comparison", confirmed=True, volume=True)

    def test_volume_report_becomes_stale_when_another_chapter_is_added(self):
        snapshot = self.records.volume_snapshot()
        self.records.save(snapshot, "Worldview", self.report, "volume-review")
        self.assertEqual(self.records.list(volume=True)[0]["status"], "current")
        self.store.create_document(kind="chapter", slug="second", title="Second", content="Other text")
        self.assertEqual(self.records.list(volume=True)[0]["status"], "stale")

    def test_new_workspace_enables_policy_but_existing_workspace_is_not_migrated(self):
        new = self.root / "new"
        init_workspace(new)
        self.assertIsNotNone(supervision_policy(ProjectStore(new)))
        old = self.root / "old"
        ProjectStore(old).initialize(name="Old")
        init_workspace(old)
        self.assertIsNone(supervision_policy(ProjectStore(old)))
