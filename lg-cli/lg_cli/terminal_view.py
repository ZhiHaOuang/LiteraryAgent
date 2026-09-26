"""Cell-aware presentation for the interactive terminal, independent of workflows."""

import io
import os

from prompt_toolkit.utils import get_cwidth
from rich.console import Console
from rich.markdown import Markdown
from rich.padding import Padding
from rich.text import Text


def clip(text, width):
    text = " ".join(text.split())
    if get_cwidth(text) <= width:
        return text
    while text and get_cwidth(text) > max(0, width - 1):
        text = text[:-1]
    return text + ("…" if width else "")


def choice_rows(labels, selected, width):
    width = max(4, width)
    result = []
    for index, label in enumerate(labels):
        if index:
            result.append(("", "\n"))
        available = width - 4
        if isinstance(label, tuple):
            name, description = label
            name = clip(name, available)
            remaining = available - get_cwidth(name)
            description = clip(description, max(0, remaining - 2))
            label = name + " " * (remaining - get_cwidth(description)) + description
        else:
            label = clip(label, available)
        body = "  " + label + " " * (width - 2 - get_cwidth(label))
        if index == selected:
            if os.environ.get("TERM") != "dumb":
                result.append(("class:command.edge", " ╭" + "─" * (width - 4) + "╮ \n"))
            if os.environ.get("TERM") != "dumb":
                result.extend(
                    [
                        ("class:command.side", " │"),
                        ("class:command.selected", body[2:-2]),
                        ("class:command.side", "│ "),
                    ]
                )
            else:
                result.append(("class:command.selected", body))
            if os.environ.get("TERM") != "dumb":
                result.append(("class:command.edge", "\n ╰" + "─" * (width - 4) + "╯ "))
        else:
            result.append(("", body))
    return result


def render_conversation(blocks, width):
    stream = io.StringIO()
    color = "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
    console = Console(
        file=stream,
        width=max(4, width),
        force_terminal=color,
        color_system="truecolor" if color else None,
        highlight=False,
    )
    for role, value in blocks:
        if role == "user":
            console.print(Padding(Text("> " + value), (0, 0), style="on #363636"))
            console.print()
        elif role == "assistant":
            marker = "*" if os.environ.get("TERM") == "dumb" else "●"
            lines = console.render_lines(
                Markdown(value),
                console.options.update(width=max(2, width - 2)),
                pad=False,
            )
            for index, segments in enumerate(lines):
                line = Text(marker + " " if index == 0 else "  ")
                for segment in segments:
                    if not segment.control:
                        line.append(segment.text, style=segment.style)
                console.print(line)
            console.print()
        else:
            console.print(Text(value.rstrip(), style="dim"))
    return stream.getvalue().rstrip("\n")
