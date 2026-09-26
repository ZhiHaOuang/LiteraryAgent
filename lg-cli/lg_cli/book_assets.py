"""Book-owned assets, with explicit, reversible legacy layout migration."""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
from pathlib import Path

from .output_writer import _atomic_text


def print_plan(plan: dict, *, json_mode: bool = False) -> None:
    if json_mode:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return
    if plan['status'] == 'complete':
        print(f"Book assets organized: {len(plan['moves'])} files. No files deleted.")
        print("Mapping: .literarygiant/assets.json")
        return
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    table = Table('Current path', 'Organized path', title='Book asset migration', box=None)
    for move in plan['moves']:
        table.add_row(Text(move['from']), Text(move['to']))
    Console().print(table)
    print(f"{len(plan['moves'])} files. Use project organize --apply to proceed. No files will be deleted.")


def manifest_path(workspace: Path) -> Path:
    return workspace / ".literarygiant" / "assets.json"


def organized(workspace: Path) -> bool:
    path = manifest_path(workspace)
    if not path.exists():
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != "lg.assets.v1" or data.get("status") != "complete":
        raise ValueError("Book asset migration is incomplete; rerun project organize --apply")
    return True


def asset_root(workspace: Path, kind: str) -> Path:
    legacy = workspace / ".literarygiant" / kind
    if not organized(workspace) and legacy.exists():
        result = legacy
    else:
        result = workspace / "ReferenceLibrary" / {"memory": "bible", "output": "drafts", "analysis": "analyses"}[kind]
    if not result.resolve().is_relative_to(workspace.resolve()):
        raise ValueError("Book assets must remain inside this project")
    return result


def resolve_artifact(workspace: Path, value: str) -> str:
    path = manifest_path(workspace)
    if not path.exists():
        return value
    data = json.loads(path.read_text(encoding="utf-8"))
    for move in data.get("moves", []):
        old = workspace / move["from"]
        if value in {str(old), move["from"]}:
            target = workspace / move["to"]
            if not target.resolve().is_relative_to(workspace.resolve()):
                raise ValueError("Asset mapping escapes the book")
            return str(target)
    return value


def organize_book(workspace: Path, *, apply: bool = False) -> dict:
    root = workspace / ".literarygiant"
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return _organize_book(workspace, apply=apply)
    finally:
        os.close(fd)


def _organize_book(workspace: Path, *, apply: bool = False) -> dict:
    from .project_store import ProjectStore
    from .init_project import MEMORY_FILES, OUTPUT_FILES

    workspace = workspace.resolve()
    project = ProjectStore(workspace).project_info()
    marker = manifest_path(workspace)
    if marker.is_symlink():
        raise ValueError("Refusing symlink asset manifest")
    if marker.exists():
        plan = json.loads(marker.read_text(encoding="utf-8"))
        if plan.get("schema") != "lg.assets.v1" or plan.get("project_id") != project.project_id:
            raise ValueError("Asset manifest does not belong to this book")
        if plan.get("status") == "complete":
            return plan
    else:
        moves = []
        roots = [(workspace / ".literarygiant" / name, name) for name in ("memory", "output", "analysis")]
        roots.append((workspace / "ReferenceLibrary", "reference"))
        for root, kind in roots:
            if root.is_symlink():
                raise ValueError(f"Refusing symlink asset root: {root}")
            paths = sorted(root.glob("*")) if kind == "reference" else sorted(root.rglob("*"))
            for source in paths:
                if source.is_symlink():
                    raise ValueError(f"Refusing symlink asset: {source}")
                if not source.is_file():
                    continue
                relative = source.relative_to(root)
                content = source.read_bytes()
                if kind == "reference":
                    name = source.name
                    group = ("plans/chapters" if name.endswith("-execution-plan.md") else
                             "reviews/editorial" if "editorial-corrections" in name else
                             "reviews/discussions" if "review-challenge" in name else
                             "plans" if name == "production-plan.json" else
                             "bible" if name == "accepted-timeline.json" else "sources/imported")
                elif kind == "memory":
                    group = "archive/placeholders" if content == MEMORY_FILES.get(source.name, "").encode() else "bible"
                elif kind == "analysis":
                    group = "analyses"
                elif content == OUTPUT_FILES.get(source.name, "").encode():
                    group = "archive/placeholders/output"
                else:
                    group = "archive" if relative.parts[0] == "exports" else "drafts"
                destination = workspace / "ReferenceLibrary" / group / relative
                if kind == "output" and relative.parts[0] == "conversations":
                    destination = workspace / ".literarygiant" / "conversation-artifacts" / relative.name
                moves.append({"from": str(source.relative_to(workspace)),
                              "to": str(destination.relative_to(workspace)),
                              "sha256": hashlib.sha256(content).hexdigest()})
        plan = {"schema": "lg.assets.v1", "project_id": project.project_id,
                "status": "planned", "moves": moves}
    targets = set()
    for move in plan["moves"]:
        source, target = workspace / move["from"], workspace / move["to"]
        if source.is_symlink() or target.is_symlink():
            raise ValueError("Refusing symlink migration path")
        if any(not p.resolve().is_relative_to(workspace) for p in (source, target)):
            raise ValueError("Asset path escapes the book")
        if target in targets:
            raise ValueError(f"Duplicate migration target: {target}")
        targets.add(target)
        if source.exists() and target.exists():
            raise ValueError(f"Destination exists; no files changed: {target}")
        existing = source if source.exists() else target
        if not existing.is_file() or hashlib.sha256(existing.read_bytes()).hexdigest() != move["sha256"]:
            raise ValueError(f"Asset changed since migration was planned: {source}")
    if not apply:
        return plan
    _atomic_text(marker, json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    for move in plan["moves"]:
        source, target = workspace / move["from"], workspace / move["to"]
        if source.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)
    plan["status"] = "complete"
    _atomic_text(marker, json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    index = workspace / "ReferenceLibrary" / "README.md"
    if not index.exists():
        _atomic_text(index, "# Book reference library\n\n"
            "- sources/: external reference material; searched by KnowledgeGateway.\n"
            "- bible/: author notes and timeline records; database remains authoritative.\n"
            "- plans/: book plan and chapter execution plans.\n"
            "- analyses/: source-bound chapter/volume reviews, candidates and adjudications.\n"
            "- reviews/: editorial corrections and review discussions.\n"
            "- drafts/: generated working artifacts, not accepted manuscript.\n"
            "- archive/: preserved legacy placeholders; excluded from retrieval.\n\n"
            "Directories are created on use. Manuscript: ../manuscript/chapters/.\n"
            "Reading exports: ../exports/. Runtime, database and conversations: ../.literarygiant/.\n"
            "Migration provenance: ../.literarygiant/assets.json (paths and SHA-256).\n")
    return plan
