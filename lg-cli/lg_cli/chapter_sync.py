from __future__ import annotations

import difflib
import fcntl
import hashlib
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from .exporter import _atomic_write
from .project_store import DocumentRecord, ProjectStore, ProjectStoreError


@dataclass(frozen=True)
class ChapterFileStatus:
    chapter: str
    path: Path
    status: str


class ChapterSync:
    def __init__(self, store: ProjectStore) -> None:
        self.store = store
        self.directory = store.workspace / "manuscript" / "chapters"
        self.manifests = store.root / "chapter-sync"

    def _documents(self, reference: str) -> list[DocumentRecord]:
        chapter = self.store.get_document(reference, kind="chapter")
        return [chapter, *self.store.list_documents(kind="scene", parent=chapter.id, limit=1000)]

    def _paths(self, chapter: DocumentRecord) -> tuple[Path, Path]:
        path = self.directory / f"{chapter.slug}.md"
        manifest = self.manifests / f"{chapter.id}.json"
        for candidate in (path, manifest):
            if not candidate.resolve().is_relative_to(self.store.workspace.resolve()):
                raise ProjectStoreError("Chapter sync paths must remain inside this project.")
        return path, manifest

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.store.project_info()
        with (self.store.root / "chapter-sync.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def status(self, reference: str) -> ChapterFileStatus:
        documents = self._documents(reference)
        path, manifest = self._paths(documents[0])
        baseline = self._baseline(manifest)
        if not path.exists():
            state = "missing"
        elif baseline is None:
            state = "untracked"
        else:
            edited = _digest(path.read_text(encoding="utf-8")) != baseline["file_hash"]
            changed = self._snapshot(documents) != baseline["documents"]
            state = "conflict" if edited and changed else "file-edited" if edited else "database-changed" if changed else "clean"
        return ChapterFileStatus(documents[0].slug, path, state)

    def export(self, reference: str) -> ChapterFileStatus:
        with self._lock():
            status = self.status(reference)
            if status.status in {"file-edited", "conflict", "untracked"}:
                raise ProjectStoreError(f"Preserving external chapter edits ({status.status}); use chapter diff/import first.")
            documents = self._documents(reference)
            path, manifest = self._paths(documents[0])
            text = _render(documents)
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, text)
            self._save_baseline(manifest, documents, text)
            return self.status(reference)

    def diff(self, reference: str) -> str:
        documents = self._documents(reference)
        path, _ = self._paths(documents[0])
        if not path.is_file():
            raise ProjectStoreError("No chapter file yet; run chapter sync first.")
        return "".join(difflib.unified_diff(
            _render(documents).splitlines(keepends=True),
            path.read_text(encoding="utf-8").splitlines(keepends=True),
            fromfile="database", tofile=str(path),
        ))

    def import_file(self, reference: str) -> ChapterFileStatus:
        with self._lock():
            documents = self._documents(reference)
            path, manifest = self._paths(documents[0])
            baseline = self._baseline(manifest)
            if baseline is None or not path.is_file():
                raise ProjectStoreError("Import requires a tracked chapter file; run chapter sync first.")
            if self._snapshot(documents) != baseline["documents"]:
                raise ProjectStoreError("Chapter changed in the database; compare and reconcile before importing.")
            text = path.read_text(encoding="utf-8")
            contents = _parse(text, documents)
            revisions = self.store.edit_chapter_documents(
                reference,
                expected_versions={doc.id: doc.active_version_id for doc in documents},
                contents=contents,
            )
            # Keep the original file hash if an editor saves again during the import.
            # The subsequent edit will remain visible as file-edited, never overwritten.
            imported = {revision.document_id: revision for revision in revisions}
            snapshot = [replace(doc, active_version_id=imported[doc.id].id)
                        if doc.id in imported else doc for doc in documents]
            self._save_baseline(manifest, snapshot, text)
            return self.status(reference)

    def _snapshot(self, documents: list[DocumentRecord]) -> list[dict]:
        return [{"id": doc.id, "version": doc.active_version_id, "title": doc.title,
                 "state": doc.state, "sequence": doc.sequence} for doc in documents]

    def _baseline(self, path: Path) -> dict | None:
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if (value["schema"] != 1 or value["project_id"] != self.store.project_info().project_id
                    or not isinstance(value["documents"], list) or not isinstance(value["file_hash"], str)):
                raise ValueError("Invalid chapter sync baseline")
            return value
        except (ValueError, KeyError, TypeError) as exc:
            raise ProjectStoreError(f"Invalid chapter sync baseline: {path}") from exc

    def _save_baseline(self, path: Path, documents: list[DocumentRecord], text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, json.dumps({
            "schema": 1, "project_id": self.store.project_info().project_id,
            "documents": self._snapshot(documents), "file_hash": _digest(text),
        }, ensure_ascii=False, indent=2) + "\n")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _render(documents: list[DocumentRecord], contents: dict[int, str] | None = None) -> str:
    lines = [f"# {documents[0].title}\n\n"]
    for doc in documents:
        content = doc.content if contents is None else contents[doc.id]
        lines.append(f"<!-- lg:document {doc.id} -->\n{content}\n<!-- /lg:document -->\n\n")
    return "".join(lines)


def _parse(text: str, documents: list[DocumentRecord]) -> dict[int, str]:
    matches = list(re.finditer(r"^<!-- lg:document (\d+) -->\n(.*?)\n<!-- /lg:document -->(?:\n|\Z)", text, re.MULTILINE | re.DOTALL))
    if [int(match[1]) for match in matches] != [doc.id for doc in documents]:
        raise ProjectStoreError("Chapter document markers were changed; preserve their IDs and order.")
    contents = {int(match[1]): match[2] for match in matches}
    # Editors may normalize EOF newlines; document contents and framing stay strict.
    if _render(documents, contents).rstrip("\n") != text.rstrip("\n"):
        raise ProjectStoreError("Edit text inside the chapter document markers; keep the heading and markers unchanged.")
    return contents
