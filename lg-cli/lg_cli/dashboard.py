from __future__ import annotations

import io
import os
import shutil
import sys

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

from . import __version__
from .config import LGConfig
from .project_store import ProjectStore
from .run_store import RunStore
from .slime_animation import slime_mark


def render_dashboard(config: LGConfig, *, mode: str, skills_count: int, agents_count: int, terminal_width: int | None = None, animation_time: float | None = None, compact: bool = False) -> str:
    terminal_width = terminal_width or shutil.get_terminal_size((96, 24)).columns
    width = max(12, min(88, terminal_width))
    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
    console = Console(
        record=True,
        file=io.StringIO(),
        width=width,
        color_system="truecolor" if color else None,
        force_terminal=color,
    )

    welcome = "What shall we create today?"
    model = "DeepSeek Flash" if config.provider == "deepseek" and config.model_label == "deepseek-flash" else f"{config.model_label} | {config.provider}"
    auth = ("ChatGPT subscription configured" if config.auth_mode == "chatgpt" else "API profile configured") if config.credentials_configured else "No model configured | /auth"
    mark = Text("LG", style="#dc795f") if os.environ.get("TERM") == "dumb" else slime_mark(None if compact else animation_time)
    if compact:
        lines = mark.split("\n")
        occupied = [line for line in lines if line.plain.strip()]
        if occupied:
            left = min(len(line.plain) - len(line.plain.lstrip()) for line in occupied)
            right = max(len(line.plain.rstrip()) for line in occupied)
            mark = Text("\n").join(line[left:right] for line in occupied)
    content = Group(
        Align.center(Text(welcome, style="bold")),
        Align.center(mark),
        Text(""),
        Align.center(Text(model)),
        Align.center(Text(auth, style="dim")),
        Align.center(Text(str(config.workspace), style="dim", overflow="fold")),
    )
    console.print(
        Panel(
            content,
            title=f" LiteraryGiant {__version__} ",
            title_align="left",
            border_style="#dc795f",
            box=box.ASCII if os.environ.get("TERM") == "dumb" else box.ROUNDED,
            padding=(1, 1),
        )
    )
    return console.file.getvalue().rstrip()


def _story_status(config: LGConfig) -> dict[str, str]:
    store = ProjectStore(config.workspace)
    latest = RunStore(config.workspace).list_runs(limit=1)
    if latest:
        run = latest[0]
        last_run = f"{run.get('status', 'unknown')}  |  {run.get('command', '-')}  |  {run.get('run_id', '-')}"
    else:
        last_run = "none"

    if not store.initialized:
        return {
            "project": "not initialized",
            "manuscript": "0 chapters  |  0 scenes",
            "bible": "0 Canonical facts  |  0 proposals",
            "last_run": last_run,
            "next": 'literary project create "My Novel" --path .',
        }

    summary = store.summary()
    next_command = "literary review consistency"
    if summary["chapters"] == 0:
        next_command = 'literary chapter create chapter-1 "Chapter One"'
    elif summary["scenes"] == 0:
        next_command = 'literary scene create opening "Opening" --chapter chapter-1'
    elif summary["candidate_plans"]:
        next_command = "literary scene list"
    elif summary["pending_proposals"]:
        next_command = "literary bible proposal list"
    elif summary["candidate_versions"]:
        next_command = "literary version list <document>"

    return {
        "project": str(summary["name"]),
        "manuscript": (
            f"{_count_label(summary['chapters'], 'chapter')}  |  "
            f"{_count_label(summary['scenes'], 'scene')}  |  "
            f"{_count_label(summary['candidate_versions'], 'candidate')}"
        ),
        "bible": (
            f"{summary['canonical_facts']} Canonical facts  |  {summary['ideas']} Ideas  |  "
            f"{summary['pending_proposals']} proposals  |  {summary['open_foreshadowing']} open threads"
        ),
        "last_run": last_run,
        "next": next_command,
    }


def _count_label(count: int, noun: str) -> str:
    return f"{count} {noun if count == 1 else noun + 's'}"
