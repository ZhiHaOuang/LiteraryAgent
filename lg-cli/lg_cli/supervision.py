from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timezone
from typing import Any

import jsonschema

from .book_analysis import (
    CATEGORIES,
    BookAnalysisStore,
    ChapterAnalysisService,
    fingerprint,
    validate_report,
)
from .config import LGConfig
from .events import EventSink
from .exporter import _atomic_write
from .project_store import (
    DocumentRecord,
    DocumentVersion,
    ProjectStore,
    ProjectStoreError,
)

DEFAULT_CATEGORIES = tuple(CATEGORIES)


class CanonConflictError(ProjectStoreError):
    """A current review requires an author discussion or manuscript correction."""


def _review_identity(report: dict[str, Any]) -> dict[str, Any]:
    # The base snapshot changes on publication; the reviewed snapshot does not.
    return {key: report[key] for key in (
        "project_id", "chapter", "library", "run_id", "fingerprint", "report",
    )}


def review_decisions(store: ProjectStore, report: dict[str, Any]) -> list[dict[str, Any]]:
    identity = _review_identity(report)
    if identity["project_id"] != store.project_info().project_id:
        raise ProjectStoreError("Adjudication belongs to another project.")
    digest = fingerprint(identity)
    records = BookAnalysisStore(store)
    directory = records.root / "adjudications" / digest
    if not directory.resolve().is_relative_to(store.workspace):
        raise ProjectStoreError("Adjudications must remain inside this book.")
    decisions = []
    for index, finding in enumerate(report["report"]["findings"], start=1):
        path = directory / f"{index}.json"
        if not path.exists() and records.legacy_root != records.root:
            path = records.legacy_root / "adjudications" / digest / f"{index}.json"
        if not path.exists():
            continue
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(store.workspace):
                raise ValueError("Symlinked decision")
            receipt = json.loads(path.read_text(encoding="utf-8"))
            if (receipt["schema"] != "lg.adjudication.v1" or receipt["report_digest"] != digest
                    or receipt["review_report"] != identity or receipt["finding_index"] != index
                    or receipt["decision"] != "dismiss_false_positive" or finding["kind"] != "canon_conflict"
                    or any(not isinstance(receipt.get(k), str) or not receipt[k].strip()
                           for k in ("reviewer", "reason", "created_at"))):
                raise ValueError("Invalid decision binding")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ProjectStoreError(f"Invalid adjudication receipt: {path}") from exc
        decisions.append({key: value for key, value in receipt.items() if key != "review_report"})
    return decisions


def unresolved_conflicts(store: ProjectStore, report: dict[str, Any]) -> list[dict[str, Any]]:
    dismissed = {item["finding_index"] for item in review_decisions(store, report)}
    return [finding for index, finding in enumerate(report["report"]["findings"], start=1)
            if finding["kind"] == "canon_conflict" and index not in dismissed]


