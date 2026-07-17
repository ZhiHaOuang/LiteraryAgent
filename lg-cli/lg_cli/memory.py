from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


DEFAULT_MEMORY_FILES = [
    "STORY_BIBLE.md",
    "CHARACTERS.md",
    "LOCATIONS.md",
    "WORLDVIEW.md",
    "TIMELINE.md",
    "PLOT_POINTS.md",
    "RELATIONSHIPS.md",
    "STYLE_GUIDE.md",
    "ERRORS.md",
]


@dataclass(frozen=True)
class MemoryFile:
    path: Path
    exists: bool
    content: str
    truncated: bool


@dataclass(frozen=True)
class MemoryContext:
    files: list[MemoryFile]
    warnings: list[str]
    total_chars: int

    @property
    def found_count(self) -> int:
        return sum(1 for item in self.files if item.exists)

    def to_prompt_text(self) -> str:
        if not self.files:
            return "No memory files were discovered."
        parts: list[str] = []
        for item in self.files:
            rel = _display_path(item.path)
            if not item.exists:
                parts.append(f"### {rel}\nMissing.")
                continue
            suffix = "\n\n[truncated]" if item.truncated else ""
            parts.append(f"### {rel}\n{item.content.strip() or '(empty)'}{suffix}")
        if self.warnings:
            parts.append("### Memory Warnings\n" + "\n".join(f"- {warning}" for warning in self.warnings))
        return "\n\n".join(parts)


def read_memory_context(workspace: Path, *, max_total_chars: int = 16000, max_file_chars: int = 3000) -> MemoryContext:
    root = workspace / ".literarygiant" / "memory"
    warnings: list[str] = []
    files: list[MemoryFile] = []
    remaining = max_total_chars
    if not root.exists():
        warnings.append(f"Memory root is missing: {root}. Run `lg init` to create it.")

    for name in DEFAULT_MEMORY_FILES:
        path = root / name
        item, remaining = _read_memory_file(path, remaining=remaining, max_file_chars=max_file_chars)
        files.append(item)

    learnings = workspace / ".learnings"
    if learnings.exists() and learnings.is_dir() and remaining > 0:
        for path in sorted(learnings.glob("*.md"))[:8]:
            item, remaining = _read_memory_file(path, remaining=remaining, max_file_chars=max_file_chars)
            files.append(item)
            if remaining <= 0:
                warnings.append("Memory context budget exhausted while reading .learnings.")
                break
    elif not learnings.exists():
        warnings.append(f"Optional learnings root is missing: {learnings}.")

    total_chars = sum(len(item.content) for item in files)
    return MemoryContext(files=files, warnings=warnings, total_chars=total_chars)


def _read_memory_file(path: Path, *, remaining: int, max_file_chars: int) -> tuple[MemoryFile, int]:
    if not path.exists() or remaining <= 0:
        return MemoryFile(path=path, exists=path.exists(), content="", truncated=False), remaining
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return MemoryFile(path=path, exists=True, content=f"[read error: {exc}]", truncated=False), remaining
    limit = min(max_file_chars, remaining)
    truncated = len(raw) > limit
    content = raw[:limit]
    return MemoryFile(path=path, exists=True, content=content, truncated=truncated), remaining - len(content)


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)
