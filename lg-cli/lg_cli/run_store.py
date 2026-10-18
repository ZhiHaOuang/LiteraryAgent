from __future__ import annotations

import hashlib
import fcntl
import json
import os
import re
import uuid
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .events import RunEvent
from .generation_sources import bind_generation_sources, text_sha
from .project_store import ProjectStore, ProjectStoreError


RUN_SCHEMA = "lg.run.v2"


def process_owner(pid=None):
    own=pid is None
    pid=os.getpid() if own else pid
    try:
        fields=(Path('/proc')/str(pid)/'stat').read_text().rsplit(')',1)[1].split()
        if fields[0]=='Z':return None
        return {'pid':pid,'startTicks':fields[19],
            'bootId':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    except (OSError,IndexError):
        if not own:
            try:
                os.kill(pid,0)
            except ProcessLookupError:
                return None
            except PermissionError:
                raise ValueError('Cannot verify run process ownership') from None
            raise ValueError('Cannot verify live run process identity') from None
        return None


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
            "owner":process_owner(),
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
        generation_sources: dict | None = None,
    ) -> Path:
        if generation_sources is not None:
            with bind_generation_sources(generation_sources):
                metadata = {**metadata, 'generation_sources': json.loads(json.dumps(generation_sources)),
                    'generation_output_sha256': text_sha(content)}
        safe_id = "".join(char if char.isalnum() or char in "-_" else "-" for char in stage_id)
        path = handle.directory / "stages" / f"{safe_id}.md"
        _atomic_text(path, content)
        _atomic_private_text(path.with_suffix(".json"), json.dumps(metadata, ensure_ascii=False, indent=2) + '\n')
        manifest = self.load(handle.run_id)
        completed = list(manifest.get("completed_stages", []))
        if stage_id not in completed:
            completed.append(stage_id)
        manifest["completed_stages"] = completed
        manifest["updated_at"] = utc_now()
        _atomic_json(handle.manifest_path, manifest)
        return path

    def write_generation_sources(self, handle: RunHandle, *, stage_id: str, receipt: dict) -> None:
        """Persist frozen inputs before model execution; no output is implied yet."""
        with bind_generation_sources(receipt):
            value = json.loads(json.dumps(receipt))
        if value.get('run_id') != handle.run_id or value.get('stage_id') != stage_id:
            raise ValueError('Generation source receipt does not match the run stage.')
        safe_id = ''.join(char if char.isalnum() or char in '-_' else '-' for char in stage_id)
        _atomic_private_text(handle.directory / 'stages' / f'{safe_id}.sources.json',
                             json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        manifest = self.load(handle.run_id)
        manifest['generation_sources'] = value
        manifest['updated_at'] = utc_now()
        _atomic_json(handle.manifest_path, manifest)

    def write_stage_prompt(self, handle: RunHandle, *, stage_id: str, prompt: str,
                           instructions: str | None = None) -> Path:
        safe_id = "".join(char if char.isalnum() or char in "-_" else "-" for char in stage_id)
        path = handle.directory / "stages" / f"{safe_id}.prompt.md"
        _atomic_text(path, prompt)
        if instructions is not None:
            _atomic_text(handle.directory / 'stages' / f'{safe_id}.instructions.md', instructions)
        return path

    def write_stage_schema(self, handle: RunHandle, *, stage_id: str, name: str, text: str) -> Path:
        if not name or name in {'.', '..'} or Path(name).name != name:
            raise ValueError('Expected a schema basename')
        safe_id = "".join(char if char.isalnum() or char in "-_" else "-" for char in stage_id)
        path = handle.directory / 'stages' / f'{safe_id}.schema' / name
        _atomic_private_text(path,text)
        return path

    def read_stage(self, run_id: str, stage_id: str) -> str | None:
        safe_id = "".join(char if char.isalnum() or char in "-_" else "-" for char in stage_id)
        path = self._path(run_id,'stages/'+safe_id+'.md')
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
        if artifact_path is not None:
            if artifact_path.resolve().is_relative_to(self.workspace.resolve()):
                try:
                    manifest['artifact_sha256'] = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
                except OSError:
                    if manifest.get('generation_sources') is not None:
                        raise
            elif manifest.get('generation_sources') is not None:
                raise ValueError('Generation source artifacts must stay inside the current book.')
        _atomic_json(handle.manifest_path, manifest)
        last_run = self.workspace / ".literarygiant" / "logs" / "last_run.json"
        _atomic_json(last_run, manifest)
        return manifest

    def read_stage_metadata(self, run_id: str, stage_id: str) -> dict[str, Any]:
        safe_id = "".join(char if char.isalnum() or char in "-_" else "-" for char in stage_id)
        path = self._path(run_id,'stages/'+safe_id+'.json')
        if not path.exists():
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Invalid stage metadata")
        return value

    def _path(self,run_id,name):
        if not isinstance(run_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',run_id):
            raise ValueError('Expected a local run ID')
        path=self.root/run_id/name
        if not path.resolve().is_relative_to(self.workspace.resolve()):
            raise ValueError('Run state must stay inside the current book')
        return path

    def assert_resumable(self,run_id,*,confirm_stopped=False):
        value=self.load(run_id)
        if value.get('status')=='running':
            owner=value.get('owner')
            if owner is None:
                if not confirm_stopped:
                    raise ValueError('Legacy run has no verified owner; confirm it is stopped with --confirm-stopped before resuming')
            elif not isinstance(owner,dict) or type(owner.get('pid')) is not int or owner['pid']<1:
                raise ValueError('Invalid run owner; inspect saved run state')
            elif (not isinstance(owner.get('startTicks'),str) or not owner['startTicks'].isdigit()
                    or not isinstance(owner.get('bootId'),str) or not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',owner['bootId'])):
                raise ValueError('Invalid run process identity')
            elif process_owner(owner['pid'])=={key:owner[key] for key in ('pid','startTicks','bootId')}:
                raise ValueError('Run is still executing; stop it before resuming')
        return value

    @contextmanager
    def resume_lock(self,run_id):
        path=self._path(run_id,'resume.lock')
        fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('This run is already being resumed; stop or wait for that recovery') from None
            yield
        finally:
            os.close(fd)

    def load(self, run_id: str) -> dict[str, Any]:
        path = self._path(run_id,'run.json')
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise FileNotFoundError(f"LG run not found: {run_id}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"invalid LG run manifest: {path}")
        if value.get('run_id')!=run_id:
            raise ValueError('Run manifest identity does not match its directory')
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
        if not (self.root/run_id/'run.json').resolve().is_relative_to(self.workspace.resolve()):
            raise ValueError('Run manifest must be inside the current book')
        manifest = self.load(run_id)
        if manifest.get("status") != "completed" or not manifest.get("artifact_path"):
            raise ValueError("Only completed runs with an artifact can be adopted")
        artifact = Path(manifest["artifact_path"]).resolve()
        if not artifact.is_relative_to(self.workspace.resolve()):
            raise ValueError("Run artifact must be inside the current book")
        artifact_bytes = artifact.read_bytes()
        # Preserve read_text's historical universal-newline import semantics.
        content = artifact_bytes.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
        if not content.strip():
            raise ValueError("Run artifact is empty")
        receipt = manifest.get('generation_sources')
        artifact_hash = manifest.get('artifact_sha256')
        if ((artifact_hash is not None and artifact_hash != hashlib.sha256(artifact_bytes).hexdigest())
                or (receipt is not None and artifact_hash is None)):
            raise ValueError('Run artifact changed or has no verified output hash; source receipt cannot be inherited')
        if receipt is not None:
            if not isinstance(receipt, dict) or receipt.get('run_id') != run_id:
                raise ValueError('Generation source receipt does not match the artifact run.')
            stage_id = receipt.get('stage_id')
            if stage_id is not None:
                if not isinstance(stage_id, str):
                    raise ValueError('Invalid generation source stage identity.')
                safe_id = ''.join(char if char.isalnum() or char in '-_' else '-' for char in stage_id)
                snapshot = self._path(run_id, f'stages/{safe_id}.sources.json')
                if snapshot.exists() and json.loads(snapshot.read_text(encoding='utf-8')) != receipt:
                    raise ValueError('Generation source receipt does not match the saved stage inputs.')
        with bind_generation_sources(receipt, include_non_candidate=True):
            return self._adopt_content(run_id, slug=slug, kind=kind, title=title, accept=accept,
                                       artifact=artifact, content=content)

    def _adopt_content(self, run_id: str, *, slug: str, kind: str, title: str, accept: bool,
                       artifact: Path, content: str) -> dict:
        project = ProjectStore(self.workspace)
        metadata = {"run_id": run_id, "artifact_path": str(artifact),
            "content_sha256":hashlib.sha256(content.encode('utf-8')).hexdigest()}
        from .story_index import artifact_graph
        graph = artifact_graph(self.workspace, artifact)
        if graph is not None:
            metadata['story_index'] = graph
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
                    reason=f"Adopted from LG run {run_id}",expected_version_id=document.active_version_id)
            elif version.content != content:
                raise ValueError('Run artifact changed after import; inspect the saved candidate and source before importing again')
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
            except (OSError, ValueError):
                continue
            if isinstance(value, dict):
                records.append(value)
            if len(records) >= limit:
                break
        return records

    def read_events(self, run_id: str) -> list[dict[str, Any]]:
        path = self._path(run_id,'events.jsonl')
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


def _atomic_private_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        stream.write(text)
    os.replace(temp,path)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