def adjudicate_review(store: ProjectStore, reference: str, *, category: str, finding_index: int,
                      reviewer: str, reason: str, confirmed: bool = False,
                      version_number: int | None = None, volume: bool = False) -> dict[str, Any]:
    if not confirmed or not reviewer.strip() or not reason.strip():
        raise ProjectStoreError("Adjudication requires explicit confirmation, reviewer and reason.")
    if category not in CATEGORIES or type(finding_index) is not int or finding_index < 1:
        raise ProjectStoreError("Choose a known category and a one-based finding index.")
    if volume:
        if version_number is not None:
            raise ProjectStoreError("Volume adjudication does not accept a candidate version.")
        reports = [r for r in BookAnalysisStore(store).list(reference, volume=True)
                   if r["library"] == category and r["status"] == "current"]
    else:
        if version_number is None:
            raise ProjectStoreError("Candidate adjudication requires --version.")
        document = store.get_document(reference)
        version = store.get_version(reference, version_number)
        reports = candidate_reports(store, document, version, categories=[category], include_conflicts=True)
    if len(reports) != 1:
        raise ProjectStoreError("A current, source-validated review is required for adjudication.")
    report = reports[0]
    findings = report["report"]["findings"]
    if finding_index > len(findings) or findings[finding_index - 1]["kind"] != "canon_conflict":
        raise ProjectStoreError("Selected finding is not a Canonical conflict.")
    identity = _review_identity(report)
    digest = fingerprint(identity)
    directory = BookAnalysisStore(store).root / "adjudications" / digest
    if any(item["finding_index"] == finding_index for item in review_decisions(store, report)):
        raise ProjectStoreError("This finding already has an immutable adjudication receipt.")
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        path = directory / f"{finding_index}.json"
        if path.exists():
            raise ProjectStoreError("This finding already has an immutable adjudication receipt.")
        receipt = {"schema": "lg.adjudication.v1", "report_digest": digest,
                   "review_report": identity, "finding_index": finding_index,
                   "decision": "dismiss_false_positive", "reviewer": reviewer.strip(),
                   "reason": reason.strip(), "created_at": datetime.now(timezone.utc).isoformat()}
        _atomic_write(path, json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    finally:
        os.close(fd)
    return {"path": str(path), **{k: v for k, v in receipt.items() if k != "review_report"}}


def configure_supervision(store: ProjectStore) -> dict[str, Any]:
    payload = {"schema": "lg.supervision.v1", "project_id": store.project_info().project_id,
               "chapter_categories": list(DEFAULT_CATEGORIES), "volume_categories": list(CATEGORIES)}
    path = store.root / "supervision.json"
    if path.exists():
        return supervision_policy(store) or payload
    _atomic_write(path, json.dumps(payload, indent=2) + "\n")
    return payload


def supervision_policy(store: ProjectStore) -> dict[str, Any] | None:
    path = store.root / "supervision.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        categories = payload["chapter_categories"]
        if (payload["schema"] != "lg.supervision.v1" or payload["project_id"] != store.project_info().project_id
                or not isinstance(categories, list) or not categories
                or any(category not in CATEGORIES for category in categories)
                or payload["volume_categories"] != list(CATEGORIES)):
            raise ValueError("Invalid supervision policy")
        return payload
    except (ValueError, TypeError, KeyError) as exc:
        raise ProjectStoreError("Invalid project supervision policy; refusing unsupervised acceptance.") from exc


def chapter_for(store: ProjectStore, document: DocumentRecord) -> str | None:
    if document.kind == "chapter":
        return document.slug
    if document.kind == "scene" and document.parent_id is not None:
        return store.get_document(document.parent_id, kind="chapter").slug
    return None


def candidate_reports(store: ProjectStore, document: DocumentRecord, version: DocumentVersion,
                      *, final: bool = False, categories: list[str] | None = None,
                      include_conflicts: bool = False) -> list[dict[str, Any]]:
    policy = supervision_policy(store)
    if policy is None or document.kind not in {"chapter", "scene"}:
        return []
    chapter = chapter_for(store, document)
    if chapter is None:
        raise ProjectStoreError("Supervised scenes must belong to a chapter.")
    records = BookAnalysisStore(store)
    base = records.snapshot(chapter)
    snapshot = records.candidate_snapshot(chapter, version, final=final)
    reports = []
    selected = policy["chapter_categories"] if categories is None else categories
    if any(category not in policy["chapter_categories"] for category in selected):
        raise ProjectStoreError("Candidate review category is not in this book's supervision policy.")
    for category in selected:
        path = records._path(chapter, category, candidate_id=version.id)
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
            if (report["project_id"] != snapshot["project_id"] or report["chapter"] != chapter
                    or report["library"] != category or report["fingerprint"] != fingerprint(snapshot)
                    or report["base_fingerprint"] != fingerprint(base)):
                raise ValueError("Stale or wrong-target supervision")
            validate_report(snapshot, report["report"])
        except (OSError, ValueError, KeyError, TypeError, jsonschema.ValidationError) as exc:
            raise ProjectStoreError(f"Missing or stale {category} candidate review; rerun version accept through the CLI.") from exc
        conflicts = unresolved_conflicts(store, report)
        if conflicts and not include_conflicts:
            raise CanonConflictError(f"{category} found an evidenced canon conflict. Discuss or revise before accepting: "
                                     + conflicts[0]["explanation"] + f"\nReport: {path}")
        reports.append(report)
    return reports


def review_for_acceptance(config: LGConfig, store: ProjectStore, reference: str, version_number: int,
                          *, final: bool = False, on_event: EventSink | None = None) -> None:
    policy = supervision_policy(store)
    document = store.get_document(reference)
    if policy is None or document.kind not in {"chapter", "scene"}:
        return
    chapter = chapter_for(store, document)
    if chapter is None:
        raise ProjectStoreError("Supervised scenes must belong to a chapter.")
    version = store.get_version(reference, version_number)
    missing = []
    for category in policy["chapter_categories"]:
        try:
            candidate_reports(store, document, version, final=final, categories=[category])
        except CanonConflictError:
            raise
        except ProjectStoreError:
            missing.append(category)
    if missing:
        ChapterAnalysisService(config).analyze(chapter, categories=missing,
                                               candidate=version, final=final, on_event=on_event)
    candidate_reports(store, document, version, final=final)


def publish_accepted_reports(store: ProjectStore, chapter: str, reports: list[dict[str, Any]]) -> None:
    records = BookAnalysisStore(store)
    snapshot = records.snapshot(chapter)
    for report in reports:
        if report["fingerprint"] != fingerprint(snapshot):
            raise ProjectStoreError("Accepted text changed before analysis publication; reports remain candidate-only.")
        records.save(snapshot, report["library"], report["report"], report["run_id"])
