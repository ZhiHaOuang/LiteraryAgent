from __future__ import annotations

import io
import os
import shutil
import sys
from pathlib import Path

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

from . import __version__
from .config import LGConfig
from .slime_animation import BODY, CROWN, slime_mark


def _render(content, width: int) -> str:
    color = (
        sys.stdout.isatty()
        and "NO_COLOR" not in os.environ
        and os.environ.get("TERM") != "dumb"
    )
    console = Console(
        file=io.StringIO(),
        width=width,
        color_system="truecolor" if color else None,
        force_terminal=color,
    )
    console.print(content)
    return console.file.getvalue().rstrip()


def model_name(config: LGConfig) -> str:
    if config.provider == "deepseek" and config.model_label == "deepseek-flash":
        return "DeepSeek Flash"
    return f"{config.model_label} | {config.provider}"


def render_status_bar(
    config: LGConfig, width: int, *, compact: bool = False, label: str = "LiteraryGiant",
    shelf_root: Path | None = None,
) -> str:
    details = Text(no_wrap=True, overflow="ellipsis")
    details.append(label, style="bold")
    details.append("\n" + model_name(config))
    location = str(config.workspace)
    if shelf_root is not None:
        relative = config.workspace.relative_to(shelf_root)
        location = f"{shelf_root.name or str(shelf_root)} / {relative}"
    details.append("\n" + location, style="dim")
    if compact or width < 32 or os.environ.get("TERM") == "dumb":
        content = details
    else:
        # Keep the mark out of Rich's flexible table allocation entirely.
        icons = ["  ▄ ▄ ▄  ", "  ▀███▀  ", "▄███████▄", "██ ██ ███", "▀██▄████▀"]
        lines = details.split("\n")
        content = Text(no_wrap=True)
        for index, icon in enumerate(icons):
            if index:
                content.append("\n")
            content.append(icon, style=CROWN if index < 2 else BODY)
            content.append("  ")
            if index < len(lines):
                line = lines[index].copy()
                line.truncate(max(1, width - 15), overflow="ellipsis")
                content.append(line)
    return _render(
        Panel(
            content,
            padding=(0, 1),
            border_style=BODY,
            box=box.ASCII if os.environ.get("TERM") == "dumb" else box.ROUNDED,
        ),
        max(4, width),
    )


def render_dashboard(
    config: LGConfig,
    *,
    mode: str,
    skills_count: int,
    agents_count: int,
    terminal_width: int | None = None,
    animation_time: float | None = None,
    compact: bool = False,
) -> str:
    terminal_width = terminal_width or shutil.get_terminal_size((96, 24)).columns
    width = max(4, terminal_width)

    welcome = "What shall we create today?"
    model = model_name(config)
    auth = (
        (
            "ChatGPT subscription configured"
            if config.auth_mode == "chatgpt"
            else "API profile configured"
        )
        if config.credentials_configured
        else "No model configured | /auth"
    )
    mark = (
        Text("LG", style="#dc795f")
        if os.environ.get("TERM") == "dumb"
        else slime_mark(None if compact else animation_time)
    )
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
        Align.center(Text(model, no_wrap=True, overflow="ellipsis")),
        Align.center(Text(auth, style="dim", no_wrap=True, overflow="ellipsis")),
        Align.center(
            Text(str(config.workspace), style="dim", overflow="ellipsis", no_wrap=True)
        ),
    )
    return _render(
        Panel(
            content,
            title=f" LiteraryGiant {__version__} ",
            title_align="left",
            border_style=BODY,
            box=box.ASCII if os.environ.get("TERM") == "dumb" else box.ROUNDED,
            padding=(1, 1),
        ),
        width,
    )
