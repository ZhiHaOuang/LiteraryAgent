from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


PROJECT_SCHEMA = "lg.story-project.v1"
DATABASE_SCHEMA = "lg.story-database.v1"

DOCUMENT_KINDS = {
    "book",
    "part",
    "chapter",
    "scene",
    "outline",
    "world",
    "character",
    "timeline",
    "style",
    "note",
    "research",
}
DOCUMENT_STATES = {"draft", "candidate", "accepted", "final", "archived", "rejected"}
FACT_STATES = {"idea", "canonical", "archived", "rejected"}
SCENE_STATES = {"planned", "approved", "drafted", "reviewed", "accepted", "final", "archived"}


class ProjectStoreError(ValueError):
    """Raised when an LG story project cannot satisfy a requested operation."""


@dataclass(frozen=True)
class ProjectInfo:
    project_id: str
    name: str
    workspace: Path
    created_at: str
    updated_at: str
    schema_version: str = PROJECT_SCHEMA


@dataclass(frozen=True)
class DocumentRecord:
    id: int
    kind: str
    slug: str
    title: str
    state: str
    parent_id: int | None
    sequence: int | None
    active_version_id: int | None
    active_version_number: int | None
    content: str
    tags: tuple[str, ...]
    metadata: dict[str, Any]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class DocumentVersion:
    id: int
    document_id: int
    version_number: int
    state: str
    content: str
    reason: str
    metadata: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class StoryFact:
    id: int
    category: str
    key: str
    value: str
    state: str
    tags: tuple[str, ...]
    source_document_id: int | None
    source_version_id: int | None
    rationale: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class FactProposal:
    id: int
    category: str
    key: str
    value: str
    tags: tuple[str, ...]
    source_document_id: int | None
    source_version_id: int | None
    status: str
    rationale: str
    created_at: str
    resolved_at: str | None


@dataclass(frozen=True)
class SceneCard:
    document: DocumentRecord
    chapter_id: int | None
    status: str
    pov: str
    narrative_tense: str
    time_label: str
    location: str
    goal: str
    characters: tuple[str, ...]
    conflict: str
    required_information: str
    emotional_change: str
    end_state: str
    word_min: int | None
    word_max: int | None
    plan: str
    plan_state: str
    metadata: dict[str, Any]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TimelineEntry:
    id: int
    label: str
    sort_key: str
    event: str
    state: str
    tags: tuple[str, ...]
    source_document_id: int | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Foreshadowing:
    id: int
    title: str
    setup_note: str
    setup_document_id: int | None
    payoff_note: str
    payoff_document_id: int | None
    state: str
    tags: tuple[str, ...]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ReviewIssue:
    id: int
    document_id: int | None
    severity: str
    category: str
    message: str
    evidence: str
    suggested_actions: tuple[str, ...]
    status: str
    created_at: str


