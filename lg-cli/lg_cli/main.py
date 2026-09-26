from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from prompt_toolkit.application import run_in_terminal

from . import __version__
from .auth_ui import run_auth
from .bookshelf import Bookshelf, remember_location, restore_location
from .catalog import load_task_modes
from .config import LGConfig, ensure_agent_config, load_config
from .conversation_store import ConversationStore
from .core_adapter import inspect_core
from .credentials import add_auth_parser, dispatch_auth
from .dashboard import render_dashboard, render_status_bar
from .definitions import DefinitionRegistry
from .doctor import run_doctor
from .init_project import init_workspace
from .knowledge import KnowledgeGateway
from .project_store import ProjectRegistry, ProjectStore
from .run_store import RunStore
from .slash_commands import command_catalog
from .story_cli import STORY_COMMANDS, add_story_commands, dispatch_story_command
from .terminal_input import LiteraryInput
from .ui import RunEventRenderer
from .workflow_runner import (
    COMMAND_TO_WORKFLOW,
    WorkflowExecutionResult,
    WorkflowRunner,
)

TASK_COMMANDS = tuple(COMMAND_TO_WORKFLOW)
TOP_LEVEL_COMMANDS = {
    *TASK_COMMANDS,
    *STORY_COMMANDS,
    "init",
    "status",
    "doctor",
    "skills",
    "agents",
    "modes",
    "run",
    "native",
    "auth",
    "newbook",
    "novel",
}


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    normalized_argv, natural = _normalize_argv(raw_argv)
    parser = build_parser()
    args = parser.parse_args(normalized_argv)
    setattr(args, "natural", natural)
    try:
        if args.command == "auth":
            return dispatch_auth(args, args.environment or "sandbox")
        if args.shelf and args.cwd:
            raise ValueError("Use either --shelf or -C, not both")
        root = Path(args.shelf or args.cwd).expanduser().resolve() if args.shelf or args.cwd else Path.cwd()
        if args.command in {None, "novel"} and sys.stdin.isatty() and not args.shelf and not args.cwd:
            root = restore_location(root, args.environment)
        if args.shelf:
            Bookshelf(root).initialize()
        if args.command == "project" and args.project_command == "organize":
            from .book_assets import organize_book, print_plan

            print_plan(organize_book(root, apply=args.apply), json_mode=args.json)
            return 0
        config = load_config(root, environment=args.environment)
        config = _apply_runtime_overrides(config, args)
        return _dispatch(args, parser, config)
    except (KeyboardInterrupt, EOFError):
        return 130
    except Exception as exc:
        if getattr(args, "debug", False):
            raise
        if getattr(args, "json", False):
            print(json.dumps({"type": "error", "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"LG error: {exc}", file=sys.stderr)
            print("Run with --debug for a traceback.", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="literary",
        description="LiteraryGiant long-form writing agent CLI.",
    )
    parser.add_argument("--version", action="version", version=f"LiteraryGiant {__version__}")
    parser.add_argument("-C", "--cwd", help="Workspace root. Defaults to current directory.")
    parser.add_argument("--shelf", help="Bookshelf root containing independent book directories.")
    parser.add_argument("--environment", help="Isolated LG credential/config space; ignores inherited model keys.")
    parser.add_argument("--debug", action="store_true", help="Show tracebacks and model events.")
    parser.add_argument("--conversation", help="Continue a conversation ID belonging to this workspace.")
    _add_runtime_options(parser)
    sub = parser.add_subparsers(dest="command")
    add_auth_parser(sub)

    sub.add_parser("init", help="Initialize the LG project workspace.")
    newbook = sub.add_parser("newbook", help="Create a book inside the selected bookshelf.")
    newbook.add_argument("name")
    novel = sub.add_parser("novel", help="Read and edit chapters with save confirmation.")
    novel.add_argument("chapter", nargs="?", help="Optional chapter slug.")
    native = sub.add_parser("native", help="Launch the separately built LG native terminal.")
    native.add_argument("prompt", nargs="*")
    native.add_argument("--manifest", help="Path to the verified native-runtime.json.")
    status = sub.add_parser("status", help="Show workspace, core, knowledge, and run status.")
    _add_json_option(status)
    doctor = sub.add_parser("doctor", help="Run actionable installation and project diagnostics.")
    doctor.add_argument("--probe", action="store_true", help="Explicitly send one small request through the configured model runtime.")
    _add_json_option(doctor)
    skills = sub.add_parser("skills", help="List resolved LG skills.")
    _add_json_option(skills)
    agents = sub.add_parser("agents", help="List resolved LG subagents.")
    _add_json_option(agents)
    modes = sub.add_parser("modes", help="List available LG task modes.")
    _add_json_option(modes)

    run = sub.add_parser("run", help="Inspect and resume durable LG runs.")
    run_sub = run.add_subparsers(dest="run_command", required=True)
    run_list = run_sub.add_parser("list", help="List recent runs.")
    run_list.add_argument("--limit", type=int, default=20)
    _add_json_option(run_list)
    run_show = run_sub.add_parser("show", help="Show one run manifest.")
    run_show.add_argument("run_id")
    run_show.add_argument("--events", action="store_true", help="Include the event stream.")
    _add_json_option(run_show)
    run_resume = run_sub.add_parser("resume", help="Resume a failed or interrupted run.")
    run_resume.add_argument("run_id")
    _add_runtime_options(run_resume, suppress_defaults=True)
    adopt = run_sub.add_parser("adopt", help="Import a completed run as a versioned book document.")
    adopt.add_argument("run_id")
    adopt.add_argument("slug")
    adopt.add_argument("--kind", choices=("outline", "world", "character", "note", "research", "style", "chapter", "scene"), default="note")
    adopt.add_argument("--title", required=True)
    adopt.add_argument("--accept", action="store_true", help="Explicitly activate the imported version.")
    _add_json_option(adopt)

    add_story_commands(sub)

    for command in TASK_COMMANDS:
        task = sub.add_parser(command, help=f"Run the {command} workflow.")
        task.add_argument("prompt", nargs="*", help="Task request. Reads stdin when omitted.")
        _add_runtime_options(task, suppress_defaults=True)
    return parser


def _add_runtime_options(parser: argparse.ArgumentParser, *, suppress_defaults: bool = False) -> None:
    default: Any = argparse.SUPPRESS if suppress_defaults else False
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=default,
        help="Resolve context and persist a workflow plan without invoking a model.",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        default=default,
        help="Explicitly allow capped TaciturnRaw retrieval for this run.",
    )
    parser.add_argument(
        "--no-reference",
        action="store_true",
        default=default,
        help="Disable knowledge retrieval for this run.",
    )
    parser.add_argument(
        "--model",
        default=argparse.SUPPRESS if suppress_defaults else None,
        help="Override every model profile for this run.",
    )
    parser.add_argument(
        "--strategy",
        choices=("staged", "single-pass"),
        default=argparse.SUPPRESS if suppress_defaults else None,
        help="Override the configured execution strategy.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=default,
        help="Emit JSONL events and a final result object.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        default=default,
        help="Suppress streaming events.",
    )


def _add_json_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)


def _dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser, config: LGConfig) -> int:
    shelf = Bookshelf.discover(config.workspace)
    if args.command == "novel":
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ValueError("The novel editor requires an interactive terminal")
        return interactive_loop(config, initial_command="/novel" + (" " + shlex.quote(args.chapter) if args.chapter else ""))
    if args.command == "newbook":
        if shelf is None:
            raise ValueError("Select a bookshelf with --shelf /path/to/books first")
        print(f"Created book: {shelf.create(args.name)}")
        return 0
    if shelf and shelf.root == config.workspace and args.command not in {
        None, "status", "doctor", "skills", "agents", "modes"
    }:
        if args.command != "project" or args.project_command not in {"create", "list"}:
            raise ValueError("Select a book first: /newbook or /focus; use -C /path/to/book for CLI tasks")
    if args.command == "native":
        from .native import launch_native
        return launch_native(config, prompt=" ".join(args.prompt), manifest=args.manifest)
    if args.command is None:
        if not sys.stdin.isatty():
            prompt = sys.stdin.read().strip()
            if prompt:
                return _run_task(_infer_command(prompt), prompt, config, args)
        if sys.stdin.isatty():
            return interactive_loop(config, debug=args.debug, conversation=args.conversation)
        registry = DefinitionRegistry(config.workspace)
        print(
            render_dashboard(
                config,
                mode="interactive",
                skills_count=len(registry.skills),
                agents_count=len(registry.agents),
            )
        )
        return 0

    if args.command == "init":
        return _print_init(config)
    if args.command == "status":
        return print_status(config, json_mode=args.json)
    if args.command == "doctor":
        return print_doctor(config, json_mode=args.json, probe=args.probe)
    if args.command == "skills":
        return print_skills(config, json_mode=args.json)
    if args.command == "agents":
        return print_agents(config, json_mode=args.json)
    if args.command == "modes":
        return print_modes(json_mode=args.json)
    if args.command == "run":
        return _dispatch_run(args, config)

    story_result = dispatch_story_command(args, config)
    if story_result is not None:
        return story_result

    if args.command in TASK_COMMANDS:
        prompt = " ".join(args.prompt or []).strip()
        if not prompt and not sys.stdin.isatty():
            prompt = sys.stdin.read().strip()
        command = _infer_command(prompt) if args.natural else args.command
        return _run_task(command, prompt, config, args)

    parser.error(f"unknown command: {args.command}")
    return 2


