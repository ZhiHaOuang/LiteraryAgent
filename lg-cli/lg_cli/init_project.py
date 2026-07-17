from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import DEFAULT_CONFIG_TEXT


MEMORY_FILES = {
    "STORY_BIBLE.md": "# Story Bible\n\n## Premise\n\n## Canon Facts\n\n## Open Questions\n\n## Do Not Change\n\n",
    "CHARACTERS.md": "# Characters\n\n## Main Cast\n\n## Antagonists\n\n## Supporting Cast\n\n## Character Facts\n\n",
    "LOCATIONS.md": "# Locations\n\n## Key Places\n\n## Place Rules\n\n## Open Questions\n\n",
    "WORLDVIEW.md": "# Worldview\n\n## Rules\n\n## Factions\n\n## Resources\n\n## Limits And Costs\n\n",
    "PLOT_POINTS.md": "# Plot Points\n\n## Main Arc\n\n## Volume Arcs\n\n## Foreshadowing\n\n## Payoff Ledger\n\n",
    "TIMELINE.md": "# Timeline\n\n## Backstory\n\n## Current Story\n\n## Future Setups\n\n",
    "RELATIONSHIPS.md": "# Relationships\n\n## Relationship Map\n\n## Tension Points\n\n## Changes\n\n",
    "STYLE_GUIDE.md": "# Style Guide\n\n## Voice\n\n## Pacing\n\n## Dialogue\n\n## Avoid\n\n",
    "ERRORS.md": "# Errors\n\n## Known Contradictions\n\n## Weak Spots\n\n## Repair Notes\n\n",
}


OUTPUT_FILES = {
    "outline.md": "# Outline\n\n",
    "worldview.md": "# Worldview\n\n",
    "characters.md": "# Characters\n\n",
}


@dataclass(frozen=True)
class InitResult:
    created: list[Path]
    skipped: list[Path]


def init_workspace(workspace: Path) -> InitResult:
    created: list[Path] = []
    skipped: list[Path] = []
    root = workspace / ".literarygiant"
    for directory in [
        root,
        root / "memory",
        root / "output",
        root / "output" / "outlines",
        root / "output" / "chapters",
        root / "output" / "diagrams",
        root / "output" / "reports",
        root / "logs",
        root / "skills",
        root / "subagents",
        workspace / "ReferenceLibrary",
    ]:
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            created.append(directory)
        else:
            skipped.append(directory)

    _write_if_missing(root / "config.toml", DEFAULT_CONFIG_TEXT, created, skipped)
    _write_if_missing(root / "logs" / "agent.log", "", created, skipped)
    _write_if_missing(root / "logs" / "failures.md", "# Failures\n\n", created, skipped)
    _write_if_missing(root / "skills" / "README.md", "# Project Skills\n\nProject-local LG skills can live here.\n", created, skipped)
    _write_if_missing(
        root / "subagents" / "README.md",
        "# Project Subagents\n\nProject-local LG subagent definitions can live here.\n",
        created,
        skipped,
    )

    for name, text in MEMORY_FILES.items():
        _write_if_missing(root / "memory" / name, text, created, skipped)
    for name, text in OUTPUT_FILES.items():
        _write_if_missing(root / "output" / name, text, created, skipped)
    return InitResult(created=created, skipped=skipped)


def _write_if_missing(path: Path, text: str, created: list[Path], skipped: list[Path]) -> None:
    if path.exists():
        skipped.append(path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    created.append(path)
