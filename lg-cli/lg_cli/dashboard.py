from __future__ import annotations

from . import __version__
from .config import LGConfig


def render_dashboard(config: LGConfig, *, mode: str, skills_count: int, agents_count: int) -> str:
    width = 78
    title = f" LiteraryGiant v{__version__} "
    top = "╭" + title + "─" * max(0, width - len(title) - 2) + "╮"
    bottom = "╰" + "─" * (width - 2) + "╯"
    body: list[str] = [top]
    body.extend(
        [
            _line(""),
            _line("                         ▐▛███▜▌"),
            _line("                        ▝▜█████▛▘"),
            _line("                          ▘▘ ▝▝"),
            _line(""),
            _line(
                f"  model: {config.model_label} · provider: {config.provider} · mode: {mode}"
            ),
            _line(f"  workspace: {config.workspace}"),
            _line(f"  skills: {skills_count} · subagents: {agents_count} · api: {config.api_key_source}"),
            _split(),
            _two_col("  Reference · Story · Memory · Coding", "Tips for getting started"),
            _two_col("  LiteraryGiant Agent Runtime", "Run /init to create config"),
            _two_col("", ""),
            _two_col("", "What's new"),
            _two_col("", "Added /skills and /agents"),
            _two_col("", "Added reference modes"),
            _two_col("", "Core in core/codex"),
            _two_col("", ""),
            _two_col("", "Useful commands"),
            _two_col("", "/outline /world /write"),
            _two_col("", "/ref /check /code"),
        ]
    )
    body.append(bottom)
    return "\n".join(body)


def _line(text: str) -> str:
    width = 78
    clipped = text[: width - 4]
    return "│ " + clipped.ljust(width - 4) + " │"


def _split() -> str:
    return "│" + "─" * 47 + "┬" + "─" * 28 + "│"


def _two_col(left: str, right: str) -> str:
    return "│ " + left[:45].ljust(45) + " │ " + right[:27].ljust(27) + "│"
