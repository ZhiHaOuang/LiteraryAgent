from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class OutputWriteResult:
    output_path: Path
    latest_path: Path


OUTPUT_DIRECTORIES = {
    "outline": "plans/outlines",
    "world": "bible/worlds",
    "character": "bible/characters",
    "plot": "plans/plots",
    "write": "drafts/chapters",
    "check": "reviews/reports",
    "ref": "sources/research",
    "plan": "plans",
    "scene-plan": "plans/scenes",
    "bible-proposal": "bible/proposals",
    "candidate": "drafts/candidates",
}

RUNTIME_DIRECTORIES = {
    "chat": "conversation-artifacts",
    "dry-run": "previews",
    "code": "code-artifacts",
}

SPECIALIST_OUTPUTS = {
    "reference": "ref", "worldbuilding": "world", "character": "character",
    "plot": "plot", "critic": "check", "continuity": "check", "style": "check",
    "memory": "bible-proposal", "chapter": "write",
}


def write_workflow_output(
    workspace: Path,
    command: str,
    content: str,
    *,
    output_root: Path | None = None,
    run_id: str | None = None,
    stage_id: str | None = None,
) -> OutputWriteResult:
    workspace = workspace.resolve()
    reference = workspace / "ReferenceLibrary"
    legacy_roots = {workspace / ".literarygiant/output", reference / "drafts", reference}
    root = (output_root.resolve() if output_root else reference)
    if root in legacy_roots:
        root = reference
    if not root.is_relative_to(reference.resolve()):
        raise ValueError("Workflow assets must be classified inside this book's ReferenceLibrary")
    if command in RUNTIME_DIRECTORIES:
        directory = workspace / ".literarygiant" / RUNTIME_DIRECTORIES[command]
    elif command in OUTPUT_DIRECTORIES:
        directory = root / OUTPUT_DIRECTORIES[command]
    else:
        raise ValueError(f"Unclassified workflow artifact: {command}")
    if not directory.resolve().is_relative_to(workspace):
        raise ValueError("Workflow artifact path escapes this book")
    if run_id and (Path(run_id).name != run_id or run_id in {".", ".."}):
        raise ValueError("Expected a local run ID")
    if stage_id:
        if not run_id or Path(stage_id).name != stage_id or stage_id in {".", ".."}:
            raise ValueError("Stage artifacts require local run and stage IDs")
        path = directory / "stages" / run_id / f"{stage_id}.md"
        if not path.resolve().is_relative_to(workspace):
            raise ValueError("Stage artifact path escapes this book")
        _atomic_text(path, f"# Working specialist artifact\n\nRun: {run_id}\nStage: {stage_id}\n"
                     "Status: provisional; not Canonical or accepted manuscript.\n\n" + content.rstrip() + "\n")
        return OutputWriteResult(output_path=path, latest_path=path)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    suffix = f"-{run_id}" if run_id else ""
    output_path = directory / f"{command}-{stamp}{suffix}.md"
    counter = 2
    while output_path.exists():
        output_path = directory / f"{command}-{stamp}{suffix}-{counter}.md"
        counter += 1
    latest_path = directory / f"{command}.latest.md"
    normalized = content.rstrip() + "\n"
    _atomic_text(output_path, normalized)
    _atomic_text(latest_path, normalized)
    return OutputWriteResult(output_path=output_path, latest_path=latest_path)


def write_outline_output(workspace: Path, content: str, *, output_root: Path | None = None) -> OutputWriteResult:
    return write_workflow_output(
        workspace,
        "outline",
        content,
        output_root=output_root,
    )


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)
