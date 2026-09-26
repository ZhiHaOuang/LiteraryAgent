from __future__ import annotations

import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

from lg_cli.book_analysis import (
    BookAnalysisStore,
    ChapterAnalysisService,
    resolve_evidence,
    response_schema,
    source_spans,
)
from lg_cli.core_adapter import CoreExecutionResult
from lg_cli.project_store import ProjectStore, ProjectStoreError

from .helpers import make_config


class BookAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.project = ProjectStore(self.root)
        self.project.initialize(name="Evidence book")
        self.doc = self.project.create_document(kind="chapter", slug="first", title="First",
                                                content="The gate was closed at dusk.")
        self.fact = self.project.add_fact(category="world", key="gate", value="The gate closes at dusk.", state="canonical")
        self.records = BookAnalysisStore(self.project)
        self.report = {"summary": "Gate rule observed", "entries": [{
            "key": "gate", "label": "Gate", "analysis": "Access is restricted at dusk.",
            "evidence": [{"document_id": self.doc.id, "version_id": self.doc.active_version_id,
                          "quote": "gate was closed at dusk"}],
        }], "findings": []}

    def test_source_changes_and_canon_changes_invalidate_reports(self):
        self.records.save(self.records.snapshot("first"), "Worldview", self.report, "run-1")
        self.assertEqual(self.records.list()[0]["status"], "current")
        self.assertIn("Gate rule observed", self.records.context())
        self.project.create_version("first", content="Changed prose", activate=True)
        self.assertEqual(self.records.list()[0]["status"], "stale")
        self.assertNotIn("Gate rule observed", self.records.context())
        self.project.accept_version("first", 1)
        self.records.save(self.records.snapshot("first"), "Worldview", self.report, "run-2")
        self.project.add_fact(category="world", key="other", value="A new rule", state="canonical")
        self.assertEqual(self.records.list()[0]["status"], "needs-canon-review")
        self.assertIn("Gate rule observed", self.records.context())
        self.assertIn("needs-canon-review", self.records.context())

    def test_invented_quote_wrong_version_and_unfounded_conflict_rejected(self):
        for field, value in (("quote", "Invented quotation"), ("version_id", -1)):
            report = copy.deepcopy(self.report)
            report["entries"][0]["evidence"][0][field] = value
            with self.assertRaises(ProjectStoreError):
                self.records.save(self.records.snapshot("first"), "Worldview", report, "bad")
        report = copy.deepcopy(self.report)
        report["findings"] = [{"kind": "canon_conflict", "explanation": "Alleged conflict",
            "evidence": report["entries"][0]["evidence"], "fact_id": self.fact.id, "fact_quote": "Invented rule"}]
        with self.assertRaises(ProjectStoreError):
            self.records.save(self.records.snapshot("first"), "Worldview", report, "bad")
        self.assertEqual(self.records.list(), [])

    def test_edit_during_model_call_does_not_publish_report(self):
        snapshot = self.records.snapshot("first")
        self.project.create_version("first", content="Author edited during analysis", activate=True)
        with self.assertRaises(ProjectStoreError):
            self.records.save(snapshot, "Worldview", self.report, "old")

    def test_service_uses_specialist_and_records_run(self):
        report = copy.deepcopy(self.report)
        report["entries"][0]["evidence"] = [{"span_id": next(iter(source_spans(self.records.snapshot("first"))))}]
        class Adapter:
            def run(self, **kwargs):
                self.kwargs = kwargs
                return CoreExecutionResult(True, False, json.dumps(report), None, "test", [], 0, "", "", 0.01)
        adapter = Adapter()
        config = replace(make_config(self.root, api_key="test-placeholder"), enable_reference=False)
        service = ChapterAnalysisService(config, adapter=adapter)
        events = []
        result = service.analyze("first", categories=["Worldview"], on_event=events.append)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.records.list()[0]["status"], "current")
        self.assertIn("stable rule", adapter.kwargs["prompt"])
        self.assertIn("Each evidence object contains ONLY span_id", adapter.kwargs["prompt"])
        self.assertIn("Every finding must include fact_id", adapter.kwargs["prompt"])
        self.assertIn("missing emphasis is not a contradiction", adapter.kwargs["prompt"])
        self.assertIn(json.dumps(response_schema(), ensure_ascii=False), adapter.kwargs["prompt"])
        self.assertEqual(service.runs.load(result["run_id"])["status"], "completed")
        started = next(event for event in events if event.type.value == "stage.started")
        self.assertEqual(started.data, {"index": 1, "total": 1, "agent": "worldbuilding"})

    def test_bad_model_output_fails_run_without_current_report(self):
        class Adapter:
            def run(self, **kwargs):
                return CoreExecutionResult(True, False, "not json", None, "test", [], 0, "", "", 0.01)
        service = ChapterAnalysisService(make_config(self.root, api_key="test-placeholder"), adapter=Adapter())
        with self.assertRaises(ProjectStoreError):
            service.analyze("first", categories=["Worldview"])
        self.assertEqual(self.records.list(), [])
        self.assertEqual(service.runs.list_runs(limit=1)[0]["status"], "failed")

    def test_independent_specialists_can_review_same_candidate_concurrently(self):
        candidate = self.project.create_version("first", content="The gate remained closed at dusk.")
        snapshot = self.records.candidate_snapshot("first", candidate)
        report = copy.deepcopy(self.report)
        report["entries"][0]["evidence"] = [{"span_id": next(iter(source_spans(snapshot)))}]
        barrier = Barrier(2)

        class Adapter:
            def run(self, **kwargs):
                barrier.wait(timeout=10)
                return CoreExecutionResult(True, False, json.dumps(report), None, "test", [], 0, "", "", 0.01)

        config = replace(make_config(self.root, api_key="test-placeholder"), enable_reference=False)

        def analyze(category):
            return ChapterAnalysisService(config, adapter=Adapter()).analyze(
                "first", categories=[category], candidate=candidate)

        categories = ["Worldview", "EventsLibrary"]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(analyze, categories))
        self.assertEqual(len({result["run_id"] for result in results}), 2)
        runs = ChapterAnalysisService(config).runs
        for category, result in zip(categories, results):
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["categories"], [category])
            self.assertEqual(runs.load(result["run_id"])["status"], "completed")
            path = self.records.root / "candidates" / str(candidate.id) / f"{category}.json"
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["run_id"], result["run_id"])
            self.assertEqual(saved["report"]["entries"][0]["evidence"][0]["version_id"], candidate.id)
        self.assertEqual(self.project.get_document("first").active_version_id, self.doc.active_version_id)
        self.assertEqual(self.records.list(), [])

    def test_invalid_evidence_gets_one_bounded_repair(self):
        valid = copy.deepcopy(self.report)
        valid["entries"][0]["evidence"] = [{"span_id": next(iter(source_spans(self.records.snapshot("first"))))}]
        invalid = copy.deepcopy(valid)
        invalid["entries"][0]["evidence"][0]["span_id"] = "invented-span"
        class Adapter:
            calls = 0

            def run(self, **kwargs):
                self.calls += 1
                output = invalid if self.calls == 1 else valid
                return CoreExecutionResult(True, False, json.dumps(output), None, "test", [], 0, "", "", 0.01)
        adapter = Adapter()
        service = ChapterAnalysisService(make_config(self.root, api_key="test-placeholder"), adapter=adapter)
        result = service.analyze("first", categories=["Worldview"])
        self.assertEqual(adapter.calls, 2)
        self.assertEqual(self.records.list()[0]["report"]["entries"][0]["evidence"][0]["quote"], self.doc.content)
        self.assertTrue((service.runs.handle(result["run_id"]).directory / "Worldview-rejected-1.json").exists())

    def test_source_spans_bind_exact_punctuation_and_version(self):
        snapshot = self.records.snapshot("first")
        snapshot["documents"][0]["content"] = 'She said: “Wait.”\nThen she left.'
        spans = source_spans(snapshot)
        response = copy.deepcopy(self.report)
        response["entries"][0]["evidence"] = [{"span_id": next(iter(spans))}]
        report = resolve_evidence(snapshot, response)
        self.assertEqual(report["entries"][0]["evidence"][0]["quote"], 'She said: “Wait.”')
        snapshot["documents"][0]["version_id"] += 1
        with self.assertRaises(ProjectStoreError):
            resolve_evidence(snapshot, response)