class ProjectRegistry:
    """Small, opt-in registry for switching between independent story workspaces."""

    def __init__(self, root: Path | None = None) -> None:
        configured = os.environ.get("LITERARYGIANT_REGISTRY_HOME", "").strip()
        selected_root = root or (Path(configured).expanduser() if configured else Path.home() / ".literarygiant")
        self.root = selected_root.resolve()
        self.path = self.root / "projects.json"

    def register(self, project: ProjectInfo) -> None:
        payload = self._read()
        records = [item for item in payload.get("projects", []) if item.get("project_id") != project.project_id]
        records.append(
            {
                "project_id": project.project_id,
                "name": project.name,
                "workspace": str(project.workspace),
                "created_at": project.created_at,
                "updated_at": project.updated_at,
            }
        )
        records.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
        self._write({"schema_version": "lg.project-registry.v1", "projects": records})

    def list(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for item in self._read().get("projects", []):
            if not isinstance(item, dict):
                continue
            workspace = Path(str(item.get("workspace") or ""))
            records.append(
                {
                    **item,
                    "workspace_exists": workspace.is_dir(),
                    "initialized": (workspace / ".literarygiant" / "project.json").is_file(),
                }
            )
        return records

    def remove(self, project_id: str) -> bool:
        payload = self._read()
        before = list(payload.get("projects", []))
        after = [item for item in before if item.get("project_id") != project_id]
        if len(after) == len(before):
            return False
        self._write({"schema_version": "lg.project-registry.v1", "projects": after})
        return True

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"schema_version": "lg.project-registry.v1", "projects": []}
        return value if isinstance(value, dict) else {"schema_version": "lg.project-registry.v1", "projects": []}

    def _write(self, payload: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(self.path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


class ProjectStore:
    """Persistent, local-first model of a long-form fiction project.

    The database holds structured state and revision history. Text remains stored as
    versions so it can be inspected, compared, accepted, rejected, or restored.
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.root = self.workspace / ".literarygiant"
        self.project_path = self.root / "project.json"
        self.database_path = self.root / "story.sqlite3"

    @property
    def initialized(self) -> bool:
        return self.project_path.is_file() and self.database_path.is_file()

    def initialize(self, *, name: str | None = None) -> ProjectInfo:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = self._read_project_payload()
        now = _utc_now()
        if not payload:
            payload = {
                "schema_version": PROJECT_SCHEMA,
                "project_id": uuid.uuid4().hex,
                "name": name or self.workspace.name or "Untitled Novel",
                "workspace": str(self.workspace),
                "created_at": now,
                "updated_at": now,
            }
        else:
            if name:
                payload["name"] = name
            payload["schema_version"] = PROJECT_SCHEMA
            payload["workspace"] = str(self.workspace)
            payload["updated_at"] = now
        _atomic_write_text(self.project_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

        with self._connection() as connection:
            self._create_schema(connection)
            self._set_meta(connection, "schema_version", DATABASE_SCHEMA)
            self._set_meta(connection, "project_id", str(payload["project_id"]))
            self._set_meta(connection, "project_name", str(payload["name"]))
            self._set_meta(connection, "workspace", str(self.workspace))
        return self.project_info()

    def project_info(self) -> ProjectInfo:
        payload = self._read_project_payload()
        if not payload:
            raise ProjectStoreError(f"No LiteraryGiant project exists at {self.workspace}. Run `literary init`.")
        return ProjectInfo(
            project_id=str(payload.get("project_id") or ""),
            name=str(payload.get("name") or self.workspace.name),
            workspace=self.workspace,
            created_at=str(payload.get("created_at") or ""),
            updated_at=str(payload.get("updated_at") or ""),
            schema_version=str(payload.get("schema_version") or PROJECT_SCHEMA),
        )

    def summary(self) -> dict[str, Any]:
        self._require_initialized()
        project = self.project_info()
        with self._connection() as connection:
            kind_rows = connection.execute(
                "SELECT kind, COUNT(*) AS count FROM documents GROUP BY kind"
            ).fetchall()
            document_kinds = {str(row["kind"]): int(row["count"]) for row in kind_rows}

            def count(query: str, parameters: tuple[Any, ...] = ()) -> int:
                row = connection.execute(query, parameters).fetchone()
                return int(row["count"]) if row is not None else 0

            return {
                "id": project.project_id,
                "name": project.name,
                "documents": sum(document_kinds.values()),
                "chapters": document_kinds.get("chapter", 0),
                "scenes": document_kinds.get("scene", 0),
                "candidate_versions": count(
                    "SELECT COUNT(*) AS count FROM document_versions WHERE state = 'candidate'"
                ),
                "candidate_plans": count(
                    "SELECT COUNT(*) AS count FROM scene_cards WHERE plan_state = 'candidate'"
                ),
                "canonical_facts": count(
                    "SELECT COUNT(*) AS count FROM story_facts WHERE state = 'canonical'"
                ),
                "ideas": count("SELECT COUNT(*) AS count FROM story_facts WHERE state = 'idea'"),
                "pending_proposals": count(
                    "SELECT COUNT(*) AS count FROM fact_proposals WHERE status = 'proposed'"
                ),
                "open_foreshadowing": count(
                    "SELECT COUNT(*) AS count FROM foreshadowing WHERE state IN ('open', 'planned')"
                ),
            }

    def integrity_check(self) -> tuple[bool, str]:
        if not self.initialized:
            return False, f"not initialized; run `literary init` ({self.root})"
        project = self.project_info()
        required_tables = {
            "project_meta",
            "documents",
            "document_versions",
            "story_facts",
            "fact_proposals",
            "scene_cards",
            "timeline_entries",
            "foreshadowing",
            "review_issues",
        }
        with self._connection() as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
            quick_check = str(result[0]) if result is not None else "no result"
            if quick_check.lower() != "ok":
                return False, f"SQLite quick_check failed: {quick_check}"
            foreign_key_issues = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_issues:
                return False, f"SQLite reports {len(foreign_key_issues)} foreign-key violation(s)"
            table_rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
            missing = sorted(required_tables - {str(row["name"]) for row in table_rows})
            if missing:
                return False, f"missing story database tables: {', '.join(missing)}"
            meta = connection.execute(
                "SELECT value FROM project_meta WHERE key = 'project_id'"
            ).fetchone()
            database_project_id = str(meta["value"]) if meta is not None else ""
            if database_project_id != project.project_id:
                return False, "project.json and story.sqlite3 project IDs do not match"
        return True, "SQLite quick_check=ok, foreign keys=ok, project identity=ok"

    def rename_project(self, name: str) -> ProjectInfo:
        normalized = name.strip()
        if not normalized:
            raise ProjectStoreError("Project name cannot be empty.")
        self._require_initialized()
        payload = self._read_project_payload()
        payload["name"] = normalized
        payload["updated_at"] = _utc_now()
        _atomic_write_text(self.project_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        with self._connection() as connection:
            self._set_meta(connection, "project_name", normalized)
        return self.project_info()

    def create_document(
        self,
        *,
        kind: str,
        slug: str,
        title: str,
        content: str = "",
        state: str = "draft",
        parent: str | int | None = None,
        sequence: int | None = None,
        tags: Iterable[str] = (),
        metadata: dict[str, Any] | None = None,
        reason: str = "Initial document",
    ) -> DocumentRecord:
        self._require_initialized()
        normalized_kind = _required_choice("document kind", kind, DOCUMENT_KINDS)
        normalized_state = _required_choice("document state", state, DOCUMENT_STATES)
        normalized_slug = _slug(slug)
        normalized_title = title.strip()
        if not normalized_title:
            raise ProjectStoreError("Document title cannot be empty.")
        normalized_tags = _tags(tags)
        with self._connection() as connection:
            parent_id = self._resolve_document_id(connection, parent) if parent is not None else None
            now = _utc_now()
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO documents(kind, slug, title, state, parent_id, sequence, active_version_id,
                                          metadata_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                    """,
                    (
                        normalized_kind,
                        normalized_slug,
                        normalized_title,
                        normalized_state,
                        parent_id,
                        sequence,
                        _json(metadata or {}),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ProjectStoreError(f"A document already uses slug `{normalized_slug}`.") from exc
            document_id = int(cursor.lastrowid)
            version_id = self._insert_version(
                connection,
                document_id=document_id,
                content=content,
                state=normalized_state,
                reason=reason,
                metadata={},
            )
            connection.execute(
                "UPDATE documents SET active_version_id = ? WHERE id = ?", (version_id, document_id)
            )
            self._replace_tags(connection, "document", document_id, normalized_tags)
            self._refresh_document_search(connection, document_id)
            document = self._get_document(connection, document_id)
        self._sync_chapter_file(document)
        return document

    def get_document(self, reference: str | int, *, kind: str | None = None) -> DocumentRecord:
        self._require_initialized()
        with self._connection() as connection:
            document_id = self._resolve_document_id(connection, reference, kind=kind)
            return self._get_document(connection, document_id)

    def list_documents(
        self,
        *,
        kind: str | None = None,
        state: str | None = None,
        parent: str | int | None = None,
        limit: int = 200,
    ) -> list[DocumentRecord]:
        self._require_initialized()
        clauses: list[str] = []
        parameters: list[Any] = []
        if kind:
            clauses.append("d.kind = ?")
            parameters.append(_required_choice("document kind", kind, DOCUMENT_KINDS))
        if state:
            clauses.append("d.state = ?")
            parameters.append(_required_choice("document state", state, DOCUMENT_STATES))
        with self._connection() as connection:
            if parent is not None:
                clauses.append("d.parent_id = ?")
                parameters.append(self._resolve_document_id(connection, parent))
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = connection.execute(
                f"""
                SELECT d.*, v.version_number AS active_version_number, v.content AS active_content
                FROM documents d
                LEFT JOIN document_versions v ON v.id = d.active_version_id
                {where}
                ORDER BY d.kind, COALESCE(d.sequence, 999999), d.created_at, d.id
                LIMIT ?
                """,
                [*parameters, max(1, min(limit, 1000))],
            ).fetchall()
            return [self._document_from_row(connection, row) for row in rows]

    def update_document(
        self,
        reference: str | int,
        *,
        title: str | None = None,
        sequence: int | None = None,
        tags: Iterable[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DocumentRecord:
        self._require_initialized()
        with self._connection() as connection:
            document_id = self._resolve_document_id(connection, reference)
            document = self._get_document(connection, document_id)
            next_title = title.strip() if title is not None else document.title
            if not next_title:
                raise ProjectStoreError("Document title cannot be empty.")
            next_metadata = metadata if metadata is not None else document.metadata
            connection.execute(
                "UPDATE documents SET title = ?, sequence = ?, metadata_json = ?, updated_at = ? WHERE id = ?",
                (next_title, sequence if sequence is not None else document.sequence, _json(next_metadata), _utc_now(), document_id),
            )
            if tags is not None:
                self._replace_tags(connection, "document", document_id, _tags(tags))
            self._refresh_document_search(connection, document_id)
            document = self._get_document(connection, document_id)
        self._sync_chapter_file(document)
        return document

    def create_version(
        self,
        reference: str | int,
        *,
        content: str,
        state: str = "candidate",
        reason: str = "",
        metadata: dict[str, Any] | None = None,
        activate: bool = False,
    ) -> DocumentVersion:
        self._require_initialized()
        normalized_state = _required_choice("version state", state, DOCUMENT_STATES)
        with self._connection() as connection:
            document_id = self._resolve_document_id(connection, reference)
            version_id = self._insert_version(
                connection,
                document_id=document_id,
                content=content,
                state=normalized_state,
                reason=reason or "New revision",
                metadata=metadata or {},
            )
            if activate:
                self._activate_version(connection, document_id, version_id, state=normalized_state)
            version = self._get_version(connection, version_id)
        if activate:
            self._sync_chapter_file(self.get_document(document_id))
        return version

    def edit_active_document(
        self, reference: str | int, *, content: str, expected_version_id: int, reason: str
    ) -> DocumentVersion:
        """Activate a revision only if the author/model read the current version."""
        self._require_initialized()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            document_id = self._resolve_document_id(connection, reference)
            document = self._get_document(connection, document_id)
            if document.active_version_id != expected_version_id:
                raise ProjectStoreError("Document changed since it was read; reload before editing.")
            version_id = self._insert_version(
                connection, document_id=document_id, content=content, state=document.state,
                reason=reason or "Author-directed edit", metadata={"previous_version_id": expected_version_id},
            )
            self._activate_version(connection, document_id, version_id, state=document.state)
            version = self._get_version(connection, version_id)
        self._sync_chapter_file(self.get_document(document_id))
        return version

    def edit_chapter_documents(
        self, reference: str | int, *, expected_versions: dict[int, int | None], contents: dict[int, str],
        reason: str = "Imported author edits from chapter file", origin: str = "chapter-file",
    ) -> list[DocumentVersion]:
        """Import an author's chapter edits atomically against the exported snapshot."""
        self._require_initialized()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            chapter_id = self._resolve_document_id(connection, reference, kind="chapter")
            rows = connection.execute(
                "SELECT id, active_version_id FROM documents WHERE id = ? OR (parent_id = ? AND kind = 'scene')",
                (chapter_id, chapter_id),
            ).fetchall()
            current = {row["id"]: row["active_version_id"] for row in rows}
            if current != expected_versions or set(contents) != set(current):
                raise ProjectStoreError("Chapter changed in the database; compare and reconcile before importing.")
            revisions: list[DocumentVersion] = []
            for document_id, content in contents.items():
                document = self._get_document(connection, document_id)
                if content == document.content:
                    continue
                version_id = self._insert_version(
                    connection, document_id=document_id, content=content, state=document.state,
                    reason=reason,
                    metadata={"previous_version_id": document.active_version_id, "origin": origin},
                )
                self._activate_version(connection, document_id, version_id, state=document.state)
                revisions.append(self._get_version(connection, version_id))
            return revisions

    def list_versions(self, reference: str | int, *, limit: int = 100) -> list[DocumentVersion]:
        self._require_initialized()
        with self._connection() as connection:
            document_id = self._resolve_document_id(connection, reference)
            rows = connection.execute(
                """
                SELECT * FROM document_versions
                WHERE document_id = ?
                ORDER BY version_number DESC
                LIMIT ?
                """,
                (document_id, max(1, min(limit, 1000))),
            ).fetchall()
            return [_version_from_row(row) for row in rows]

    def get_version(self, reference: str | int, version: int) -> DocumentVersion:
        self._require_initialized()
        with self._connection() as connection:
            document_id = self._resolve_document_id(connection, reference)
            return self._resolve_version(connection, document_id, version)

    def accept_version(
        self,
        reference: str | int,
        version: int,
        *,
        final: bool = False,
    ) -> DocumentRecord:
        self._require_initialized()
        target_state = "final" if final else "accepted"
        from .supervision import candidate_reports, chapter_for, publish_accepted_reports

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            document_id = self._resolve_document_id(connection, reference)
            source_document = self._get_document(connection, document_id)
            version_record = self._resolve_version(connection, document_id, version)
            if version_record.state == "rejected":
                raise ProjectStoreError("A rejected version cannot be accepted; restore it as a new candidate first.")
            reports = candidate_reports(self, source_document, version_record, final=final)
            self._activate_version(connection, document_id, version_record.id, state=target_state)
            if self._document_kind(connection, document_id) == "scene":
                scene_state = "final" if final else "accepted"
                connection.execute(
                    "UPDATE scene_cards SET status = ?, updated_at = ? WHERE document_id = ?",
                    (scene_state, _utc_now(), document_id),
                )
            self._refresh_document_search(connection, document_id)
            document = self._get_document(connection, document_id)
        if reports:
            try:
                publish_accepted_reports(self, chapter_for(self, document), reports)
            except (OSError, ValueError) as exc:
                warnings.warn(f"Version accepted, but analysis publication needs attention: {exc}", UserWarning, stacklevel=2)
        self._sync_chapter_file(document)
        return document

    def _sync_chapter_file(self, document: DocumentRecord) -> None:
        if document.kind not in {"chapter", "scene"}:
            return
        chapter_id = document.id if document.kind == "chapter" else document.parent_id
        if chapter_id is None:
            return
        from .chapter_sync import ChapterSync

        try:
            chapter = self.get_document(chapter_id, kind="chapter")
            ChapterSync(self).export(chapter.slug)
        except (OSError, ValueError) as exc:
            warnings.warn(
                f"Database saved; chapter file was preserved and needs attention: {exc}",
                UserWarning, stacklevel=2,
            )

    def reject_version(self, reference: str | int, version: int) -> DocumentVersion:
        self._require_initialized()
        with self._connection() as connection:
            document_id = self._resolve_document_id(connection, reference)
            document = self._get_document(connection, document_id)
            target = self._resolve_version(connection, document_id, version)
            if document.active_version_id == target.id:
                raise ProjectStoreError("Cannot reject the active version. Accept or restore another version first.")
            connection.execute(
                "UPDATE document_versions SET state = ? WHERE id = ?", ("rejected", target.id)
            )
            return self._get_version(connection, target.id)

    def restore_version(self, reference: str | int, version: int, *, reason: str = "Restored revision") -> DocumentVersion:
        self._require_initialized()
        with self._connection() as connection:
            document_id = self._resolve_document_id(connection, reference)
            source = self._resolve_version(connection, document_id, version)
            restored_id = self._insert_version(
                connection,
                document_id=document_id,
                content=source.content,
                state="candidate",
                reason=reason,
                metadata={"restored_from_version": source.version_number},
            )
            return self._get_version(connection, restored_id)

    def search_documents(
        self,
        query: str,
        *,
        kinds: Iterable[str] = (),
        tags: Iterable[str] = (),
        limit: int = 12,
    ) -> list[DocumentRecord]:
        self._require_initialized()
        normalized_kinds = tuple(_required_choice("document kind", kind, DOCUMENT_KINDS) for kind in kinds)
        normalized_tags = _tags(tags)
        with self._connection() as connection:
            candidate_ids = self._search_document_ids(connection, query, normalized_kinds, normalized_tags, limit)
            return [self._get_document(connection, document_id) for document_id in candidate_ids]

    def add_fact(
        self,
        *,
        category: str,
        key: str,
        value: str,
        state: str = "idea",
        tags: Iterable[str] = (),
        source_document: str | int | None = None,
        source_version: int | None = None,
        rationale: str = "",
    ) -> StoryFact:
        self._require_initialized()
        normalized_category = _required_text("Fact category", category)
        normalized_key = _required_text("Fact key", key)
        normalized_value = _required_text("Fact value", value)
        normalized_state = _required_choice("fact state", state, FACT_STATES)
        with self._connection() as connection:
            source_document_id = (
                self._resolve_document_id(connection, source_document) if source_document is not None else None
            )
            source_version_id = self._resolve_source_version(connection, source_document_id, source_version)
            now = _utc_now()
            cursor = connection.execute(
                """
                INSERT INTO story_facts(category, fact_key, value, state, tags_json, source_document_id,
                                        source_version_id, rationale, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_category,
                    normalized_key,
                    normalized_value,
                    normalized_state,
                    _json(list(_tags(tags))),
                    source_document_id,
                    source_version_id,
                    rationale.strip(),
                    now,
                    now,
                ),
            )
            fact_id = int(cursor.lastrowid)
            self._record_fact_history(connection, fact_id, "created", None, normalized_value, rationale)
            return self._get_fact(connection, fact_id)

    def set_fact(
        self,
        *,
        category: str,
        key: str,
        value: str,
        state: str = "canonical",
        tags: Iterable[str] = (),
        source_document: str | int | None = None,
        source_version: int | None = None,
        rationale: str = "",
    ) -> StoryFact:
        self._require_initialized()
        normalized_category = _required_text("Fact category", category)
        normalized_key = _required_text("Fact key", key)
        normalized_value = _required_text("Fact value", value)
        normalized_state = _required_choice("fact state", state, FACT_STATES)
        with self._connection() as connection:
            source_document_id = (
                self._resolve_document_id(connection, source_document) if source_document is not None else None
            )
            source_version_id = self._resolve_source_version(connection, source_document_id, source_version)
            return self._upsert_fact(
                connection,
                category=normalized_category,
                key=normalized_key,
                value=normalized_value,
                state=normalized_state,
                tags=_tags(tags),
                source_document_id=source_document_id,
                source_version_id=source_version_id,
                rationale=rationale or "Explicit fact set",
            )

    def get_fact(self, fact_id: int) -> StoryFact:
        self._require_initialized()
        with self._connection() as connection:
            return self._get_fact(connection, fact_id)

    def list_facts(
        self,
        *,
        state: str | None = None,
        category: str | None = None,
        query: str = "",
        tags: Iterable[str] = (),
        limit: int = 300,
    ) -> list[StoryFact]:
        self._require_initialized()
        clauses: list[str] = []
        parameters: list[Any] = []
        if state:
            clauses.append("state = ?")
            parameters.append(_required_choice("fact state", state, FACT_STATES))
        if category:
            clauses.append("category = ?")
            parameters.append(category.strip())
        if query.strip():
            clauses.append("LOWER(category || ' ' || fact_key || ' ' || value) LIKE ?")
            parameters.append(f"%{query.lower().strip()}%")
        for tag in _tags(tags):
            clauses.append("tags_json LIKE ?")
            parameters.append(f'%"{tag}"%')
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM story_facts {where} ORDER BY state, category, fact_key, id LIMIT ?",
                [*parameters, max(1, min(limit, 1000))],
            ).fetchall()
            return [_fact_from_row(row) for row in rows]

    def promote_fact(self, fact_id: int, *, rationale: str = "Confirmed by author") -> StoryFact:
        return self._set_fact_state(fact_id, "canonical", rationale)

    def archive_fact(self, fact_id: int, *, rationale: str = "Archived by author") -> StoryFact:
        return self._set_fact_state(fact_id, "archived", rationale)

    def fact_history(self, fact_id: int) -> list[dict[str, Any]]:
        self._require_initialized()
        with self._connection() as connection:
            self._get_fact(connection, fact_id)
            rows = connection.execute(
                "SELECT * FROM fact_history WHERE fact_id = ? ORDER BY id DESC", (fact_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    def propose_fact(
        self,
        *,
        category: str,
        key: str,
        value: str,
        tags: Iterable[str] = (),
        source_document: str | int | None = None,
        source_version: int | None = None,
        rationale: str = "",
    ) -> FactProposal:
        self._require_initialized()
        with self._connection() as connection:
            source_document_id = (
                self._resolve_document_id(connection, source_document) if source_document is not None else None
            )
            source_version_id = self._resolve_source_version(connection, source_document_id, source_version)
            cursor = connection.execute(
                """
                INSERT INTO fact_proposals(category, fact_key, value, tags_json, source_document_id,
                                           source_version_id, status, rationale, created_at, resolved_at)
                VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?, ?, NULL)
                """,
                (
                    _required_text("Proposal category", category),
                    _required_text("Proposal key", key),
                    _required_text("Proposal value", value),
                    _json(list(_tags(tags))),
                    source_document_id,
                    source_version_id,
                    rationale.strip(),
                    _utc_now(),
                ),
            )
            return self._get_proposal(connection, int(cursor.lastrowid))

    def list_proposals(self, *, status: str = "proposed", limit: int = 300) -> list[FactProposal]:
        self._require_initialized()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM fact_proposals WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, max(1, min(limit, 1000))),
            ).fetchall()
            return [_proposal_from_row(row) for row in rows]

    def accept_proposal(self, proposal_id: int, *, state: str = "canonical") -> StoryFact:
        self._require_initialized()
        normalized_state = _required_choice("fact state", state, FACT_STATES)
        with self._connection() as connection:
            proposal = self._get_proposal(connection, proposal_id)
            if proposal.status != "proposed":
                raise ProjectStoreError(f"Fact proposal {proposal_id} is already {proposal.status}.")
            fact = self._upsert_fact(
                connection,
                category=proposal.category,
                key=proposal.key,
                value=proposal.value,
                state=normalized_state,
                tags=proposal.tags,
                source_document_id=proposal.source_document_id,
                source_version_id=proposal.source_version_id,
                rationale=f"Accepted proposal {proposal_id}: {proposal.rationale}",
            )
            connection.execute(
                "UPDATE fact_proposals SET status = 'accepted', resolved_at = ? WHERE id = ?",
                (_utc_now(), proposal_id),
            )
            return fact

    def reject_proposal(self, proposal_id: int) -> FactProposal:
        self._require_initialized()
        with self._connection() as connection:
            proposal = self._get_proposal(connection, proposal_id)
            if proposal.status != "proposed":
                raise ProjectStoreError(f"Fact proposal {proposal_id} is already {proposal.status}.")
            connection.execute(
                "UPDATE fact_proposals SET status = 'rejected', resolved_at = ? WHERE id = ?",
                (_utc_now(), proposal_id),
            )
            return self._get_proposal(connection, proposal_id)

    def create_scene(
        self,
        *,
        slug: str,
        title: str,
        chapter: str | int | None = None,
        sequence: int | None = None,
        pov: str = "",
        narrative_tense: str = "",
        time_label: str = "",
        location: str = "",
        goal: str = "",
        characters: Iterable[str] = (),
        conflict: str = "",
        required_information: str = "",
        emotional_change: str = "",
        end_state: str = "",
        word_min: int | None = None,
        word_max: int | None = None,
        tags: Iterable[str] = (),
    ) -> SceneCard:
        self._require_initialized()
        if word_min is not None and word_min < 0:
            raise ProjectStoreError("Scene minimum word count cannot be negative.")
        if word_max is not None and word_max < 1:
            raise ProjectStoreError("Scene maximum word count must be positive.")
        if word_min is not None and word_max is not None and word_min > word_max:
            raise ProjectStoreError("Scene minimum word count cannot exceed the maximum.")
        document = self.create_document(
            kind="scene",
            slug=slug,
            title=title,
            content="",
            state="draft",
            parent=chapter,
            sequence=sequence,
            tags=tags,
            metadata={"created_from": "scene-card"},
            reason="Scene card created",
        )
        with self._connection() as connection:
            chapter_id = self._resolve_document_id(connection, chapter, kind="chapter") if chapter else document.parent_id
            if chapter_id is not None and document.parent_id != chapter_id:
                connection.execute(
                    "UPDATE documents SET parent_id = ?, updated_at = ? WHERE id = ?",
                    (chapter_id, _utc_now(), document.id),
                )
            now = _utc_now()
            connection.execute(
                """
                INSERT INTO scene_cards(document_id, chapter_id, status, pov, narrative_tense, time_label,
                                        location, goal, characters_json, conflict, required_information,
                                        emotional_change, end_state, word_min, word_max, plan, plan_state,
                                        metadata_json, created_at, updated_at)
                VALUES (?, ?, 'planned', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 'empty', '{}', ?, ?)
                """,
                (
                    document.id,
                    chapter_id,
                    pov.strip(),
                    narrative_tense.strip(),
                    time_label.strip(),
                    location.strip(),
                    goal.strip(),
                    _json(list(_tags(characters))),
                    conflict.strip(),
                    required_information.strip(),
                    emotional_change.strip(),
                    end_state.strip(),
                    word_min,
                    word_max,
                    now,
                    now,
                ),
            )
            return self._get_scene(connection, document.id)

    def get_scene(self, reference: str | int) -> SceneCard:
        self._require_initialized()
        with self._connection() as connection:
            return self._get_scene(connection, self._resolve_document_id(connection, reference, kind="scene"))

    def list_scenes(self, *, chapter: str | int | None = None, limit: int = 300) -> list[SceneCard]:
        self._require_initialized()
        with self._connection() as connection:
            if chapter is None:
                rows = connection.execute(
                    "SELECT document_id FROM scene_cards ORDER BY chapter_id, document_id LIMIT ?",
                    (max(1, min(limit, 1000)),),
                ).fetchall()
            else:
                chapter_id = self._resolve_document_id(connection, chapter, kind="chapter")
                rows = connection.execute(
                    "SELECT document_id FROM scene_cards WHERE chapter_id = ? ORDER BY document_id LIMIT ?",
                    (chapter_id, max(1, min(limit, 1000))),
                ).fetchall()
            scenes = [self._get_scene(connection, int(row["document_id"])) for row in rows]
            return sorted(scenes, key=lambda item: (item.document.sequence or 999999, item.document.id))

    def update_scene_card(self, reference: str | int, **changes: Any) -> SceneCard:
        self._require_initialized()
        allowed = {
            "pov",
            "narrative_tense",
            "time_label",
            "location",
            "goal",
            "characters",
            "conflict",
            "required_information",
            "emotional_change",
            "end_state",
            "word_min",
            "word_max",
            "metadata",
        }
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise ProjectStoreError(f"Unknown scene card fields: {', '.join(unknown)}")
        with self._connection() as connection:
            document_id = self._resolve_document_id(connection, reference, kind="scene")
            current = self._get_scene(connection, document_id)
            values = {
                "pov": current.pov,
                "narrative_tense": current.narrative_tense,
                "time_label": current.time_label,
                "location": current.location,
                "goal": current.goal,
                "characters_json": _json(list(current.characters)),
                "conflict": current.conflict,
                "required_information": current.required_information,
                "emotional_change": current.emotional_change,
                "end_state": current.end_state,
                "word_min": current.word_min,
                "word_max": current.word_max,
                "metadata_json": _json(current.metadata),
            }
            for field, value in changes.items():
                target = "characters_json" if field == "characters" else "metadata_json" if field == "metadata" else field
                if field == "characters":
                    values[target] = _json(list(_tags(value or ())))
                elif field == "metadata":
                    values[target] = _json(value or {})
                elif isinstance(value, str):
                    values[target] = value.strip()
                else:
                    values[target] = value
            if values["word_min"] is not None and values["word_max"] is not None and values["word_min"] > values["word_max"]:
                raise ProjectStoreError("Scene minimum word count cannot exceed the maximum.")
            connection.execute(
                """
                UPDATE scene_cards
                SET pov = ?, narrative_tense = ?, time_label = ?, location = ?, goal = ?, characters_json = ?,
                    conflict = ?, required_information = ?, emotional_change = ?, end_state = ?, word_min = ?,
                    word_max = ?, metadata_json = ?, updated_at = ?
                WHERE document_id = ?
                """,
                (
                    values["pov"],
                    values["narrative_tense"],
                    values["time_label"],
                    values["location"],
                    values["goal"],
                    values["characters_json"],
                    values["conflict"],
                    values["required_information"],
                    values["emotional_change"],
                    values["end_state"],
                    values["word_min"],
                    values["word_max"],
                    values["metadata_json"],
                    _utc_now(),
                    document_id,
                ),
            )
            return self._get_scene(connection, document_id)

    def set_scene_plan(self, reference: str | int, plan: str, *, state: str = "candidate") -> SceneCard:
        self._require_initialized()
        if not plan.strip():
            raise ProjectStoreError("Scene plan cannot be empty.")
        if state not in {"candidate", "approved"}:
            raise ProjectStoreError("Scene plan state must be candidate or approved.")
        with self._connection() as connection:
            document_id = self._resolve_document_id(connection, reference, kind="scene")
            connection.execute(
                """
                UPDATE scene_cards
                SET plan = ?, plan_state = ?, status = ?, updated_at = ?
                WHERE document_id = ?
                """,
                (plan.strip(), state, "approved" if state == "approved" else "planned", _utc_now(), document_id),
            )
            return self._get_scene(connection, document_id)

    def approve_scene(self, reference: str | int) -> SceneCard:
        self._require_initialized()
        with self._connection() as connection:
            document_id = self._resolve_document_id(connection, reference, kind="scene")
            scene = self._get_scene(connection, document_id)
            if not scene.plan.strip():
                raise ProjectStoreError("Create or supply a scene plan before approval.")
            connection.execute(
                "UPDATE scene_cards SET plan_state = 'approved', status = 'approved', updated_at = ? WHERE document_id = ?",
                (_utc_now(), document_id),
            )
            return self._get_scene(connection, document_id)

    def mark_scene_drafted(self, reference: str | int) -> SceneCard:
        self._require_initialized()
        with self._connection() as connection:
            document_id = self._resolve_document_id(connection, reference, kind="scene")
            connection.execute(
                "UPDATE scene_cards SET status = 'drafted', updated_at = ? WHERE document_id = ?",
                (_utc_now(), document_id),
            )
            return self._get_scene(connection, document_id)

    def add_timeline_entry(
        self,
        *,
        label: str,
        event: str,
        sort_key: str = "",
        state: str = "idea",
        tags: Iterable[str] = (),
        source_document: str | int | None = None,
    ) -> TimelineEntry:
        self._require_initialized()
        normalized_state = _required_choice("timeline state", state, FACT_STATES)
        with self._connection() as connection:
            source_document_id = (
                self._resolve_document_id(connection, source_document) if source_document is not None else None
            )
            now = _utc_now()
            cursor = connection.execute(
                """
                INSERT INTO timeline_entries(label, sort_key, event, state, tags_json, source_document_id,
                                             created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _required_text("Timeline label", label),
                    sort_key.strip(),
                    _required_text("Timeline event", event),
                    normalized_state,
                    _json(list(_tags(tags))),
                    source_document_id,
                    now,
                    now,
                ),
            )
            return self._get_timeline_entry(connection, int(cursor.lastrowid))

    def update_timeline_entry(
        self,
        entry_id: int,
        *,
        label: str | None = None,
        event: str | None = None,
        sort_key: str | None = None,
        state: str | None = None,
        tags: Iterable[str] | None = None,
        source_document: str | int | None = None,
    ) -> TimelineEntry:
        self._require_initialized()
        with self._connection() as connection:
            entry = self._get_timeline_entry(connection, entry_id)
            connection.execute(
                """UPDATE timeline_entries
                   SET label = ?, event = ?, sort_key = ?, state = ?, tags_json = ?,
                       source_document_id = ?, updated_at = ? WHERE id = ?""",
                (
                    _required_text("Timeline label", label) if label is not None else entry.label,
                    _required_text("Timeline event", event) if event is not None else entry.event,
                    sort_key.strip() if sort_key is not None else entry.sort_key,
                    _required_choice("timeline state", state, FACT_STATES) if state is not None else entry.state,
                    _json(list(_tags(tags))) if tags is not None else _json(list(entry.tags)),
                    self._resolve_document_id(connection, source_document)
                    if source_document is not None else entry.source_document_id,
                    _utc_now(),
                    entry_id,
                ),
            )
            return self._get_timeline_entry(connection, entry_id)

    def list_timeline(self, *, state: str | None = None, limit: int = 500) -> list[TimelineEntry]:
        self._require_initialized()
        with self._connection() as connection:
            if state:
                normalized_state = _required_choice("timeline state", state, FACT_STATES)
                rows = connection.execute(
                    "SELECT * FROM timeline_entries WHERE state = ? ORDER BY sort_key, id LIMIT ?",
                    (normalized_state, max(1, min(limit, 1000))),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM timeline_entries ORDER BY sort_key, id LIMIT ?",
                    (max(1, min(limit, 1000)),),
                ).fetchall()
            return [_timeline_from_row(row) for row in rows]

    def add_foreshadowing(
        self,
        *,
        title: str,
        setup_note: str,
        setup_document: str | int | None = None,
        tags: Iterable[str] = (),
        state: str = "open",
    ) -> Foreshadowing:
        self._require_initialized()
        if state not in {"open", "planned", "paid", "abandoned"}:
            raise ProjectStoreError("Foreshadowing state must be open, planned, paid, or abandoned.")
        with self._connection() as connection:
            setup_document_id = (
                self._resolve_document_id(connection, setup_document) if setup_document is not None else None
            )
            now = _utc_now()
            cursor = connection.execute(
                """
                INSERT INTO foreshadowing(title, setup_note, setup_document_id, payoff_note,
                                          payoff_document_id, state, tags_json, created_at, updated_at)
                VALUES (?, ?, ?, '', NULL, ?, ?, ?, ?)
                """,
                (
                    _required_text("Foreshadowing title", title),
                    _required_text("Foreshadowing setup", setup_note),
                    setup_document_id,
                    state,
                    _json(list(_tags(tags))),
                    now,
                    now,
                ),
            )
            return self._get_foreshadowing(connection, int(cursor.lastrowid))

    def resolve_foreshadowing(
        self,
        foreshadow_id: int,
        *,
        payoff_note: str,
        payoff_document: str | int | None = None,
        state: str = "paid",
    ) -> Foreshadowing:
        self._require_initialized()
        if state not in {"paid", "abandoned"}:
            raise ProjectStoreError("Resolved foreshadowing state must be paid or abandoned.")
        with self._connection() as connection:
            self._get_foreshadowing(connection, foreshadow_id)
            payoff_document_id = (
                self._resolve_document_id(connection, payoff_document) if payoff_document is not None else None
            )
            connection.execute(
                """
                UPDATE foreshadowing
                SET payoff_note = ?, payoff_document_id = ?, state = ?, updated_at = ?
                WHERE id = ?
                """,
                (_required_text("Foreshadowing payoff", payoff_note), payoff_document_id, state, _utc_now(), foreshadow_id),
            )
            return self._get_foreshadowing(connection, foreshadow_id)

    def list_foreshadowing(self, *, state: str | None = None, limit: int = 500) -> list[Foreshadowing]:
        self._require_initialized()
        with self._connection() as connection:
            if state:
                rows = connection.execute(
                    "SELECT * FROM foreshadowing WHERE state = ? ORDER BY id LIMIT ?",
                    (state, max(1, min(limit, 1000))),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM foreshadowing ORDER BY state, id LIMIT ?",
                    (max(1, min(limit, 1000)),),
                ).fetchall()
            return [_foreshadowing_from_row(row) for row in rows]

    def replace_review_issues(
        self,
        *,
        document: str | int | None,
        issues: Iterable[dict[str, Any]],
        run_id: str | None = None,
    ) -> list[ReviewIssue]:
        self._require_initialized()
        with self._connection() as connection:
            document_id = self._resolve_document_id(connection, document) if document is not None else None
            now = _utc_now()
            if document_id is None:
                connection.execute(
                    "UPDATE review_issues SET status = 'superseded' WHERE status = 'open' AND document_id IS NULL"
                )
            else:
                connection.execute(
                    "UPDATE review_issues SET status = 'superseded' WHERE status = 'open' AND document_id = ?",
                    (document_id,),
                )
            created: list[ReviewIssue] = []
            for issue in issues:
                severity = str(issue.get("severity") or "warning").lower()
                if severity not in {"info", "warning", "error"}:
                    severity = "warning"
                cursor = connection.execute(
                    """
                    INSERT INTO review_issues(document_id, run_id, severity, category, message, evidence,
                                              suggested_actions_json, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)
                    """,
                    (
                        document_id,
                        run_id,
                        severity,
                        str(issue.get("category") or "general"),
                        str(issue.get("message") or ""),
                        str(issue.get("evidence") or ""),
                        _json([str(item) for item in issue.get("suggested_actions", [])]),
                        now,
                    ),
                )
                created.append(self._get_review_issue(connection, int(cursor.lastrowid)))
            return created

    def list_review_issues(
        self,
        *,
        document: str | int | None = None,
        status: str = "open",
        limit: int = 500,
    ) -> list[ReviewIssue]:
        self._require_initialized()
        with self._connection() as connection:
            clauses = ["status = ?"]
            parameters: list[Any] = [status]
            if document is not None:
                clauses.append("document_id = ?")
                parameters.append(self._resolve_document_id(connection, document))
            rows = connection.execute(
                f"SELECT * FROM review_issues WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?",
                [*parameters, max(1, min(limit, 1000))],
            ).fetchall()
            return [_review_issue_from_row(row) for row in rows]

    def context_snapshot(
        self,
        *,
        scene: str | int | None = None,
        query: str = "",
        max_documents: int = 8,
        max_facts: int = 80,
    ) -> dict[str, Any]:
        """Return structured project context for prompts and review without loading the whole novel."""
        self._require_initialized()
        selected_scene = self.get_scene(scene) if scene is not None else None
        terms: list[str] = [query]
        if selected_scene is not None:
            terms.extend(
                [
                    selected_scene.document.title,
                    selected_scene.goal,
                    selected_scene.location,
                    *selected_scene.characters,
                ]
            )
        joined_query = " ".join(term for term in terms if term.strip())
        relevant_tags = selected_scene.characters if selected_scene is not None else ()
        canonical = self.list_facts(state="canonical", tags=relevant_tags, limit=max_facts)
        if len(canonical) < max_facts:
            seen_facts = {fact.id for fact in canonical}
            canonical.extend(fact for fact in self.list_facts(state="canonical", limit=max_facts)
                             if fact.id not in seen_facts)
            canonical = canonical[:max_facts]
        ideas = self.list_facts(state="idea", query=joined_query, tags=relevant_tags, limit=30)
        # Select nonempty active text in SQL: future chapter containers must not
        # evict the design bible, and context assembly must not load the whole novel.
        selection = """
            SELECT d.id FROM documents d JOIN document_versions v ON v.id = d.active_version_id
            WHERE TRIM(v.content) != '' AND d.state NOT IN ('archived', 'rejected')
        """
        documents: list[DocumentRecord] = []
        with self._connection() as connection:
            for kind in ("outline", "world", "character", "style"):
                row = connection.execute(selection + " AND d.kind = ? ORDER BY d.updated_at DESC, d.id DESC LIMIT 1", (kind,)).fetchone()
                if row:
                    documents.append(self._get_document(connection, int(row["id"])))
            rows = connection.execute(selection + " ORDER BY d.updated_at DESC, d.id DESC LIMIT ?", (max_documents,)).fetchall()
            recent = [self._get_document(connection, int(row["id"])) for row in rows]
        known = {document.id for document in documents}
        matches = self.search_documents(joined_query, tags=relevant_tags, limit=max_documents)
        for document in [*matches, *recent]:
            if document.id not in known and document.content.strip() and document.state not in {"archived", "rejected"}:
                documents.append(document)
                known.add(document.id)
        documents = documents[:max_documents]
        if selected_scene is not None:
            with self._connection() as connection:
                siblings = self._scene_siblings_before(connection, selected_scene)
            seen = {document.id for document in siblings}
            documents = [*siblings, *[document for document in documents if document.id not in seen]][:max_documents]
        unresolved_threads = self.list_foreshadowing(state="open", limit=40)
        if len(unresolved_threads) < 40:
            unresolved_threads.extend(self.list_foreshadowing(state="planned", limit=40 - len(unresolved_threads)))
        timeline = self.list_timeline(state="canonical", limit=80)
        return {
            "project": self.project_info(),
            "scene": selected_scene,
            "canonical_facts": canonical,
            "ideas": ideas,
            "documents": documents,
            "foreshadowing": unresolved_threads,
            "timeline": timeline,
        }

    def _scene_siblings_before(self, connection: sqlite3.Connection, scene: SceneCard) -> list[DocumentRecord]:
        if scene.chapter_id is None:
            return []
        chapter = self._get_document(connection, scene.chapter_id)
        rows = connection.execute(
            """
            SELECT d.id
            FROM documents d
            JOIN scene_cards s ON s.document_id = d.id
            JOIN documents c ON c.id = s.chapter_id
            LEFT JOIN document_versions v ON v.id = d.active_version_id
            WHERE (s.chapter_id = ? AND (d.sequence < ? OR (d.sequence = ? AND d.id < ?)))
               OR (c.sequence < ? AND d.state IN ('accepted', 'final') AND TRIM(v.content) != '')
            ORDER BY c.sequence DESC, d.sequence DESC, d.id DESC
            LIMIT 2
            """,
            (scene.chapter_id, scene.document.sequence or 999999, scene.document.sequence or 999999,
             scene.document.id, chapter.sequence),
        ).fetchall()
        return [self._get_document(connection, int(row["id"])) for row in reversed(rows)]

    def _set_fact_state(self, fact_id: int, state: str, rationale: str) -> StoryFact:
        self._require_initialized()
        normalized_state = _required_choice("fact state", state, FACT_STATES)
        with self._connection() as connection:
            fact = self._get_fact(connection, fact_id)
            if fact.state == normalized_state:
                return fact
            connection.execute(
                "UPDATE story_facts SET state = ?, updated_at = ? WHERE id = ?",
                (normalized_state, _utc_now(), fact_id),
            )
            self._record_fact_history(connection, fact_id, f"state:{normalized_state}", fact.value, fact.value, rationale)
            return self._get_fact(connection, fact_id)

    def _require_initialized(self) -> None:
        if not self.initialized:
            raise ProjectStoreError(f"No LiteraryGiant project exists at {self.workspace}. Run `literary init`.")

    def _read_project_payload(self) -> dict[str, Any]:
        try:
            value = json.loads(self.project_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.database_path))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS project_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL,
                slug TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                state TEXT NOT NULL,
                parent_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
                sequence INTEGER,
                active_version_id INTEGER,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS document_versions (
                id INTEGER PRIMARY KEY,
                document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                version_number INTEGER NOT NULL,
                state TEXT NOT NULL,
                content TEXT NOT NULL,
                reason TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                UNIQUE(document_id, version_number)
            );

            CREATE INDEX IF NOT EXISTS idx_documents_parent_sequence ON documents(parent_id, sequence, id);
            CREATE INDEX IF NOT EXISTS idx_document_versions_document ON document_versions(document_id, version_number DESC);

            CREATE TABLE IF NOT EXISTS entity_tags (
                owner_type TEXT NOT NULL,
                owner_id INTEGER NOT NULL,
                tag TEXT NOT NULL,
                PRIMARY KEY(owner_type, owner_id, tag)
            );
            CREATE INDEX IF NOT EXISTS idx_entity_tags_lookup ON entity_tags(owner_type, tag, owner_id);

            CREATE TABLE IF NOT EXISTS story_facts (
                id INTEGER PRIMARY KEY,
                category TEXT NOT NULL,
                fact_key TEXT NOT NULL,
                value TEXT NOT NULL,
                state TEXT NOT NULL,
                tags_json TEXT NOT NULL DEFAULT '[]',
                source_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
                source_version_id INTEGER REFERENCES document_versions(id) ON DELETE SET NULL,
                rationale TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_story_facts_state_category ON story_facts(state, category, fact_key);

            CREATE TABLE IF NOT EXISTS fact_history (
                id INTEGER PRIMARY KEY,
                fact_id INTEGER NOT NULL REFERENCES story_facts(id) ON DELETE CASCADE,
                action TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                rationale TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS fact_proposals (
                id INTEGER PRIMARY KEY,
                category TEXT NOT NULL,
                fact_key TEXT NOT NULL,
                value TEXT NOT NULL,
                tags_json TEXT NOT NULL DEFAULT '[]',
                source_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
                source_version_id INTEGER REFERENCES document_versions(id) ON DELETE SET NULL,
                status TEXT NOT NULL,
                rationale TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                resolved_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_fact_proposals_status ON fact_proposals(status, id DESC);

            CREATE TABLE IF NOT EXISTS scene_cards (
                document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
                chapter_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
                status TEXT NOT NULL,
                pov TEXT NOT NULL DEFAULT '',
                narrative_tense TEXT NOT NULL DEFAULT '',
                time_label TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                goal TEXT NOT NULL DEFAULT '',
                characters_json TEXT NOT NULL DEFAULT '[]',
                conflict TEXT NOT NULL DEFAULT '',
                required_information TEXT NOT NULL DEFAULT '',
                emotional_change TEXT NOT NULL DEFAULT '',
                end_state TEXT NOT NULL DEFAULT '',
                word_min INTEGER,
                word_max INTEGER,
                plan TEXT NOT NULL DEFAULT '',
                plan_state TEXT NOT NULL DEFAULT 'empty',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_scene_cards_chapter ON scene_cards(chapter_id, document_id);

            CREATE TABLE IF NOT EXISTS timeline_entries (
                id INTEGER PRIMARY KEY,
                label TEXT NOT NULL,
                sort_key TEXT NOT NULL DEFAULT '',
                event TEXT NOT NULL,
                state TEXT NOT NULL,
                tags_json TEXT NOT NULL DEFAULT '[]',
                source_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_timeline_state_sort ON timeline_entries(state, sort_key, id);

            CREATE TABLE IF NOT EXISTS foreshadowing (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                setup_note TEXT NOT NULL,
                setup_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
                payoff_note TEXT NOT NULL DEFAULT '',
                payoff_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
                state TEXT NOT NULL,
                tags_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_foreshadowing_state ON foreshadowing(state, id);

            CREATE TABLE IF NOT EXISTS review_issues (
                id INTEGER PRIMARY KEY,
                document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
                run_id TEXT,
                severity TEXT NOT NULL,
                category TEXT NOT NULL,
                message TEXT NOT NULL,
                evidence TEXT NOT NULL DEFAULT '',
                suggested_actions_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_review_issues_document ON review_issues(document_id, status, id DESC);
            """
        )
        try:
            connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(slug, title, kind, content, tags)"
            )
        except sqlite3.OperationalError:
            # FTS5 is standard in supported Python distributions; LIKE fallback remains usable.
            pass

    def _set_meta(self, connection: sqlite3.Connection, key: str, value: str) -> None:
        connection.execute(
            "INSERT INTO project_meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def _resolve_document_id(
        self,
        connection: sqlite3.Connection,
        reference: str | int | None,
        *,
        kind: str | None = None,
    ) -> int:
        if reference is None:
            raise ProjectStoreError("A document reference is required.")
        if isinstance(reference, int) or (isinstance(reference, str) and reference.strip().isdigit()):
            row = connection.execute("SELECT id, kind FROM documents WHERE id = ?", (int(reference),)).fetchone()
        else:
            raw = str(reference).strip()
            slug = raw.split(":", 1)[-1] if ":" in raw else raw
            row = connection.execute("SELECT id, kind FROM documents WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            raise ProjectStoreError(f"Document `{reference}` was not found.")
        if kind is not None and str(row["kind"]) != kind:
            raise ProjectStoreError(f"Document `{reference}` is a {row['kind']}, not a {kind}.")
        return int(row["id"])

    def _get_document(self, connection: sqlite3.Connection, document_id: int) -> DocumentRecord:
        row = connection.execute(
            """
            SELECT d.*, v.version_number AS active_version_number, v.content AS active_content
            FROM documents d
            LEFT JOIN document_versions v ON v.id = d.active_version_id
            WHERE d.id = ?
            """,
            (document_id,),
        ).fetchone()
        if row is None:
            raise ProjectStoreError(f"Document `{document_id}` was not found.")
        return self._document_from_row(connection, row)

    def _document_from_row(self, connection: sqlite3.Connection, row: sqlite3.Row) -> DocumentRecord:
        tags = connection.execute(
            "SELECT tag FROM entity_tags WHERE owner_type = 'document' AND owner_id = ? ORDER BY tag",
            (int(row["id"]),),
        ).fetchall()
        return DocumentRecord(
            id=int(row["id"]),
            kind=str(row["kind"]),
            slug=str(row["slug"]),
            title=str(row["title"]),
            state=str(row["state"]),
            parent_id=int(row["parent_id"]) if row["parent_id"] is not None else None,
            sequence=int(row["sequence"]) if row["sequence"] is not None else None,
            active_version_id=int(row["active_version_id"]) if row["active_version_id"] is not None else None,
            active_version_number=(
                int(row["active_version_number"]) if row["active_version_number"] is not None else None
            ),
            content=str(row["active_content"] or ""),
            tags=tuple(str(item["tag"]) for item in tags),
            metadata=_json_object(row["metadata_json"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _insert_version(
        self,
        connection: sqlite3.Connection,
        *,
        document_id: int,
        content: str,
        state: str,
        reason: str,
        metadata: dict[str, Any],
    ) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(version_number), 0) AS maximum FROM document_versions WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        version_number = int(row["maximum"]) + 1
        cursor = connection.execute(
            """
            INSERT INTO document_versions(document_id, version_number, state, content, reason, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (document_id, version_number, state, content, reason, _json(metadata), _utc_now()),
        )
        return int(cursor.lastrowid)

    def _get_version(self, connection: sqlite3.Connection, version_id: int) -> DocumentVersion:
        row = connection.execute("SELECT * FROM document_versions WHERE id = ?", (version_id,)).fetchone()
        if row is None:
            raise ProjectStoreError(f"Version `{version_id}` was not found.")
        return _version_from_row(row)

    def _resolve_version(self, connection: sqlite3.Connection, document_id: int, version: int) -> DocumentVersion:
        row = connection.execute(
            "SELECT * FROM document_versions WHERE document_id = ? AND version_number = ?",
            (document_id, version),
        ).fetchone()
        if row is None:
            raise ProjectStoreError(f"No version `{version}` exists for document `{document_id}`.")
        return _version_from_row(row)

    def _activate_version(
        self,
        connection: sqlite3.Connection,
        document_id: int,
        version_id: int,
        *,
        state: str,
    ) -> None:
        connection.execute("UPDATE document_versions SET state = ? WHERE id = ?", (state, version_id))
        connection.execute(
            "UPDATE documents SET active_version_id = ?, state = ?, updated_at = ? WHERE id = ?",
            (version_id, state, _utc_now(), document_id),
        )

    def _replace_tags(
        self,
        connection: sqlite3.Connection,
        owner_type: str,
        owner_id: int,
        tags: tuple[str, ...],
    ) -> None:
        connection.execute("DELETE FROM entity_tags WHERE owner_type = ? AND owner_id = ?", (owner_type, owner_id))
        connection.executemany(
            "INSERT INTO entity_tags(owner_type, owner_id, tag) VALUES (?, ?, ?)",
            [(owner_type, owner_id, tag) for tag in tags],
        )

    def _refresh_document_search(self, connection: sqlite3.Connection, document_id: int) -> None:
        try:
            document = self._get_document(connection, document_id)
            connection.execute("DELETE FROM document_fts WHERE rowid = ?", (document_id,))
            connection.execute(
                "INSERT INTO document_fts(rowid, slug, title, kind, content, tags) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    document_id,
                    document.slug,
                    document.title,
                    document.kind,
                    document.content,
                    " ".join(document.tags),
                ),
            )
        except sqlite3.OperationalError:
            pass

    def _search_document_ids(
        self,
        connection: sqlite3.Connection,
        query: str,
        kinds: tuple[str, ...],
        tags: tuple[str, ...],
        limit: int,
    ) -> list[int]:
        if not query.strip() and not tags:
            clauses = []
            parameters: list[Any] = []
            if kinds:
                clauses.append(f"kind IN ({','.join('?' for _ in kinds)})")
                parameters.extend(kinds)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = connection.execute(
                f"SELECT id FROM documents {where} ORDER BY updated_at DESC LIMIT ?", [*parameters, limit]
            ).fetchall()
            return [int(row["id"]) for row in rows]

        if not query.strip():
            clauses: list[str] = []
            parameters: list[Any] = []
            if kinds:
                clauses.append(f"d.kind IN ({','.join('?' for _ in kinds)})")
                parameters.extend(kinds)
            for tag in tags:
                clauses.append(
                    "EXISTS (SELECT 1 FROM entity_tags et WHERE et.owner_type = 'document' AND et.owner_id = d.id AND et.tag = ?)"
                )
                parameters.append(tag)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = connection.execute(
                f"SELECT d.id FROM documents d {where} ORDER BY d.updated_at DESC LIMIT ?",
                [*parameters, limit],
            ).fetchall()
            return [int(row["id"]) for row in rows]

        if query.strip():
            try:
                fts_query = _fts_query(query)
                clauses = ["document_fts MATCH ?"]
                parameters: list[Any] = [fts_query]
                if kinds:
                    clauses.append(f"d.kind IN ({','.join('?' for _ in kinds)})")
                    parameters.extend(kinds)
                for tag in tags:
                    clauses.append(
                        "EXISTS (SELECT 1 FROM entity_tags et WHERE et.owner_type = 'document' AND et.owner_id = d.id AND et.tag = ?)"
                    )
                    parameters.append(tag)
                rows = connection.execute(
                    f"""
                    SELECT d.id FROM document_fts
                    JOIN documents d ON d.id = document_fts.rowid
                    WHERE {' AND '.join(clauses)}
                    ORDER BY bm25(document_fts), d.updated_at DESC
                    LIMIT ?
                    """,
                    [*parameters, limit],
                ).fetchall()
                if rows:
                    return [int(row["id"]) for row in rows]
            except sqlite3.OperationalError:
                pass

        terms = [term.lower() for term in _search_terms(query)]
        clauses: list[str] = []
        parameters = []
        if kinds:
            clauses.append(f"d.kind IN ({','.join('?' for _ in kinds)})")
            parameters.extend(kinds)
        for tag in tags:
            clauses.append(
                "EXISTS (SELECT 1 FROM entity_tags et WHERE et.owner_type = 'document' AND et.owner_id = d.id AND et.tag = ?)"
            )
            parameters.append(tag)
        for term in terms:
            clauses.append("LOWER(d.slug || ' ' || d.title || ' ' || COALESCE(v.content, '')) LIKE ?")
            parameters.append(f"%{term}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = connection.execute(
            f"""
            SELECT d.id FROM documents d
            LEFT JOIN document_versions v ON v.id = d.active_version_id
            {where}
            ORDER BY d.updated_at DESC
            LIMIT ?
            """,
            [*parameters, limit],
        ).fetchall()
        return [int(row["id"]) for row in rows]

    def _get_fact(self, connection: sqlite3.Connection, fact_id: int) -> StoryFact:
        row = connection.execute("SELECT * FROM story_facts WHERE id = ?", (fact_id,)).fetchone()
        if row is None:
            raise ProjectStoreError(f"Story Bible fact `{fact_id}` was not found.")
        return _fact_from_row(row)

    def _upsert_fact(
        self,
        connection: sqlite3.Connection,
        *,
        category: str,
        key: str,
        value: str,
        state: str,
        tags: Iterable[str],
        source_document_id: int | None,
        source_version_id: int | None,
        rationale: str,
    ) -> StoryFact:
        row = connection.execute(
            """
            SELECT id, value FROM story_facts
            WHERE category = ? AND fact_key = ? AND state = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (category, key, state),
        ).fetchone()
        now = _utc_now()
        if row is None:
            cursor = connection.execute(
                """
                INSERT INTO story_facts(category, fact_key, value, state, tags_json, source_document_id,
                                        source_version_id, rationale, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    category,
                    key,
                    value,
                    state,
                    _json(list(tags)),
                    source_document_id,
                    source_version_id,
                    rationale.strip(),
                    now,
                    now,
                ),
            )
            fact_id = int(cursor.lastrowid)
            self._record_fact_history(connection, fact_id, "created", None, value, rationale)
            return self._get_fact(connection, fact_id)

        fact_id = int(row["id"])
        old_value = str(row["value"])
        connection.execute(
            """
            UPDATE story_facts
            SET value = ?, tags_json = ?, source_document_id = ?, source_version_id = ?, rationale = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                value,
                _json(list(tags)),
                source_document_id,
                source_version_id,
                rationale.strip(),
                now,
                fact_id,
            ),
        )
        self._record_fact_history(connection, fact_id, "updated", old_value, value, rationale)
        return self._get_fact(connection, fact_id)

    def _record_fact_history(
        self,
        connection: sqlite3.Connection,
        fact_id: int,
        action: str,
        old_value: str | None,
        new_value: str | None,
        rationale: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO fact_history(fact_id, action, old_value, new_value, rationale, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (fact_id, action, old_value, new_value, rationale.strip(), _utc_now()),
        )

    def _resolve_source_version(
        self,
        connection: sqlite3.Connection,
        source_document_id: int | None,
        source_version: int | None,
    ) -> int | None:
        if source_version is None:
            return None
        if source_document_id is None:
            raise ProjectStoreError("A source document is required when a source version is supplied.")
        return self._resolve_version(connection, source_document_id, source_version).id

    def _get_proposal(self, connection: sqlite3.Connection, proposal_id: int) -> FactProposal:
        row = connection.execute("SELECT * FROM fact_proposals WHERE id = ?", (proposal_id,)).fetchone()
        if row is None:
            raise ProjectStoreError(f"Fact proposal `{proposal_id}` was not found.")
        return _proposal_from_row(row)

    def _get_scene(self, connection: sqlite3.Connection, document_id: int) -> SceneCard:
        row = connection.execute("SELECT * FROM scene_cards WHERE document_id = ?", (document_id,)).fetchone()
        if row is None:
            raise ProjectStoreError(f"Scene card for document `{document_id}` was not found.")
        document = self._get_document(connection, document_id)
        return SceneCard(
            document=document,
            chapter_id=int(row["chapter_id"]) if row["chapter_id"] is not None else None,
            status=str(row["status"]),
            pov=str(row["pov"]),
            narrative_tense=str(row["narrative_tense"]),
            time_label=str(row["time_label"]),
            location=str(row["location"]),
            goal=str(row["goal"]),
            characters=tuple(_json_list(row["characters_json"])),
            conflict=str(row["conflict"]),
            required_information=str(row["required_information"]),
            emotional_change=str(row["emotional_change"]),
            end_state=str(row["end_state"]),
            word_min=int(row["word_min"]) if row["word_min"] is not None else None,
            word_max=int(row["word_max"]) if row["word_max"] is not None else None,
            plan=str(row["plan"]),
            plan_state=str(row["plan_state"]),
            metadata=_json_object(row["metadata_json"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _document_kind(self, connection: sqlite3.Connection, document_id: int) -> str:
        row = connection.execute("SELECT kind FROM documents WHERE id = ?", (document_id,)).fetchone()
        if row is None:
            raise ProjectStoreError(f"Document `{document_id}` was not found.")
        return str(row["kind"])

    def _get_timeline_entry(self, connection: sqlite3.Connection, entry_id: int) -> TimelineEntry:
        row = connection.execute("SELECT * FROM timeline_entries WHERE id = ?", (entry_id,)).fetchone()
        if row is None:
            raise ProjectStoreError(f"Timeline entry `{entry_id}` was not found.")
        return _timeline_from_row(row)

    def _get_foreshadowing(self, connection: sqlite3.Connection, item_id: int) -> Foreshadowing:
        row = connection.execute("SELECT * FROM foreshadowing WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise ProjectStoreError(f"Foreshadowing `{item_id}` was not found.")
        return _foreshadowing_from_row(row)

    def _get_review_issue(self, connection: sqlite3.Connection, issue_id: int) -> ReviewIssue:
        row = connection.execute("SELECT * FROM review_issues WHERE id = ?", (issue_id,)).fetchone()
        if row is None:
            raise ProjectStoreError(f"Review issue `{issue_id}` was not found.")
        return _review_issue_from_row(row)


def _version_from_row(row: sqlite3.Row) -> DocumentVersion:
    return DocumentVersion(
        id=int(row["id"]),
        document_id=int(row["document_id"]),
        version_number=int(row["version_number"]),
        state=str(row["state"]),
        content=str(row["content"]),
        reason=str(row["reason"]),
        metadata=_json_object(row["metadata_json"]),
        created_at=str(row["created_at"]),
    )


def _fact_from_row(row: sqlite3.Row) -> StoryFact:
    return StoryFact(
        id=int(row["id"]),
        category=str(row["category"]),
        key=str(row["fact_key"]),
        value=str(row["value"]),
        state=str(row["state"]),
        tags=tuple(_json_list(row["tags_json"])),
        source_document_id=(int(row["source_document_id"]) if row["source_document_id"] is not None else None),
        source_version_id=(int(row["source_version_id"]) if row["source_version_id"] is not None else None),
        rationale=str(row["rationale"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _proposal_from_row(row: sqlite3.Row) -> FactProposal:
    return FactProposal(
        id=int(row["id"]),
        category=str(row["category"]),
        key=str(row["fact_key"]),
        value=str(row["value"]),
        tags=tuple(_json_list(row["tags_json"])),
        source_document_id=(int(row["source_document_id"]) if row["source_document_id"] is not None else None),
        source_version_id=(int(row["source_version_id"]) if row["source_version_id"] is not None else None),
        status=str(row["status"]),
        rationale=str(row["rationale"]),
        created_at=str(row["created_at"]),
        resolved_at=str(row["resolved_at"]) if row["resolved_at"] is not None else None,
    )


def _timeline_from_row(row: sqlite3.Row) -> TimelineEntry:
    return TimelineEntry(
        id=int(row["id"]),
        label=str(row["label"]),
        sort_key=str(row["sort_key"]),
        event=str(row["event"]),
        state=str(row["state"]),
        tags=tuple(_json_list(row["tags_json"])),
        source_document_id=(int(row["source_document_id"]) if row["source_document_id"] is not None else None),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _foreshadowing_from_row(row: sqlite3.Row) -> Foreshadowing:
    return Foreshadowing(
        id=int(row["id"]),
        title=str(row["title"]),
        setup_note=str(row["setup_note"]),
        setup_document_id=(int(row["setup_document_id"]) if row["setup_document_id"] is not None else None),
        payoff_note=str(row["payoff_note"]),
        payoff_document_id=(int(row["payoff_document_id"]) if row["payoff_document_id"] is not None else None),
        state=str(row["state"]),
        tags=tuple(_json_list(row["tags_json"])),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _review_issue_from_row(row: sqlite3.Row) -> ReviewIssue:
    return ReviewIssue(
        id=int(row["id"]),
        document_id=int(row["document_id"]) if row["document_id"] is not None else None,
        severity=str(row["severity"]),
        category=str(row["category"]),
        message=str(row["message"]),
        evidence=str(row["evidence"]),
        suggested_actions=tuple(_json_list(row["suggested_actions_json"])),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: Any) -> list[str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _tags(values: Iterable[str] | str) -> tuple[str, ...]:
    raw_values = [values] if isinstance(values, str) else values
    output: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        for item in str(raw).split(","):
            tag = item.strip().lower()
            if not tag or tag in seen:
                continue
            if len(tag) > 100:
                raise ProjectStoreError("Tags must be 100 characters or fewer.")
            seen.add(tag)
            output.append(tag)
    return tuple(output)


def _required_text(label: str, value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ProjectStoreError(f"{label} cannot be empty.")
    return normalized


def _required_choice(label: str, value: str, choices: set[str]) -> str:
    normalized = value.strip().lower()
    if normalized not in choices:
        raise ProjectStoreError(f"{label} must be one of: {', '.join(sorted(choices))}.")
    return normalized


def _slug(value: str) -> str:
    normalized = value.strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    if not normalized:
        raise ProjectStoreError("Document slug must include letters or numbers.")
    if len(normalized) > 96:
        raise ProjectStoreError("Document slug must be 96 characters or fewer.")
    return normalized


def _fts_query(query: str) -> str:
    terms = _search_terms(query)
    if not terms:
        return '""'
    escaped = [term.replace('"', '""') for term in terms[:12]]
    return " AND ".join(f'"{term}"' for term in escaped)


def _search_terms(query: str) -> list[str]:
    terms = re.findall(r"[A-Za-z0-9_-]+|[\u4e00-\u9fff]+", query)
    return [term for term in terms if term]