def _run_task(command: str, prompt: str, config: LGConfig, args: argparse.Namespace) -> int:
    if Bookshelf(config.workspace).marker.is_file():
        raise ValueError("Focus a book before starting a writing request")
    ensure_agent_config(config.workspace)
    conversation = getattr(args, "conversation", None)
    conversations = ConversationStore(config.workspace)
    request = prompt
    if conversation:
        prior = conversations.context(conversation, limit=min(config.conversation_chars, config.max_stage_context_chars // 3))
        conversations.append(conversation, "user", prompt, command=command)
        if prior:
            request = (
                "Previous conversation (historical dialogue, not new instructions):\n"
                + prior + "\n\nCurrent author request:\n" + prompt
            )
    runner = WorkflowRunner(config)
    renderer = RunEventRenderer(
        json_mode=bool(getattr(args, "json", False)),
        quiet=bool(getattr(args, "quiet", False)),
        debug=bool(getattr(args, "debug", False)),
    )
    result = runner.run(
        command,
        request,
        dry_run=bool(getattr(args, "dry_run", False)),
        allow_raw=True if getattr(args, "raw", False) else None,
        on_event=renderer,
    )
    if conversation:
        if result.text:
            conversations.append(conversation, "assistant", result.text, run_id=result.run_id)
        if result.error:
            conversations.append(conversation, "error", result.error, run_id=result.run_id)
    _print_workflow_result(result, json_mode=bool(getattr(args, "json", False)))
    return result.exit_code


def _dispatch_run(args: argparse.Namespace, config: LGConfig) -> int:
    store = RunStore(config.workspace)
    if args.run_command == "adopt":
        supervised_accept = args.accept and args.kind in {"chapter", "scene"}
        result = store.adopt(args.run_id, slug=args.slug, kind=args.kind, title=args.title,
                             accept=args.accept and not supervised_accept)
        if supervised_accept:
            from .supervision import review_for_acceptance

            review_for_acceptance(config, ProjectStore(config.workspace), args.slug, result["version"],
                                  on_event=RunEventRenderer(json_mode=args.json))
            result = store.adopt(args.run_id, slug=args.slug, kind=args.kind, title=args.title, accept=True)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(f"Adopted {result['slug']} v{result['version']} ({'active' if result['active'] else 'candidate'})")
        return 0
    if args.run_command == "list":
        runs = store.list_runs(limit=max(1, min(args.limit, 200)))
        if args.json:
            print(json.dumps({"runs": runs}, ensure_ascii=False))
        elif not runs:
            print("No LG runs found.")
        else:
            print("Recent LG runs")
            for item in runs:
                print(
                    f"  {item.get('run_id')}  {str(item.get('status', '')).upper():9} "
                    f"{item.get('command')}  {item.get('updated_at')}"
                )
        return 0
    if args.run_command == "show":
        try:
            manifest = store.load(args.run_id)
            events = store.read_events(args.run_id) if args.events else []
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 5
        if args.json:
            payload: dict[str, Any] = {"run": manifest}
            if args.events:
                payload["events"] = events
            print(json.dumps(payload, ensure_ascii=False))
        else:
            _print_run_manifest(manifest)
            if args.events:
                print("\nEvents")
                for event in events:
                    print(
                        f"  {event.get('sequence'):>3}  {event.get('type')}  "
                        f"{event.get('stage_id') or '-'}  {event.get('message') or ''}"
                    )
        return 0
    if args.run_command == "resume":
        runner = WorkflowRunner(config)
        renderer = RunEventRenderer(
            json_mode=bool(getattr(args, "json", False)),
            quiet=bool(getattr(args, "quiet", False)),
            debug=bool(getattr(args, "debug", False)),
        )
        result = runner.resume(
            args.run_id,
            dry_run=bool(getattr(args, "dry_run", False)),
            allow_raw=True if getattr(args, "raw", False) else None,
            on_event=renderer,
        )
        _print_workflow_result(result, json_mode=bool(getattr(args, "json", False)))
        return result.exit_code
    return 2


def _print_workflow_result(result: WorkflowExecutionResult, *, json_mode: bool) -> None:
    if json_mode:
        print(
            json.dumps(
                {
                    "type": "run.result",
                    "run_id": result.run_id,
                    "workflow": result.workflow_name,
                    "status": result.status,
                    "exit_code": result.exit_code,
                    "artifact_path": str(result.artifact_path) if result.artifact_path else None,
                    "latest_path": str(result.latest_path) if result.latest_path else None,
                    "error": result.error,
                    "text": result.text,
                },
                ensure_ascii=False,
            )
        )
        return
    if result.error:
        print(f"LG run failed: {result.error}", file=sys.stderr)
        print(f"Run: {result.run_id}", file=sys.stderr)
        return
    if result.text:
        print("\n" + result.text.rstrip())
    print(f"\nRun: {result.run_id}")
    if result.artifact_path:
        print(f"Artifact: {result.artifact_path}")
    if result.latest_path:
        print(f"Latest: {result.latest_path}")


def interactive_loop(config: LGConfig, *, debug: bool = False, conversation: str | None = None,
                     initial_command: str | None = None) -> int:
    shelf = Bookshelf.discover(config.workspace)
    active_book = shelf is None or shelf.root != config.workspace
    if active_book and not ProjectStore(config.workspace).initialized:
        init_workspace(config.workspace)
    if active_book:
        ensure_agent_config(config.workspace)
    history_path = config.workspace / ".literarygiant" / "history"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    animation_started: float | None = None
    conversations = ConversationStore(config.workspace)
    if shelf and active_book and conversation is None:
        saved = conversations.list()
        conversation = saved[0][0] if saved else None
    if conversation:
        conversations.read(conversation)

    def welcome(width: int) -> str:
        nonlocal animation_started
        now = time.monotonic()
        if animation_started is None:
            animation_started = now
        return render_dashboard(
            config, mode="interactive", skills_count=0, agents_count=0,
            terminal_width=width, animation_time=now - animation_started,
        )

    session = LiteraryInput(
        history_path,
        welcome=welcome,
        compact_welcome=lambda width: render_dashboard(
            config, mode="interactive", skills_count=0, agents_count=0,
            terminal_width=width, compact=True,
        ),
        minimal_welcome=lambda width: render_status_bar(config, width, compact=True,
            shelf_root=shelf.root if shelf else None),
        commands=command_catalog(build_parser()),
        status_bar=lambda width: render_status_bar(config, width, compact=session.app.output.get_size().rows < 20,
            shelf_root=shelf.root if shelf else None,
            label="LiteraryGiant | Book" if active_book else "LiteraryGiant | Bookshelf"),
    )

    def restore(selected):
        nonlocal conversation
        records = conversations.read(selected)
        session.clear()
        session.welcome = None
        for record in records:
            session.append(record["text"], role=record["role"] if record["role"] != "error" else "system")
        conversation = selected

    if conversation:
        restore(conversation)

    remember_location(config.workspace, config.environment)

    def focus_book(target):
        nonlocal config, conversations, conversation, active_book
        updated = load_config(target, environment=config.environment)
        init_workspace(target)
        config = updated
        conversations = ConversationStore(target)
        conversation = None
        active_book = True
        session.set_history(target / ".literarygiant" / "history")
        session.clear()
        session.model, session.provider = config.model_label, config.provider
        saved = conversations.list() if shelf else []
        if saved:
            restore(saved[0][0])
        remember_location(target, config.environment)
        session.append(f"Opened project: {target}\n")

    async def handle(raw: str) -> bool:
        nonlocal config, conversations, conversation, shelf, active_book
        if raw == "/novel" or raw.startswith("/novel "):
            if not active_book:
                session.append("先用 /focus 选择一本书。\n")
                return True
            from .novel_editor import browse_novel

            parts = shlex.split(raw)
            if len(parts) > 2:
                raise ValueError("Usage: /novel [chapter-slug]")
            await browse_novel(session, ProjectStore(config.workspace), parts[1] if len(parts) == 2 else None)
            return True
        read_aliases = {
            "/chapter list": "chapters", "/analysis list": "analyses",
            "/bible list": "bible", "/timeline list": "timeline",
            "/foreshadowing list": "storylines",
        }
        if raw.strip() in read_aliases:
            raw = "/browse " + read_aliases[raw.strip()]
        if raw in {"/browse", "/characters", "/events", "/storylines", "/timeline"} or raw.startswith("/browse "):
            if not active_book:
                session.append("Focus a book before browsing its assets.\n")
                return True
            from .project_browser import TOPICS, browse_project
            parts = shlex.split(raw)
            topic = (parts[1] if len(parts) == 2 else None) if parts[0] == "/browse" else parts[0][1:]
            if len(parts) > 2 or (topic is not None and topic not in TOPICS):
                raise ValueError("Usage: /browse [" + "|".join(TOPICS) + "]")
            await browse_project(session, ProjectStore(config.workspace), topic)
            return True
        if raw == "/shelf" or raw.startswith("/shelf "):
            parts = shlex.split(raw)
            if len(parts) != 2:
                session.append(f"Bookshelf: {shelf.root if shelf else 'not selected'}\nUsage: /shelf /path/to/books\n")
                return True
            target = Path(parts[1]).expanduser()
            if not target.is_absolute():
                target = (shelf.root if shelf else config.workspace) / target
            selected = Bookshelf(target)
            updated = load_config(selected.root, environment=config.environment)
            selected.initialize()
            shelf, config, active_book = selected, updated, False
            conversations, conversation = ConversationStore(config.workspace), None
            session.set_history(config.workspace / ".literarygiant" / "history")
            session.clear()
            session.model, session.provider = config.model_label, config.provider
            session.append(f"Bookshelf: {shelf.root}\n")
            remember_location(config.workspace, config.environment)
            return True
        if raw == "/newbook" or raw.startswith("/newbook "):
            if shelf is None:
                try:
                    directory = await session.ask(
                        kind="input", title="Choose bookshelf",
                        text="Parent directory for your books (not an existing book)",
                        default=str(config.workspace.parent),
                    )
                finally:
                    session.close_dialog()
                if not directory or not directory.strip():
                    return True
                await handle("/shelf " + shlex.quote(directory.strip()))
            parts = shlex.split(raw)
            if len(parts) == 1:
                try:
                    name = await session.ask(kind="input", title="New book",
                        text=f"Book name | Create in: {shelf.root}")
                finally:
                    session.close_dialog()
                if not name:
                    return True
            else:
                name = " ".join(parts[1:])
            target = shelf.create(name)
            focus_book(target)
            session.append(f"Created book: {target}\n")
            return True
        if raw in {"/open", "/focus"} or raw.startswith(("/open ", "/focus ")):
            parts = shlex.split(raw)
            focusing = parts[0] == "/focus"
            projects = (shelf.books() if shelf else ProjectRegistry().list()) if focusing else []
            if focusing and len(parts) == 1:
                choices = [(p["workspace"], f"{p['name']} | {p['workspace']}") for p in projects
                           if p["workspace_exists"] and p["initialized"]]
                if not choices:
                    session.append('No books found. Use /newbook "Title" in a bookshelf.\n')
                    return True
                try:
                    selected = await session.ask(kind="choice", title="Focus a book",
                        text="Projects", values=choices)
                finally:
                    session.close_dialog()
                if selected is None:
                    return True
                parts.append(selected)
            if len(parts) != 2:
                session.append('Usage: /open "/path/to/book" or /focus [name-or-path]\n')
                return True
            if focusing:
                matches = [p for p in projects if parts[1] in {p["name"], p["project_id"]}]
                if len(matches) > 1:
                    raise ValueError("Multiple books have that name; choose /focus or use an exact path")
                if matches:
                    parts[1] = matches[0]["workspace"]
            target = Path(parts[1]).expanduser()
            if not target.is_absolute():
                target = (shelf.root if shelf else config.workspace) / target
            target = target.resolve()
            if target.exists() and not target.is_dir():
                raise ValueError("Project path is not a directory")
            if shelf:
                shelf.check_book(target)
            if focusing and not ProjectStore(target).initialized:
                raise ValueError("Not an initialized book. Use /project create or /open first")
            focus_book(target)
            return True
        if raw == "/new":
            conversation = None
            session.clear()
            session.append("New conversation. Project memory is unchanged.\n")
            return True
        if raw in {"resume", "/resume"} or raw.startswith("/resume "):
            if not active_book:
                session.append("Focus a book to load its conversations.\n")
                return True
            parts = shlex.split(raw)
            if len(parts) > 2:
                raise ValueError("Usage: /resume [conversation-id]")
            selected = parts[1] if len(parts) == 2 else None
            if selected is None:
                choices = conversations.list()
                if not choices:
                    session.append("No saved conversations in this project.\n")
                    return True
                try:
                    selected = await session.ask(kind="choice", title="Conversations",
                        text=str(config.workspace),
                        values=[(key, f"{key}  {title}") for key, title in choices])
                finally:
                    session.close_dialog()
            if selected:
                restore(selected)
            return True
        if raw in {"/exit", "exit", "quit"}:
            return False
        if raw == "/clear":
            session.clear()
            return True
        if raw in {"/help", "help"}:
            session.append("\n".join(f"{command.text}  {command.description}" for command in command_catalog(build_parser()) if " " not in command.text) + "\n")
            return True
        if raw.startswith("/"):
            try:
                nested = shlex.split(raw[1:])
            except ValueError as exc:
                session.append(f"Cannot parse command: {exc}\n")
                return True
            if nested and nested[0] == "model":
                nested = ["auth", "model", *nested[1:]] if len(nested) > 1 else ["auth"]
            if not nested or nested[0] not in TOP_LEVEL_COMMANDS:
                session.append(f"Unknown command: {raw}\n")
                return True
        else:
            nested = [_infer_command(raw), raw]
        if not active_book and nested[0] not in {"auth", "status", "doctor", "skills", "agents", "modes", "project"}:
            session.append('Choose a book with /focus or create one with /newbook "Title".\n')
            return True
        env_args = ["--environment", config.environment] if config.environment else []
        argv = ["-C", str(config.workspace), *env_args, *(["--debug"] if debug else []), *nested]
        if nested[0] in TASK_COMMANDS:
            if conversation is None:
                conversation = conversations.create()
            argv[0:0] = ["--conversation", conversation]
        if nested[0] in {"auth", "native"}:
            def external_command():
                try:
                    return main(argv)
                except SystemExit as exc:
                    return exc.code
            if nested[0] == "auth":
                def refresh_auth():
                    nonlocal config
                    config = load_config(config.workspace, environment=config.environment)
                    session.model, session.provider = config.model_label, config.provider
                result = await run_auth(session, external_command, refresh=refresh_auth)
            else:
                result = await run_in_terminal(external_command, in_executor=True)
            if result == 0 and nested[0] == "auth":
                session.append(f"Active model: {config.provider} / {config.model_label}\n")
        else:
            structured = not debug and "--json" not in nested and (
                nested[0] in {*TASK_COMMANDS, "edit"}
                or nested[:2] in (["run", "resume"], ["run", "adopt"], ["scene", "plan"], ["scene", "draft"],
                                 ["scene", "accept"], ["version", "accept"], ["bible", "curate"],
                                 ["analysis", "chapter"], ["analysis", "volume"])
            )
            if structured:
                argv.insert(0, "--json")
            await session.run_command(
                [sys.executable, "-u", "-m", "lg_cli", *argv], structured=structured
            )
        return True

    options = {"initial_command": initial_command} if initial_command else {}
    return session.run(handle, config.model_label, config.provider, **options)


def print_status(config: LGConfig, *, json_mode: bool = False) -> int:
    core = inspect_core(runtime_manifest=config.runtime_manifest)
    registry = DefinitionRegistry(config.workspace)
    gateway = KnowledgeGateway(config)
    runs = RunStore(config.workspace).list_runs(limit=1)
    project_store = ProjectStore(config.workspace)
    project: dict[str, Any] | None = None
    if project_store.initialized:
        project = project_store.summary()
    payload = {
        "version": __version__,
        "workspace": str(config.workspace),
        "initialized": (config.workspace / ".literarygiant").is_dir(),
        "provider": config.provider,
        "model": config.model_label,
        "api_key_source": config.api_key_source,
        "execution_strategy": config.execution_strategy,
        "memory": str(config.memory_path),
        "output": str(config.output_path),
        "library": str(gateway.library_root) if gateway.library_root else None,
        "skills": len(registry.skills),
        "subagents": len(registry.agents),
        "core": {
            "root": str(core.root),
            "source_available": core.source_available,
            "runtime_available": core.runtime_available,
            "pinned_commit": core.pinned_commit,
            "candidates": [
                {
                    "name": candidate.name,
                    "available": candidate.available,
                    "reason": candidate.reason,
                    "version": candidate.version,
                }
                for candidate in core.candidates
            ],
        },
        "last_run": runs[0] if runs else None,
        "project": project,
        "config_files": [str(path) for path in config.loaded_files],
        "warnings": list(config.warnings),
    }
    if json_mode:
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    print("LiteraryGiant status")
    print(f"  version: {payload['version']}")
    print(f"  workspace: {payload['workspace']}")
    print(f"  initialized: {'yes' if payload['initialized'] else 'no'}")
    print(f"  provider/model: {config.provider} / {config.model_label}")
    print(f"  auth: {config.api_key_source if config.credentials_configured else 'not configured'} ({config.auth_mode})")
    print(f"  strategy: {config.execution_strategy}")
    print(f"  memory: {config.memory_path}")
    print(f"  output: {config.output_path}")
    print(f"  knowledge library: {payload['library'] or 'not found'}")
    print(f"  definitions: {len(registry.skills)} skills / {len(registry.agents)} subagents")
    if project:
        print(
            f"  story project: {project['name']} "
            f"({project['chapters']} chapters / {project['scenes']} scenes / "
            f"{project['canonical_facts']} canonical facts)"
        )
    else:
        print("  story project: not initialized")
    print(f"  core pin: {core.pinned_commit or 'missing'}")
    print(f"  core source: {'available' if core.source_available else 'not available'} ({core.root})")
    print(f"  core runtime: {'available' if core.runtime_available else 'not available'}")
    for candidate in core.candidates:
        marker = "PASS" if candidate.available else "FAIL"
        print(f"    {marker} {candidate.name}: {candidate.version or candidate.reason}")
    print(f"  last run: {runs[0].get('run_id') if runs else 'none'}")
    return 0


def print_doctor(config: LGConfig, *, json_mode: bool = False, probe: bool = False) -> int:
    report = run_doctor(config, probe=probe)
    if json_mode:
        print(
            json.dumps(
                {"ok": report.ok, "checks": [check.to_dict() for check in report.checks]},
                ensure_ascii=False,
            )
        )
    else:
        print("LiteraryGiant doctor")
        for check in report.checks:
            print(f"  {check.status:4}  {check.name:14} {check.message}")
    return report.exit_code


def print_skills(config: LGConfig, *, json_mode: bool = False) -> int:
    skills = DefinitionRegistry(config.workspace).skills
    if json_mode:
        print(
            json.dumps(
                {
                    "skills": [
                        {
                            "id": skill.id,
                            "description": skill.description,
                            "tools": list(skill.tools),
                            "source": skill.source,
                        }
                        for skill in skills
                    ]
                },
                ensure_ascii=False,
            )
        )
        return 0
    print(f"LG skills ({len(skills)})")
    id_width = max((len(skill.id) for skill in skills), default=0)
    for skill in skills:
        print(f"  {skill.id:<{id_width}}  {skill.description} [{skill.source}]")
    return 0


def print_agents(config: LGConfig, *, json_mode: bool = False) -> int:
    agents = DefinitionRegistry(config.workspace).agents
    if json_mode:
        print(
            json.dumps(
                {
                    "agents": [
                        {
                            "id": agent.id,
                            "name": agent.name,
                            "role": agent.role,
                            "model_profile": agent.model_profile,
                            "tools": list(agent.tools),
                            "skills": list(agent.skills),
                            "source": agent.source,
                        }
                        for agent in agents
                    ]
                },
                ensure_ascii=False,
            )
        )
        return 0
    print(f"LG subagents ({len(agents)})")
    id_width = max((len(agent.id) for agent in agents), default=0)
    for agent in agents:
        print(f"  {agent.id:<{id_width}}  {agent.role} [model={agent.model_profile}, source={agent.source}]")
    return 0


def print_modes(*, json_mode: bool = False) -> int:
    modes = load_task_modes()
    if json_mode:
        print(json.dumps({"modes": modes}, ensure_ascii=False))
        return 0
    print(f"LG task modes ({len(modes)})")
    for mode in modes:
        print(f"  {mode.get('command', ''):12} {mode.get('description', '')}")
    return 0


def _print_init(config: LGConfig) -> int:
    result = init_workspace(config.workspace)
    print(f"LiteraryGiant initialized at {config.workspace / '.literarygiant'}")
    print(f"Created {len(result.created)} item(s); preserved {len(result.skipped)} existing item(s).")
    return 0


def _print_run_manifest(manifest: dict[str, Any]) -> None:
    print(f"LG run {manifest.get('run_id')}")
    for key in (
        "status",
        "workflow",
        "command",
        "created_at",
        "updated_at",
        "provider",
        "model",
        "resumed_from",
        "artifact_path",
        "latest_path",
        "error",
    ):
        print(f"  {key}: {manifest.get(key)}")
    print(f"  completed_stages: {', '.join(manifest.get('completed_stages', [])) or 'none'}")
    print(f"  request: {manifest.get('request', '')}")


def _apply_runtime_overrides(config: LGConfig, args: argparse.Namespace) -> LGConfig:
    model = getattr(args, "model", None)
    strategy = getattr(args, "strategy", None)
    if model:
        config = replace(
            config,
            default_model=model,
            writer_model=model,
            coder_model=model,
            critic_model=model,
        )
    if strategy:
        config = replace(config, execution_strategy=strategy)
    if getattr(args, "no_reference", False):
        config = replace(config, enable_reference=False)
    return config


def _normalize_argv(argv: list[str]) -> tuple[list[str], bool]:
    if not argv:
        return argv, False
    options_with_values = {"-C", "--cwd", "--shelf", "--model", "--strategy", "--environment", "--conversation"}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in options_with_values:
            index += 2
            continue
        if any(token.startswith(prefix + "=") for prefix in ("--cwd", "--shelf", "--model", "--strategy", "--environment", "--conversation")):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        if token not in TOP_LEVEL_COMMANDS:
            return [*argv[:index], "chat", *argv[index:]], True
        return argv, False
    return argv, False


def _infer_command(text: str) -> str:
    lowered = text.lower()
    rules = (
        ("check", ("一致性", "矛盾", "检查设定", "continuity", "audit")),
        ("ref", ("参考", "检索", "素材库", "reference", "research")),
        ("world", ("世界观", "力量体系", "势力", "worldbuilding")),
        ("character", ("人物", "角色", "人设", "character")),
        ("plot", ("剧情", "情节", "冲突升级", "plot")),
        ("outline", ("大纲", "分卷", "outline")),
        ("write", ("写一章", "写下一章", "正文", "chapter")),
    )
    for command, terms in rules:
        if any(term in lowered for term in terms):
            return command
    return "chat"


if __name__ == "__main__":
    raise SystemExit(main())
