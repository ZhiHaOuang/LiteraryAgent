from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import TextIO

from rich.console import Console
from rich.text import Text

from .events import EventType, RunEvent


@dataclass
class RunEventRenderer:
    json_mode: bool = False
    quiet: bool = False
    debug: bool = False
    stream: TextIO | None = None

    def __post_init__(self) -> None:
        if self.stream is None:
            self.stream = sys.stdout
        self.console = Console(file=self.stream, highlight=False)

    def __call__(self, event: RunEvent) -> None:
        if self.quiet:
            return
        if self.json_mode:
            assert self.stream is not None
            self.stream.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
            self.stream.flush()
            return
        if event.type == EventType.MODEL_EVENT and not self.debug:
            return
        line, style = _event_line(event)
        if line:
            self.console.print(Text(line, style=style))


def _event_line(event: RunEvent) -> tuple[str, str]:
    if event.type in {EventType.RUN_STARTED, EventType.RUN_RESUMED}:
        return f"LG run {event.run_id} | {event.message}", "bold cyan"
    if event.type == EventType.CONTEXT_STARTED:
        return "CONTEXT  Loading project context", "dim"
    if event.type == EventType.CONTEXT_COMPLETED:
        return f"CONTEXT  {event.message}", "green"
    if event.type == EventType.STAGE_STARTED:
        index = event.data.get("index", "?")
        total = event.data.get("total", "?")
        return f"[{index}/{total}] START  {event.stage_id}  {event.message}", "bold"
    if event.type == EventType.STAGE_COMPLETED:
        restored = " (restored)" if event.data.get("restored") else ""
        return f"      DONE   {event.stage_id}{restored}  {event.message}", "green"
    if event.type == EventType.STAGE_FAILED:
        return f"      FAIL   {event.stage_id}  {event.message}", "bold red"
    if event.type == EventType.MODEL_EVENT:
        return f"      MODEL  {event.stage_id}  {event.message}", "dim"
    if event.type == EventType.ARTIFACT_WRITTEN:
        return f"ARTIFACT {event.data.get('artifact_path', event.message)}", "cyan"
    if event.type == EventType.RUN_PLANNED:
        return f"PLANNED  {event.message}", "yellow"
    if event.type == EventType.RUN_COMPLETED:
        return f"COMPLETE {event.message}", "bold green"
    if event.type == EventType.RUN_FAILED:
        return f"FAILED   {event.message}", "bold red"
    return event.message, ""
