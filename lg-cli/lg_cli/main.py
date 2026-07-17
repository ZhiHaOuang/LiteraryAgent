from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .catalog import load_skills, load_subagents, load_task_modes
from .config import load_config
from .core_adapter import inspect_core
from .dashboard import render_dashboard
from .init_project import init_workspace
from .workflow import run_workflow


TASK_COMMANDS = ["chat", "code", "write", "outline", "world", "character", "plot", "ref", "check"]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(Path(args.cwd).resolve() if args.cwd else None)
        skills = load_skills()
        agents = load_subagents()
        return _dispatch(args, parser, config, skills, agents)
    except Exception as exc:
        if getattr(args, "debug", False):
            raise
        print(f"LG error: {exc}", file=sys.stderr)
        print("Run with --debug for a traceback.", file=sys.stderr)
        return 1


def _dispatch(args, parser: argparse.ArgumentParser, config, skills, agents) -> int:
    if args.command is None:
        print(render_dashboard(config, mode="interactive", skills_count=len(skills), agents_count=len(agents)))
        if sys.stdin.isatty():
            return interactive_loop(config, debug=args.debug)
        return 0

    if args.command == "init":
        result = init_workspace(config.workspace)
        print(f"LiteraryGiant initialized at {config.workspace / '.literarygiant'}")
        if result.created:
            print("Created:")
            for path in result.created:
                print(f"  - {path}")
        else:
            print("No files changed; existing files were preserved.")
        if result.skipped:
            print(f"Skipped existing items: {len(result.skipped)}")
        return 0

    if args.command == "status":
        return print_status(config, skills, agents)
    if args.command == "skills":
        return print_skills(skills)
    if args.command == "agents":
        return print_agents(agents)
    if args.command == "modes":
        return print_modes()

    if args.command in TASK_COMMANDS:
        prompt = " ".join(args.prompt or []).strip()
        result = run_workflow(args.command, prompt, config, debug=args.debug)
        print(result.text)
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lg",
        description="LiteraryGiant custom agent CLI.",
    )
    parser.add_argument("--version", action="version", version=f"LiteraryGiant {__version__}")
    parser.add_argument("-C", "--cwd", help="Workspace root. Defaults to current directory.")
    parser.add_argument("--debug", action="store_true", help="Show Python traceback and include extra adapter logs.")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init", help="Create .literarygiant config, memory, output, and logs.")
    sub.add_parser("status", help="Show LG workspace, model, core, skills, and agent status.")
    sub.add_parser("skills", help="List available LG skills.")
    sub.add_parser("agents", help="List available LG subagents.")
    sub.add_parser("modes", help="List available LG task modes.")
    for command in TASK_COMMANDS:
        task = sub.add_parser(command, help=f"Run {command} task mode.")
        task.add_argument("prompt", nargs="*", help="Task prompt.")
    return parser


def interactive_loop(config, *, debug: bool = False) -> int:
    print("Type /help for commands. Type /exit to leave.")
    while True:
        try:
            raw = input("lg> ").strip()
        except EOFError:
            print()
            return 0
        if not raw:
            continue
        if raw in {"/exit", "exit", "quit"}:
            return 0
        if raw in {"/help", "help"}:
            print("/init /write /outline /world /character /plot /ref /code /check /skills /agents /status /exit")
            continue
        command, prompt = _parse_slash(raw)
        if command == "init":
            result = init_workspace(config.workspace)
            print(f"Initialized; created {len(result.created)} item(s), skipped {len(result.skipped)} existing item(s).")
        elif command == "status":
            print_status(config, load_skills(), load_subagents())
        elif command == "skills":
            print_skills(load_skills())
        elif command == "agents":
            print_agents(load_subagents())
        elif command in TASK_COMMANDS:
            print(run_workflow(command, prompt, config, debug=debug).text)
        else:
            print(f"Unknown command: {raw}")


def _parse_slash(raw: str) -> tuple[str, str]:
    if raw.startswith("/"):
        raw = raw[1:]
    if " " not in raw:
        return raw, ""
    command, prompt = raw.split(" ", 1)
    return command, prompt.strip()


def print_status(config, skills, agents) -> int:
    core = inspect_core()
    modes = load_task_modes()
    lg_root = config.workspace / ".literarygiant"
    last_run = lg_root / "logs" / "last_run.json"
    reference_dirs = _reference_dirs(config.workspace, config.reference_path)
    print("LiteraryGiant status")
    print(f"  version: {__version__}")
    print(f"  workspace: {config.workspace}")
    print(f"  initialized: {'yes' if lg_root.exists() else 'no'} ({lg_root})")
    print(f"  provider: {config.provider}")
    print(f"  model: {config.model_label}")
    print(f"  api key: {'configured via ' + config.api_key_source if config.api_key else 'not configured'}")
    print(f"  memory: {config.memory_path}")
    print(f"  output: {config.output_path}")
    print(f"  reference: {config.reference_path}")
    print(f"  reference dirs found: {', '.join(str(path) for path in reference_dirs) or 'none'}")
    print(f"  last run: {last_run if last_run.exists() else 'none'}")
    print(f"  config files: {', '.join(str(path) for path in config.loaded_files) or 'none'}")
    print(f"  modes: {len(modes)}")
    print(f"  skills: {len(skills)}")
    print(f"  subagents: {len(agents)}")
    print(f"  core/codex: {'available' if core.available else 'missing'} ({core.root})")
    for note in core.notes:
        print(f"    - {note}")
    print("  core candidates:")
    for candidate in core.candidates:
        marker = "available" if candidate.available else "unavailable"
        print(f"    - {candidate.name}: {marker} ({candidate.reason})")
    return 0


def _reference_dirs(workspace: Path, configured: Path) -> list[Path]:
    candidates = [
        configured,
        workspace / "ReferenceLibrary",
        workspace / "AbstractLibrary",
        workspace / "Bridges",
        workspace / "TaciturnRaw",
        workspace.parent / "ReferenceLibrary" if workspace.name == "LiteraryAgent" else workspace / "ReferenceLibrary",
        workspace.parent / "Library" / "AbstractLibrary" if workspace.name == "LiteraryAgent" else workspace / "Library" / "AbstractLibrary",
        workspace.parent / "Library" / "Bridges" if workspace.name == "LiteraryAgent" else workspace / "Library" / "Bridges",
        workspace.parent / "Library" / "TaciturnRaw" if workspace.name == "LiteraryAgent" else workspace / "Library" / "TaciturnRaw",
    ]
    found: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen or not path.exists() or not path.is_dir():
            continue
        seen.add(key)
        found.append(path)
    return found


def print_skills(skills) -> int:
    print(f"LG skills ({len(skills)})")
    for skill in skills:
        print(f"  - {skill['name']}: {skill['purpose']}")
    return 0


def print_agents(agents) -> int:
    print(f"LG subagents ({len(agents)})")
    for agent in agents:
        print(f"  - {agent['name']}: {agent['role']}")
    return 0


def print_modes() -> int:
    modes = load_task_modes()
    print(f"LG task modes ({len(modes)})")
    for mode in modes:
        print(f"  - {mode['name']}: {mode['description']}")
    return 0
