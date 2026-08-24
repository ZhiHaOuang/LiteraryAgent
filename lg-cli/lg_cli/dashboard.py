from __future__ import annotations

import io
import shutil

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from .config import LGConfig
from .project_store import ProjectStore
from .run_store import RunStore


def render_dashboard(config: LGConfig, *, mode: str, skills_count: int, agents_count: int) -> str:
    terminal_width = shutil.get_terminal_size((96, 24)).columns
    width = max(48, min(104, terminal_width))
    console = Console(
        record=True,
        file=io.StringIO(),
        width=width,
        color_system=None,
        force_terminal=False,
    )

    heading = Text()
    heading.append("LITERARYGIANT", style="bold")
    heading.append("  /  LG\n")
    heading.append("Literary agent workflow runtime", style="dim")

    facts = Table.grid(expand=True, padding=(0, 1))
    facts.add_column(style="bold", no_wrap=True)
    facts.add_column(ratio=1, overflow="fold")
    facts.add_row("Workspace", str(config.workspace))
    facts.add_row("Model", f"{config.model_label}  |  {config.provider}")
    facts.add_row("Runtime", f"{mode}  |  {skills_count} skills  |  {agents_count} subagents")
    facts.add_row("API", config.api_key_source if config.api_key else "not configured (dry-run available)")

    story = _story_status(config)
    facts.add_row("Project", story["project"])
    facts.add_row("Manuscript", story["manuscript"])
    facts.add_row("Story Bible", story["bible"])
    facts.add_row("Last run", story["last_run"])
    facts.add_row("Next", story["next"])

    commands = Table.grid(expand=True, padding=(0, 1))
    commands.add_column(ratio=1)
    commands.add_column(ratio=1)
    commands.add_row("/project info   /bible list", "/chapter list   /scene list")
    commands.add_row("/scene plan <id>   /scene draft <id>", "/version list <doc>   /review consistency")
    commands.add_row("/outline   /world   /character", "/plot   /write   /check   /ref")
    commands.add_row("/export manuscript   /status", "/run list   /run resume <id>")

    content = Group(heading, Text(""), facts, Text(""), Text("Commands", style="bold"), commands)
    console.print(
        Panel(
            content,
            title=f" LiteraryGiant {__version__} ",
            subtitle="Type naturally to chat. /exit closes the session.",
            border_style="cyan",
            padding=(1, 2),
        )
    )
    return console.export_text().rstrip()


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
