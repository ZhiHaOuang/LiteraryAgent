from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class OutputWriteResult:
    output_path: Path
    latest_path: Path


def write_outline_output(workspace: Path, content: str, *, output_root: Path | None = None) -> OutputWriteResult:
    output_root = output_root or workspace / ".literarygiant" / "output"
    outlines = output_root / "outlines"
    outlines.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_path = outlines / f"outline-{stamp}.md"
    counter = 2
    while output_path.exists():
        output_path = outlines / f"outline-{stamp}-{counter}.md"
        counter += 1
    output_path.write_text(content, encoding="utf-8")
    latest_path = output_root / "outline.latest.md"
    latest_path.write_text(content, encoding="utf-8")
    return OutputWriteResult(output_path=output_path, latest_path=latest_path)
