from __future__ import annotations

import difflib
import fcntl
import hashlib
import json
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
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
        pending = self._recovery(manifest, documents[0])
        if pending is not None and self._committed(pending, documents):
            return ChapterFileStatus(documents[0].slug, path, 'committed-needs-sync')
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
            documents = self._documents(reference)
            _, manifest = self._paths(documents[0])
            if self._recovery(manifest, documents[0]) is not None:
                raise ProjectStoreError('Chapter import recovery is pending; retry chapter import before exporting.')
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
            # Validate and parse one snapshot: an editor may save again while
            # this import runs, and that later edit must remain a future import.
            text = path.read_text(encoding="utf-8")
            pending = self._recovery(manifest, documents[0])
            if pending is not None:
                if self._committed(pending, documents):
                    return self._finish_import(reference, manifest, pending, documents, refresh=True)
                if self._snapshot(documents) != pending['before'] or _digest(text) != pending['file_hash']:
                    raise ProjectStoreError('Chapter import recovery conflicts with later edits; compare before retrying. No content was replayed.')
            if self._snapshot(documents) != baseline["documents"]:
                raise ProjectStoreError("Chapter changed in the database; compare and reconcile before importing.")
            contents = _parse(text, documents)
            pending = pending or {'schema': 1, 'state': 'pending',
                'project_id': self.store.project_info().project_id, 'chapter_id': documents[0].id,
                'origin': 'chapter-file:' + uuid.uuid4().hex, 'before': self._snapshot(documents),
                'file_hash': _digest(text), 'contents': {str(key): _digest(value) for key, value in contents.items()},
                'changed': [doc.id for doc in documents if doc.content != contents[doc.id]]}
            self._save_recovery(manifest, pending)
            try:
                self.store.edit_chapter_documents(reference,
                    expected_versions={doc.id: doc.active_version_id for doc in documents},
                    contents=contents, origin=pending['origin'])
            except (OSError, ValueError):
                # The store may have committed before its derived index failed.
                if self._committed(pending, self._documents(reference)):
                    return ChapterFileStatus(documents[0].slug, path, 'committed-needs-sync')
                raise
            current = self._documents(reference)
            if not self._committed(pending, current):
                raise ProjectStoreError('Chapter import completed but recovery conflicts with later database edits; inspect the saved versions.')
            return self._finish_import(reference, manifest, pending, current, refresh=False)

    def _recovery_path(self, manifest: Path) -> Path:
        path = manifest.with_suffix('.import.json')
        if path.is_symlink() or not path.resolve().is_relative_to(self.store.workspace.resolve()):
            raise ProjectStoreError('Chapter import recovery must remain inside this project.')
        return path

    def _save_recovery(self, manifest: Path, value: dict) -> None:
        path = self._recovery_path(manifest)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')

    def _recovery(self, manifest: Path, chapter: DocumentRecord) -> dict | None:
        path = self._recovery_path(manifest)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            if (value['schema'] != 1 or value['project_id'] != self.store.project_info().project_id
                    or value['chapter_id'] != chapter.id or value['state'] not in {'pending', 'complete'}
                    or not isinstance(value['before'], list) or not isinstance(value['contents'], dict)
                    or not isinstance(value['changed'], list)
                    or not re.fullmatch(r'chapter-file:[0-9a-f]{32}', value['origin'])
                    or not re.fullmatch(r'[0-9a-f]{64}', value['file_hash'])):
                raise ValueError()
            ids = [row['id'] for row in value['before']]
            if (not ids or ids[0] != chapter.id or any(type(identity) is not int or identity < 1 for identity in ids)
                    or any(set(row) != {'id', 'version', 'title', 'state', 'sequence'}
                        or not isinstance(row['title'], str) or not isinstance(row['state'], str)
                        or row['version'] is not None and (type(row['version']) is not int or row['version'] < 1)
                        or row['sequence'] is not None and type(row['sequence']) is not int for row in value['before'])
                    or len(ids) != len(set(ids)) or set(map(str, ids)) != set(value['contents'])
                    or not set(value['changed']) <= set(ids)
                    or any(not re.fullmatch(r'[0-9a-f]{64}', digest) for digest in value['contents'].values())):
                raise ValueError()
            return value if value['state'] == 'pending' else None
        except (ValueError, KeyError, TypeError) as exc:
            raise ProjectStoreError('Invalid chapter import recovery record; inspect before retrying.') from exc

    def _committed(self, pending: dict, documents: list[DocumentRecord]) -> bool:
        before = {row['id']: row for row in pending['before']}
        if set(before) != {doc.id for doc in documents}:
            return False
        for doc, current in zip(documents, self._snapshot(documents)):
            previous = before[doc.id]
            if ({**current, 'version': previous['version']} != previous
                    or _digest(doc.content) != pending['contents'][str(doc.id)]):
                return False
            if doc.id not in pending['changed']:
                if doc.active_version_id != previous['version']:
                    return False
            else:
                version = self.store.get_version(doc.id, doc.active_version_number)
                if (version.id == previous['version'] or version.metadata.get('origin') != pending['origin']
                        or version.metadata.get('previous_version_id') != previous['version']):
                    return False
        return True

    def _finish_import(self, reference, manifest, pending, documents, *, refresh):
        try:
            if refresh:
                from .story_index import refresh_index
                refresh_index(self.store)
            # Original file hash preserves any editor save made after the import.
            self._save_baseline(manifest, documents, '', file_hash=pending['file_hash'])
            self._save_recovery(manifest, {**pending, 'state': 'complete'})
        except (OSError, ValueError):
            path, _ = self._paths(documents[0])
            return ChapterFileStatus(documents[0].slug, path, 'committed-needs-sync')
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

    def _save_baseline(self, path: Path, documents: list[DocumentRecord], text: str, *, file_hash: str | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, json.dumps({
            "schema": 1, "project_id": self.store.project_info().project_id,
            "documents": self._snapshot(documents), "file_hash": file_hash if file_hash is not None else _digest(text),
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
