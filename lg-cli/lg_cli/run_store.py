from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .events import RunEvent
from .project_store import ProjectStore, ProjectStoreError


RUN_SCHEMA = "lg.run.v2"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RunHandle:
    run_id: str
    directory: Path
    manifest_path: Path
    events_path: Path


class RunStore:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.root = workspace / ".literarygiant" / "runs"

    def create(
        self,
        *,
        workflow: str,
        command: str,
        request: str,
        provider: str,
        model: str,
        resumed_from: str | None = None,
    ) -> RunHandle:
        self.root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{stamp}-{uuid.uuid4().hex[:8]}"
        directory = self.root / run_id
        (directory / "stages").mkdir(parents=True, exist_ok=False)
        handle = RunHandle(
            run_id=run_id,
            directory=directory,
            manifest_path=directory / "run.json",
            events_path=directory / "events.jsonl",
        )
        payload = {
            "schema_version": RUN_SCHEMA,
            "run_id": run_id,
            "workflow": workflow,
            "command": command,
            "request": request,
            "status": "running",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "workspace": str(self.workspace),
            "provider": provider,
            "model": model or "auto",
            "resumed_from": resumed_from,
            "completed_stages": [],
            "artifact_path": None,
            "latest_path": None,
            "error": None,
        }
        _atomic_json(handle.manifest_path, payload)
        return handle

    def append_event(self, handle: RunHandle, event: RunEvent) -> None:
        handle.events_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with handle.events_path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")

    def write_stage(
        self,
        handle: RunHandle,
        *,
        stage_id: str,
        content: str,
        metadata: dict[str, Any],
    ) -> Path:
        safe_id = "".join(char if char.isalnum() or char in "-_" else "-" for char in stage_id)
        path = handle.directory / "stages" / f"{safe_id}.md"
        _atomic_text(path, content)
        _atomic_json(path.with_suffix(".json"), metadata)
        manifest = self.load(handle.run_id)
        completed = list(manifest.get("completed_stages", []))
        if stage_id not in completed:
            completed.append(stage_id)
        manifest["completed_stages"] = completed
        manifest["updated_at"] = utc_now()
        _atomic_json(handle.manifest_path, manifest)
        return path

    def write_stage_prompt(self, handle: RunHandle, *, stage_id: str, prompt: str) -> Path:
        safe_id = "".join(char if char.isalnum() or char in "-_" else "-" for char in stage_id)
        path = handle.directory / "stages" / f"{safe_id}.prompt.md"
        _atomic_text(path, prompt)
        return path

    def read_stage(self, run_id: str, stage_id: str) -> str | None:
        safe_id = "".join(char if char.isalnum() or char in "-_" else "-" for char in stage_id)
        path = self.root / run_id / "stages" / f"{safe_id}.md"
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def finalize(
        self,
        handle: RunHandle,
        *,
        status: str,
        artifact_path: Path | None = None,
        latest_path: Path | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        manifest = self.load(handle.run_id)
        manifest.update(
            {
                "status": status,
                "updated_at": utc_now(),
                "completed_at": utc_now(),
                "artifact_path": str(artifact_path) if artifact_path else None,
                "latest_path": str(latest_path) if latest_path else None,
                "error": error,
            }
        )
        _atomic_json(handle.manifest_path, manifest)
        last_run = self.workspace / ".literarygiant" / "logs" / "last_run.json"
        _atomic_json(last_run, manifest)
        return manifest

    def read_stage_metadata(self, run_id: str, stage_id: str) -> dict[str, Any]:
        safe_id = "".join(char if char.isalnum() or char in "-_" else "-" for char in stage_id)
        path = self.root / run_id / "stages" / f"{safe_id}.json"
        if not path.exists():
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Invalid stage metadata")
        return value

    def load(self, run_id: str) -> dict[str, Any]:
        path = self.root / run_id / "run.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise FileNotFoundError(f"LG run not found: {run_id}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"invalid LG run manifest: {path}")
        from .book_assets import resolve_artifact

        for key in ("artifact_path", "latest_path"):
            if value.get(key):
                value[key] = resolve_artifact(self.workspace, value[key])
        return value

    def handle(self, run_id: str) -> RunHandle:
        directory = self.root / run_id
        if not (directory / "run.json").exists():
            raise FileNotFoundError(f"LG run not found: {run_id}")
        return RunHandle(run_id, directory, directory / "run.json", directory / "events.jsonl")

    def adopt(self, run_id: str, *, slug: str, kind: str, title: str, accept: bool = False) -> dict:
        if Path(run_id).name != run_id or run_id in {".", ".."}:
            raise ValueError("Expected a local run ID")
        manifest = self.load(run_id)
        if manifest.get("status") != "completed" or not manifest.get("artifact_path"):
            raise ValueError("Only completed runs with an artifact can be adopted")
        artifact = Path(manifest["artifact_path"]).resolve()
        if not artifact.is_relative_to(self.workspace.resolve()):
            raise ValueError("Run artifact must be inside the current book")
        content = artifact.read_text(encoding="utf-8")
        if not content.strip():
            raise ValueError("Run artifact is empty")
        project = ProjectStore(self.workspace)
        metadata = {"run_id": run_id, "artifact_path": str(artifact)}
        try:
            document = project.get_document(slug)
        except ProjectStoreError:
            if kind in {"chapter", "scene"}:
                raise ValueError("Create the chapter or scene container before adopting manuscript text") from None
            document = project.create_document(
                kind=kind, slug=slug, title=title, content=content,
                state="accepted" if accept else "draft", metadata=metadata,
                reason=f"Adopted from LG run {run_id}",
            )
            version = project.get_version(document.id, 1)
        else:
            if document.kind != kind:
                raise ValueError("Existing document kind differs; choose a different slug")
            version = next((v for v in project.list_versions(document.id, limit=1000)
                            if v.metadata.get("run_id") == run_id
                            or (v.version_number == 1 and document.metadata.get("run_id") == run_id)), None)
            if version is None:
                version = project.create_version(document.id, content=content, metadata=metadata,
                    reason=f"Adopted from LG run {run_id}")
        if accept:
            project.accept_version(document.id, version.version_number)
        return {"run_id": run_id, "slug": document.slug, "version": version.version_number,
                "active": project.get_document(document.id).active_version_number == version.version_number}

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        records: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*/run.json"), reverse=True):
            try:
                value = self.load(path.parent.name)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                records.append(value)
            if len(records) >= limit:
                break
        return records

    def read_events(self, run_id: str) -> list[dict[str, Any]]:
        path = self.root / run_id / "events.jsonl"
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                events.append(value)
        return events


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
