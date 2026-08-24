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
    "outline": "outlines",
    "world": "worlds",
    "character": "characters",
    "plot": "plots",
    "write": "chapters",
    "check": "reports",
    "ref": "references",
    "chat": "conversations",
    "code": "code",
    "plan": "plans",
    "candidate": "candidates",
}


def write_workflow_output(
    workspace: Path,
    command: str,
    content: str,
    *,
    output_root: Path | None = None,
    run_id: str | None = None,
) -> OutputWriteResult:
    root = output_root or workspace / ".literarygiant" / "output"
    directory = root / OUTPUT_DIRECTORIES.get(command, f"{command}s")
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    suffix = f"-{run_id}" if run_id else ""
    output_path = directory / f"{command}-{stamp}{suffix}.md"
    counter = 2
    while output_path.exists():
        output_path = directory / f"{command}-{stamp}{suffix}-{counter}.md"
        counter += 1
    latest_path = root / f"{command}.latest.md"
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
