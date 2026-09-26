from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .config import LGConfig
from .chapter_sync import ChapterSync
from .project_browser import TOPICS, project_entries
from .book_analysis import BookAnalysisStore, CATEGORIES, ChapterAnalysisService
from .exporter import EXPORT_FORMATS, EXPORT_TARGETS, export_project
from .init_project import init_workspace
from .project_store import (
    DOCUMENT_KINDS,
    DOCUMENT_STATES,
    FACT_STATES,
    ProjectRegistry,
    ProjectStore,
)
from .story_review import chapter_quality_report, run_consistency_check
from .story_workflow import REVISION_MODES, StoryTaskResult, StoryWorkflowService
from .ui import RunEventRenderer


STORY_COMMANDS = {
    "project",
    "bible",
    "document",
    "chapter",
    "scene",
    "version",
    "review",
    "edit",
    "timeline",
    "foreshadowing",
    "export",
    "search",
    "browse",
    "characters",
    "events",
    "storylines",
    "analysis",
}


def add_story_commands(subparsers: argparse._SubParsersAction[Any]) -> None:
    analysis = subparsers.add_parser("analysis", help="Inspect and generate source-linked book analyses.")
    analysis_commands = analysis.add_subparsers(dest="analysis_command", required=True)
    enable = analysis_commands.add_parser("enable", help="Enable candidate supervision for this book.")
    _json_option(enable)
    analysis_list = analysis_commands.add_parser("list")
    analysis_list.add_argument("reference", nargs="?")
    analysis_list.add_argument("--volume", action="store_true")
    _json_option(analysis_list)
    analysis_chapter = analysis_commands.add_parser("chapter")
    analysis_chapter.add_argument("reference")
    analysis_chapter.add_argument("--category", action="append", choices=sorted(CATEGORIES))
    _json_option(analysis_chapter)
    analysis_volume = analysis_commands.add_parser("volume", help="Review all five dimensions across a volume or book.")
    analysis_volume.add_argument("reference", nargs="?", default="__book__")
    _json_option(analysis_volume)
    adjudicate = analysis_commands.add_parser("adjudicate", help="Record an explicit, source-bound human dismissal of one false-positive finding.")
    adjudicate.add_argument("reference")
    adjudicate.add_argument("--version", type=int)
    adjudicate.add_argument("--volume", action="store_true")
    adjudicate.add_argument("--category", required=True, choices=sorted(CATEGORIES))
    adjudicate.add_argument("--finding", required=True, type=int, help="One-based index in report.findings.")
    adjudicate.add_argument("--reviewer", required=True)
    adjudicate.add_argument("--reason", required=True)
    adjudicate.add_argument("--confirm", action="store_true")
    _json_option(adjudicate)
    browse = subparsers.add_parser("browse", help="Read project assets without calling a model.")
    browse.add_argument("topic", choices=sorted(TOPICS))
    _json_option(browse)
    for topic in ("characters", "events", "storylines"):
        command = subparsers.add_parser(topic, help=f"Browse existing {topic} without generation.")
        _json_option(command)
    _add_project_parser(subparsers)
    _add_bible_parser(subparsers)
    _add_document_parser(subparsers)
    _add_chapter_parser(subparsers)
    _add_scene_parser(subparsers)
    _add_version_parser(subparsers)
    _add_review_parser(subparsers)
    _add_edit_parser(subparsers)
    _add_timeline_parser(subparsers)
    _add_foreshadowing_parser(subparsers)
    _add_export_parser(subparsers)
    search = subparsers.add_parser("search", help="Search accepted project text and Story Bible facts.")
    search.add_argument("query")
    search.add_argument("--kind", action="append", choices=sorted(DOCUMENT_KINDS), default=[])
    search.add_argument("--tag", action="append", default=[])
    search.add_argument("--limit", type=int, default=12)
    _json_option(search)


def dispatch_story_command(args: argparse.Namespace, config: LGConfig) -> int | None:
    if args.command not in STORY_COMMANDS:
        return None
    if args.command == "project":
        return _dispatch_project(args, config)
    store = ProjectStore(config.workspace)
    if args.command == "analysis":
        if args.analysis_command == "adjudicate":
            from .supervision import adjudicate_review

            result = adjudicate_review(store, args.reference, category=args.category,
                finding_index=args.finding, reviewer=args.reviewer, reason=args.reason,
                confirmed=args.confirm, version_number=args.version, volume=args.volume)
            return _emit(result, args, text=f"Adjudication recorded: {result['path']}. Original report retained; manuscript unchanged.")
        if args.analysis_command == "enable":
            from .supervision import configure_supervision

            return _emit(configure_supervision(store), args, text="Chapter supervision enabled for this book.")
        if args.analysis_command == "list":
            return _emit_many(BookAnalysisStore(store).list(args.reference, volume=args.volume), args, heading="Book analyses",
                formatter=lambda item: f"{item['chapter']} | {item['library']} | {item['status']}\n{item['report']['summary']}")
        result = ChapterAnalysisService(config).analyze(args.reference, categories=getattr(args, "category", None),
            on_event=_renderer(args), volume=args.analysis_command == "volume")
        _emit(result, args, text=f"Analysis run {result['run_id']}: {result['status']}")
        return 1 if result["status"] == "blocked" else 0
    if args.command in {"browse", "characters", "events", "storylines"}:
        topic = args.topic if args.command == "browse" else args.command
        entries = project_entries(store, topic)
        if not getattr(args, "json", False):
            from rich.console import Console
            from rich.table import Table
            from rich.text import Text

            table = Table(title=f"{store.project_info().name} / {TOPICS[topic]}",
                          box=None, expand=True, padding=(0, 1))
            table.add_column("#", width=4, style="dim")
            table.add_column("Name", ratio=2)
            table.add_column("Summary", ratio=3)
            for number, entry in enumerate(entries, 1):
                summary = next((line for line in entry.text.splitlines()[1:]
                                if line.strip() and not line.startswith(("#", "Status:", "Source:"))), "")
                table.add_row(str(number), Text(entry.title), Text(summary[:160]))
            Console().print(table)
            return 0
        return _emit_many(entries, args, heading=TOPICS[topic],
                          formatter=lambda entry: entry.text + "\n")
    if args.command == "bible":
        if args.bible_command == "curate":
            result = StoryWorkflowService(config, project=store).curate_document(
                args.reference, request=" ".join(args.request), dry_run=args.dry_run,
                on_event=_renderer(args),
            )
            return _emit_story_result(result, args)
        return _dispatch_bible(args, store)
    if args.command == "document":
        return _dispatch_document(args, store)
    if args.command == "chapter":
        return _dispatch_chapter(args, store)
    if args.command == "scene":
        return _dispatch_scene(args, config, store)
    if args.command == "version":
        if args.version_command == "accept":
            from .supervision import review_for_acceptance

            review_for_acceptance(config, store, args.reference, args.version, final=args.final, on_event=_renderer(args))
        return _dispatch_version(args, store)
    if args.command == "review":
        return _dispatch_review(args, store)
    if args.command == "edit":
        return _dispatch_edit(args, config, store)
    if args.command == "timeline":
        return _dispatch_timeline(args, store)
    if args.command == "foreshadowing":
        return _dispatch_foreshadowing(args, store)
    if args.command == "export":
        return _dispatch_export(args, store)
    return _dispatch_search(args, store)


def _add_project_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    parser = subparsers.add_parser("project", help="Create, list, and inspect independent novel projects.")
    commands = parser.add_subparsers(dest="project_command", required=True)
    organize = commands.add_parser("organize", help="Preview or apply book asset organization; never deletes files.")
    organize.add_argument("--apply", action="store_true")
    _json_option(organize)
    create = commands.add_parser("create", help="Create and register a new story workspace.")
    create.add_argument("name")
    create.add_argument("--path", help="Project directory. Defaults to a directory under the current workspace.")
    _json_option(create)
    listing = commands.add_parser("list", help="List registered projects.")
    _json_option(listing)
    info = commands.add_parser("info", help="Show the current project.")
    _json_option(info)
    rename = commands.add_parser("rename", help="Rename the current project without moving it.")
    rename.add_argument("name")
    _json_option(rename)
    unregister = commands.add_parser("unregister", help="Remove a missing/unused project from the registry only.")
    unregister.add_argument("project_id")
    _json_option(unregister)


def _add_bible_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    parser = subparsers.add_parser("bible", help="Manage Canonical facts, Ideas, and memory proposals.")
    commands = parser.add_subparsers(dest="bible_command", required=True)
    curate = commands.add_parser("curate", help="Extract source-linked memory proposals from a book document.")
    curate.add_argument("reference")
    curate.add_argument("request", nargs="*")
    _generation_options(curate)
    for name, default_state, help_text in (
        ("add", "idea", "Add a replaceable Idea or explicit fact."),
        ("set", "canonical", "Set or update an author-confirmed Canonical fact."),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("category")
        command.add_argument("key")
        command.add_argument("value", nargs="+")
        command.add_argument("--state", choices=sorted(FACT_STATES), default=default_state)
        command.add_argument("--tag", action="append", default=[])
        command.add_argument("--source")
        command.add_argument("--source-version", type=int)
        command.add_argument("--reason", default="")
        _json_option(command)
    listing = commands.add_parser("list", help="List Story Bible facts.")
    listing.add_argument("--state", choices=sorted(FACT_STATES))
    listing.add_argument("--category")
    listing.add_argument("--query", default="")
    listing.add_argument("--tag", action="append", default=[])
    listing.add_argument("--limit", type=int, default=300)
    _json_option(listing)
    show = commands.add_parser("show", help="Show one Story Bible fact.")
    show.add_argument("fact_id", type=int)
    _json_option(show)
    promote = commands.add_parser("promote", help="Confirm an Idea as Canonical.")
    promote.add_argument("fact_id", type=int)
    promote.add_argument("--reason", default="Confirmed by author")
    _json_option(promote)
    archive = commands.add_parser("archive", help="Archive a fact without deleting history.")
    archive.add_argument("fact_id", type=int)
    archive.add_argument("--reason", default="Archived by author")
    _json_option(archive)
    history = commands.add_parser("history", help="Show the audit trail for one fact.")
    history.add_argument("fact_id", type=int)
    _json_option(history)

    proposal = commands.add_parser("proposal", help="Review memory changes proposed by generated text.")
    proposal_commands = proposal.add_subparsers(dest="proposal_command", required=True)
    proposal_list = proposal_commands.add_parser("list")
    proposal_list.add_argument("--status", default="proposed", choices=("proposed", "accepted", "rejected"))
    _json_option(proposal_list)
    proposal_accept = proposal_commands.add_parser("accept")
    proposal_accept.add_argument("proposal_id", type=int)
    proposal_accept.add_argument("--state", choices=("canonical", "idea"), default="canonical")
    _json_option(proposal_accept)
    proposal_reject = proposal_commands.add_parser("reject")
    proposal_reject.add_argument("proposal_id", type=int)
    _json_option(proposal_reject)


def _add_document_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    parser = subparsers.add_parser("document", help="Manage generic outline, world, character, note, and prose documents.")
    commands = parser.add_subparsers(dest="document_command", required=True)
    create = commands.add_parser("create")
    create.add_argument("kind", choices=sorted(DOCUMENT_KINDS))
    create.add_argument("slug")
    create.add_argument("title")
    create.add_argument("--parent")
    create.add_argument("--sequence", type=int)
    create.add_argument("--state", choices=sorted(DOCUMENT_STATES), default="draft")
    create.add_argument("--content", default="")
    create.add_argument("--file", type=Path)
    create.add_argument("--tag", action="append", default=[])
    _json_option(create)
    listing = commands.add_parser("list")
    listing.add_argument("--kind", choices=sorted(DOCUMENT_KINDS))
    listing.add_argument("--state", choices=sorted(DOCUMENT_STATES))
    listing.add_argument("--parent")
    listing.add_argument("--limit", type=int, default=200)
    _json_option(listing)
    show = commands.add_parser("show")
    show.add_argument("reference")
    show.add_argument("--version", type=int)
    _json_option(show)
    revision = commands.add_parser("add-version", help="Import text as a new candidate version.")
    revision.add_argument("reference")
    revision.add_argument("--content", default="")
    revision.add_argument("--file", type=Path)
    revision.add_argument("--state", choices=sorted(DOCUMENT_STATES), default="candidate")
    revision.add_argument("--reason", default="Manual import")
    _json_option(revision)


def _add_chapter_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    parser = subparsers.add_parser("chapter", help="Create and inspect chapter containers.")
    commands = parser.add_subparsers(dest="chapter_command", required=True)
    for name in ("sync", "status", "diff", "import"):
        command = commands.add_parser(name, help=f"{name.capitalize()} per-chapter editable Markdown files.")
        command.add_argument("reference", nargs="?" if name in {"sync", "status"} else None)
        _json_option(command)
    create = commands.add_parser("create")
    create.add_argument("slug")
    create.add_argument("title")
    create.add_argument("--number", type=int)
    create.add_argument("--content", default="")
    create.add_argument("--file", type=Path)
    create.add_argument("--tag", action="append", default=[])
    _json_option(create)
    listing = commands.add_parser("list")
    listing.add_argument("--limit", type=int, default=500)
    _json_option(listing)
    show = commands.add_parser("show")
    show.add_argument("reference")
    _json_option(show)


def _add_scene_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    parser = subparsers.add_parser("scene", help="Manage scene cards and the plan-confirm-draft workflow.")
    commands = parser.add_subparsers(dest="scene_command", required=True)
    create = commands.add_parser("create")
    create.add_argument("slug")
    create.add_argument("title")
    create.add_argument("--chapter")
    create.add_argument("--sequence", type=int)
    _scene_card_arguments(create)
    _json_option(create)
    update = commands.add_parser("set", help="Update explicit fields on a scene card.")
    update.add_argument("reference")
    _scene_card_arguments(update, suppress_defaults=True)
    _json_option(update)
    listing = commands.add_parser("list")
    listing.add_argument("--chapter")
    listing.add_argument("--limit", type=int, default=300)
    _json_option(listing)
    show = commands.add_parser("show")
    show.add_argument("reference")
    _json_option(show)
    plan = commands.add_parser("plan", help="Generate a candidate scene plan.")
    plan.add_argument("reference")
    plan.add_argument("request", nargs="*")
    _generation_options(plan)
    approve = commands.add_parser("approve", help="Approve the current scene plan for drafting.")
    approve.add_argument("reference")
    _json_option(approve)
    draft = commands.add_parser("draft", help="Generate candidate prose from an approved scene plan.")
    draft.add_argument("reference")
    draft.add_argument("request", nargs="*")
    _generation_options(draft)
    accept = commands.add_parser("accept", help="Accept a scene candidate version as manuscript text.")
    accept.add_argument("reference")
    accept.add_argument("version", type=int)
    accept.add_argument("--final", action="store_true")
    _json_option(accept)


def _add_version_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    parser = subparsers.add_parser("version", help="Compare, accept, reject, and restore document revisions.")
    commands = parser.add_subparsers(dest="version_command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("reference")
    _json_option(listing)
    show = commands.add_parser("show")
    show.add_argument("reference")
    show.add_argument("version", type=int)
    _json_option(show)
    diff = commands.add_parser("diff")
    diff.add_argument("reference")
    diff.add_argument("from_version", type=int)
    diff.add_argument("to_version", type=int)
    _json_option(diff)
    accept = commands.add_parser("accept")
    accept.add_argument("reference")
    accept.add_argument("version", type=int)
    accept.add_argument("--final", action="store_true")
    _json_option(accept)
    reject = commands.add_parser("reject")
    reject.add_argument("reference")
    reject.add_argument("version", type=int)
    _json_option(reject)
    restore = commands.add_parser("restore")
    restore.add_argument("reference")
    restore.add_argument("version", type=int)
    restore.add_argument("--reason", default="Restored by author")
    _json_option(restore)


def _add_review_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    parser = subparsers.add_parser("review", help="Run non-destructive consistency and chapter quality checks.")
    commands = parser.add_subparsers(dest="review_command", required=True)
    consistency = commands.add_parser("consistency")
    consistency.add_argument("--document")
    _json_option(consistency)
    chapter = commands.add_parser("chapter")
    chapter.add_argument("reference")
    _json_option(chapter)
    listing = commands.add_parser("list")
    listing.add_argument("--document")
    listing.add_argument("--status", default="open")
    _json_option(listing)


def _add_edit_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    parser = subparsers.add_parser("edit", help="Create a candidate revision using an explicit editing mode.")
    parser.add_argument("reference")
    parser.add_argument("request", nargs="*")
    parser.add_argument("--mode", choices=sorted(REVISION_MODES), required=True)
    parser.add_argument("--source-version", type=int)
    parser.add_argument("--passage", help="Revise only this exact, uniquely matching source passage.")
    _generation_options(parser)


def _add_timeline_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    parser = subparsers.add_parser("timeline", help="Track ordered canonical events and candidate chronology.")
    commands = parser.add_subparsers(dest="timeline_command", required=True)
    add = commands.add_parser("add")
    add.add_argument("label")
    add.add_argument("event", nargs="+")
    add.add_argument("--order", default="")
    add.add_argument("--state", choices=sorted(FACT_STATES), default="idea")
    add.add_argument("--tag", action="append", default=[])
    add.add_argument("--source")
    _json_option(add)
    update = commands.add_parser("set", help="Correct or confirm an existing timeline entry.")
    update.add_argument("entry_id", type=int)
    update.add_argument("--label")
    update.add_argument("--event")
    update.add_argument("--order")
    update.add_argument("--state", choices=sorted(FACT_STATES))
    update.add_argument("--tag", action="append")
    update.add_argument("--source")
    _json_option(update)
    listing = commands.add_parser("list")
    listing.add_argument("--state", choices=sorted(FACT_STATES))
    _json_option(listing)


def _add_foreshadowing_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    parser = subparsers.add_parser("foreshadowing", help="Track setup, planned payoff, and resolved threads.")
    commands = parser.add_subparsers(dest="foreshadowing_command", required=True)
    add = commands.add_parser("add")
    add.add_argument("title")
    add.add_argument("setup", nargs="+")
    add.add_argument("--document")
    add.add_argument("--state", choices=("open", "planned"), default="open")
    add.add_argument("--tag", action="append", default=[])
    _json_option(add)
    listing = commands.add_parser("list")
    listing.add_argument("--state", choices=("open", "planned", "paid", "abandoned"))
    _json_option(listing)
    resolve = commands.add_parser("resolve")
    resolve.add_argument("foreshadow_id", type=int)
    resolve.add_argument("payoff", nargs="+")
    resolve.add_argument("--document")
    resolve.add_argument("--abandon", action="store_true")
    _json_option(resolve)


def _add_export_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    parser = subparsers.add_parser("export", help="Export manuscript and project references to Markdown, TXT, or DOCX.")
    parser.add_argument("target", choices=sorted(EXPORT_TARGETS))
    parser.add_argument("--format", choices=sorted(EXPORT_FORMATS), default="md")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--include-drafts", action="store_true")
    _json_option(parser)


def _scene_card_arguments(parser: argparse.ArgumentParser, *, suppress_defaults: bool = False) -> None:
    default: Any = argparse.SUPPRESS if suppress_defaults else ""
    parser.add_argument("--pov", default=default)
    parser.add_argument("--tense", dest="narrative_tense", default=default)
    parser.add_argument("--time", dest="time_label", default=default)
    parser.add_argument("--location", default=default)
    parser.add_argument("--goal", default=default)
    parser.add_argument("--character", dest="characters", action="append", default=argparse.SUPPRESS if suppress_defaults else [])
    parser.add_argument("--conflict", default=default)
    parser.add_argument("--reveal", dest="required_information", default=default)
    parser.add_argument("--emotion", dest="emotional_change", default=default)
    parser.add_argument("--end", dest="end_state", default=default)
    parser.add_argument("--word-min", type=int, default=argparse.SUPPRESS if suppress_defaults else None)
    parser.add_argument("--word-max", type=int, default=argparse.SUPPRESS if suppress_defaults else None)
    if not suppress_defaults:
        parser.add_argument("--tag", action="append", default=[])


def _generation_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--model", default=argparse.SUPPRESS)


def _json_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)


def _dispatch_project(args: argparse.Namespace, config: LGConfig) -> int:
    if args.project_command == "organize":
        from .book_assets import organize_book, print_plan

        result = organize_book(config.workspace, apply=args.apply)
        print_plan(result, json_mode=getattr(args, 'json', False))
        return 0
    registry = ProjectRegistry()
    if args.project_command == "create":
        target = _project_target(config.workspace, args.name, args.path)
        target.mkdir(parents=True, exist_ok=True)
        init_workspace(target)
        store = ProjectStore(target)
        project = store.rename_project(args.name)
        registry.register(project)
        return _emit(project, args, text=f"Created project `{project.name}` at {project.workspace}")
    if args.project_command == "list":
        projects = registry.list()
        return _emit_many(projects, args, heading="LiteraryGiant projects")
    if args.project_command == "unregister":
        removed = registry.remove(args.project_id)
        return _emit(
            {"project_id": args.project_id, "removed": removed},
            args,
            text=("Project registry entry removed." if removed else "Project registry entry was not found."),
            exit_code=0 if removed else 5,
        )
    store = ProjectStore(config.workspace)
    if args.project_command == "rename":
        project = store.rename_project(args.name)
        registry.register(project)
        return _emit(project, args, text=f"Project renamed to `{project.name}`.")
    project = store.project_info()
    registry.register(project)
    counts = {
        "documents": len(store.list_documents(limit=1000)),
        "canonical_facts": len(store.list_facts(state="canonical", limit=1000)),
        "ideas": len(store.list_facts(state="idea", limit=1000)),
        "open_proposals": len(store.list_proposals(status="proposed", limit=1000)),
    }
    return _emit({"project": project, "counts": counts}, args, text=_project_info_text(project, counts))


def _dispatch_bible(args: argparse.Namespace, store: ProjectStore) -> int:
    if args.bible_command in {"add", "set"}:
        operation = store.set_fact if args.bible_command == "set" else store.add_fact
        fact = operation(
            category=args.category,
            key=args.key,
            value=" ".join(args.value),
            state=args.state,
            tags=args.tag,
            source_document=args.source,
            source_version=args.source_version,
            rationale=args.reason,
        )
        return _emit(fact, args, text=_fact_line(fact))
    if args.bible_command == "list":
        facts = store.list_facts(
            state=args.state,
            category=args.category,
            query=args.query,
            tags=args.tag,
            limit=args.limit,
        )
        return _emit_many(facts, args, heading="Story Bible", formatter=_fact_line)
    if args.bible_command == "show":
        fact = store.get_fact(args.fact_id)
        return _emit(fact, args, text=_fact_detail(fact))
    if args.bible_command == "promote":
        fact = store.promote_fact(args.fact_id, rationale=args.reason)
        return _emit(fact, args, text=f"Confirmed Canonical fact: {_fact_line(fact)}")
    if args.bible_command == "archive":
        fact = store.archive_fact(args.fact_id, rationale=args.reason)
        return _emit(fact, args, text=f"Archived: {_fact_line(fact)}")
    if args.bible_command == "history":
        return _emit_many(store.fact_history(args.fact_id), args, heading=f"Fact {args.fact_id} history")
    if args.proposal_command == "list":
        proposals = store.list_proposals(status=args.status)
        return _emit_many(proposals, args, heading=f"Memory proposals ({args.status})", formatter=_proposal_line)
    if args.proposal_command == "accept":
        fact = store.accept_proposal(args.proposal_id, state=args.state)
        return _emit(fact, args, text=f"Proposal accepted as {fact.state}: {_fact_line(fact)}")
    proposal = store.reject_proposal(args.proposal_id)
    return _emit(proposal, args, text=f"Proposal {proposal.id} rejected; Story Bible was not changed.")


def _dispatch_document(args: argparse.Namespace, store: ProjectStore) -> int:
    if args.document_command == "create":
        document = store.create_document(
            kind=args.kind,
            slug=args.slug,
            title=args.title,
            content=_content(args.content, args.file),
            state=args.state,
            parent=args.parent,
            sequence=args.sequence,
            tags=args.tag,
        )
        return _emit(document, args, text=_document_line(document))
    if args.document_command == "list":
        documents = store.list_documents(
            kind=args.kind, state=args.state, parent=args.parent, limit=args.limit
        )
        return _emit_many(documents, args, heading="Documents", formatter=_document_line)
    if args.document_command == "show":
        document = store.get_document(args.reference)
        if args.version is None:
            return _emit(document, args, text=_document_detail(document))
        version = store.get_version(document.id, args.version)
        return _emit(version, args, text=_version_detail(document, version))
    version = store.create_version(
        args.reference,
        content=_content(args.content, args.file),
        state=args.state,
        reason=args.reason,
    )
    return _emit(version, args, text=f"Created candidate v{version.version_number}; active text is unchanged.")


def _dispatch_chapter(args: argparse.Namespace, store: ProjectStore) -> int:
    if args.chapter_command in {"sync", "status", "diff", "import"}:
        sync = ChapterSync(store)
        if args.chapter_command == "diff":
            difference = sync.diff(args.reference)
            return _emit({"diff": difference}, args, text=difference or "No changes.")
        method = {"sync": sync.export, "status": sync.status, "import": sync.import_file}[args.chapter_command]
        references = [args.reference] if args.reference else [
            chapter.slug for chapter in store.list_documents(kind="chapter", limit=1000)
        ]
        results = [method(reference) for reference in references]
        return _emit_many(results, args, heading="Chapter files",
                          formatter=lambda item: f"{item.chapter}: {item.status} | {item.path}")
    if args.chapter_command == "create":
        chapter = store.create_document(
            kind="chapter",
            slug=args.slug,
            title=args.title,
            content=_content(args.content, args.file),
            sequence=args.number,
            tags=args.tag,
        )
        return _emit(chapter, args, text=_document_line(chapter))
    if args.chapter_command == "list":
        chapters = store.list_documents(kind="chapter", limit=args.limit)
        return _emit_many(chapters, args, heading="Chapters", formatter=_document_line)
    chapter = store.get_document(args.reference, kind="chapter")
    scenes = store.list_scenes(chapter=chapter.id)
    return _emit(
        {"chapter": chapter, "scenes": scenes},
        args,
        text=_document_detail(chapter) + "\n\nScenes\n" + "\n".join(_scene_line(scene) for scene in scenes),
    )


def _dispatch_scene(args: argparse.Namespace, config: LGConfig, store: ProjectStore) -> int:
    if args.scene_command == "create":
        scene = store.create_scene(
            slug=args.slug,
            title=args.title,
            chapter=args.chapter,
            sequence=args.sequence,
            pov=args.pov,
            narrative_tense=args.narrative_tense,
            time_label=args.time_label,
            location=args.location,
            goal=args.goal,
            characters=args.characters,
            conflict=args.conflict,
            required_information=args.required_information,
            emotional_change=args.emotional_change,
            end_state=args.end_state,
            word_min=args.word_min,
            word_max=args.word_max,
            tags=args.tag,
        )
        return _emit(scene, args, text=_scene_detail(scene))
    if args.scene_command == "set":
        changes = {
            key: value
            for key, value in vars(args).items()
            if key
            in {
                "pov",
                "narrative_tense",
                "time_label",
                "location",
                "goal",
                "characters",
                "conflict",
                "required_information",
                "emotional_change",
                "end_state",
                "word_min",
                "word_max",
            }
        }
        scene = store.update_scene_card(args.reference, **changes)
        return _emit(scene, args, text=_scene_detail(scene))
    if args.scene_command == "list":
        scenes = store.list_scenes(chapter=args.chapter, limit=args.limit)
        return _emit_many(scenes, args, heading="Scenes", formatter=_scene_line)
    if args.scene_command == "show":
        scene = store.get_scene(args.reference)
        return _emit(scene, args, text=_scene_detail(scene))
    if args.scene_command == "approve":
        scene = store.approve_scene(args.reference)
        return _emit(scene, args, text=f"Approved scene plan for `{scene.document.slug}`. It is now eligible for drafting.")
    if args.scene_command == "accept":
        from .supervision import review_for_acceptance

        review_for_acceptance(config, store, args.reference, args.version, final=args.final, on_event=_renderer(args))
        document = store.accept_version(args.reference, args.version, final=args.final)
        return _emit(document, args, text=f"Accepted v{document.active_version_number} as {document.state} for `{document.slug}`.")
    renderer = _renderer(args)
    service = StoryWorkflowService(config, project=store)
    request = " ".join(args.request)
    if args.scene_command == "plan":
        result = service.plan_scene(args.reference, request=request, dry_run=args.dry_run, on_event=renderer)
    else:
        result = service.draft_scene(args.reference, request=request, dry_run=args.dry_run, on_event=renderer)
    return _emit_story_result(result, args)


def _dispatch_version(args: argparse.Namespace, store: ProjectStore) -> int:
    if args.version_command == "list":
        versions = store.list_versions(args.reference)
        return _emit_many(versions, args, heading=f"Versions for {args.reference}", formatter=_version_line)
    if args.version_command == "show":
        document = store.get_document(args.reference)
        version = store.get_version(document.id, args.version)
        return _emit(version, args, text=_version_detail(document, version))
    if args.version_command == "diff":
        before = store.get_version(args.reference, args.from_version)
        after = store.get_version(args.reference, args.to_version)
        lines = list(
            difflib.unified_diff(
                before.content.splitlines(),
                after.content.splitlines(),
                fromfile=f"{args.reference}@v{before.version_number}",
                tofile=f"{args.reference}@v{after.version_number}",
                lineterm="",
            )
        )
        diff = "\n".join(lines) or "No textual differences."
        return _emit({"from": before, "to": after, "diff": diff}, args, text=diff)
    if args.version_command == "accept":
        document = store.accept_version(args.reference, args.version, final=args.final)
        return _emit(document, args, text=f"Accepted v{document.active_version_number} as {document.state}.")
    if args.version_command == "reject":
        version = store.reject_version(args.reference, args.version)
        return _emit(version, args, text=f"Rejected v{version.version_number}; active text was not changed.")
    version = store.restore_version(args.reference, args.version, reason=args.reason)
    return _emit(version, args, text=f"Restored source as new candidate v{version.version_number}; review before accepting.")


def _dispatch_review(args: argparse.Namespace, store: ProjectStore) -> int:
    if args.review_command == "consistency":
        report = run_consistency_check(store, document=args.document)
        if not report.issues:
            text = f"No deterministic consistency doubts found across {report.checked_documents} document(s)."
        else:
            text = _review_heading(report.errors, report.warnings) + "\n" + "\n".join(
                _issue_line(issue) for issue in report.issues
            )
        payload = {
            "document": report.document,
            "errors": report.errors,
            "warnings": report.warnings,
            "checked_documents": report.checked_documents,
            "checked_facts": report.checked_facts,
            "issues": report.issues,
        }
        return _emit(payload, args, text=text)
    if args.review_command == "chapter":
        report = chapter_quality_report(store, args.reference)
        text = (
            f"Chapter quality: {report.document.title}\n"
            f"  words: {report.word_count}\n"
            f"  paragraphs: {report.paragraph_count}\n"
            f"  dialogue ratio: {report.dialogue_ratio:.1%}\n"
            f"  ending hook signal: {'yes' if report.ending_hook_signal else 'no'}\n"
            f"  accepted scenes: {report.accepted_scene_count}/{report.scene_count}"
        )
        if report.issues:
            text += "\n\nDoubts\n" + "\n".join(_issue_line(issue) for issue in report.issues)
        return _emit(report, args, text=text)
    issues = store.list_review_issues(document=args.document, status=args.status)
    return _emit_many(issues, args, heading=f"Review issues ({args.status})", formatter=_issue_line)


def _dispatch_edit(args: argparse.Namespace, config: LGConfig, store: ProjectStore) -> int:
    service = StoryWorkflowService(config, project=store)
    result = service.revise_document(
        args.reference,
        mode=args.mode,
        request=" ".join(args.request),
        source_version=args.source_version,
        dry_run=args.dry_run,
        on_event=_renderer(args),
        passage=args.passage,
    )
    return _emit_story_result(result, args)


def _dispatch_timeline(args: argparse.Namespace, store: ProjectStore) -> int:
    if args.timeline_command == "set":
        entry = store.update_timeline_entry(
            args.entry_id, label=args.label, event=args.event, sort_key=args.order,
            state=args.state, tags=args.tag, source_document=args.source,
        )
        return _emit(entry, args, text=_timeline_line(entry))
    if args.timeline_command == "add":
        entry = store.add_timeline_entry(
            label=args.label,
            event=" ".join(args.event),
            sort_key=args.order,
            state=args.state,
            tags=args.tag,
            source_document=args.source,
        )
        return _emit(entry, args, text=_timeline_line(entry))
    entries = store.list_timeline(state=args.state)
    return _emit_many(entries, args, heading="Timeline", formatter=_timeline_line)


def _dispatch_foreshadowing(args: argparse.Namespace, store: ProjectStore) -> int:
    if args.foreshadowing_command == "add":
        item = store.add_foreshadowing(
            title=args.title,
            setup_note=" ".join(args.setup),
            setup_document=args.document,
            tags=args.tag,
            state=args.state,
        )
        return _emit(item, args, text=_foreshadowing_line(item))
    if args.foreshadowing_command == "list":
        items = store.list_foreshadowing(state=args.state)
        return _emit_many(items, args, heading="Foreshadowing", formatter=_foreshadowing_line)
    item = store.resolve_foreshadowing(
        args.foreshadow_id,
        payoff_note=" ".join(args.payoff),
        payoff_document=args.document,
        state="abandoned" if args.abandon else "paid",
    )
    return _emit(item, args, text=_foreshadowing_line(item))


def _dispatch_export(args: argparse.Namespace, store: ProjectStore) -> int:
    result = export_project(
        store,
        target=args.target,
        format=args.format,
        output_path=args.output,
        include_drafts=args.include_drafts,
    )
    return _emit(result, args, text=f"Exported {result.target} to {result.path} ({result.document_count} item(s)).")


def _dispatch_search(args: argparse.Namespace, store: ProjectStore) -> int:
    documents = store.search_documents(args.query, kinds=args.kind, tags=args.tag, limit=args.limit)
    facts = store.list_facts(query=args.query, tags=args.tag, limit=args.limit)
    text = "Documents\n" + ("\n".join(_document_line(item) for item in documents) or "  none")
    text += "\n\nStory Bible\n" + ("\n".join(_fact_line(item) for item in facts) or "  none")
    return _emit({"documents": documents, "facts": facts}, args, text=text)


def _renderer(args: argparse.Namespace) -> RunEventRenderer:
    return RunEventRenderer(
        json_mode=bool(getattr(args, "json", False)),
        quiet=bool(getattr(args, "quiet", False)),
        debug=bool(getattr(args, "debug", False)),
    )


def _emit_story_result(result: StoryTaskResult, args: argparse.Namespace) -> int:
    if getattr(args, "json", False):
        print(json.dumps({"type": "story.result", **_jsonable(result)}, ensure_ascii=False))
    elif result.error:
        print(f"Literary workflow failed: {result.error}", file=sys.stderr)
        print(f"Run: {result.run_id}", file=sys.stderr)
    else:
        print("\n" + result.text.rstrip())
        print(f"\nRun: {result.run_id}")
        if result.artifact_path:
            print(f"Artifact: {result.artifact_path}")
    return result.exit_code


def _emit(value: Any, args: argparse.Namespace, *, text: str, exit_code: int = 0) -> int:
    if getattr(args, "json", False):
        print(json.dumps(_jsonable(value), ensure_ascii=False))
    else:
        print(text)
    return exit_code


def _emit_many(
    values: list[Any],
    args: argparse.Namespace,
    *,
    heading: str,
    formatter: Any | None = None,
) -> int:
    if getattr(args, "json", False):
        print(json.dumps(_jsonable(values), ensure_ascii=False))
        return 0
    print(f"{heading} ({len(values)})")
    if not values:
        print("  none")
        return 0
    for value in values:
        print("  " + (formatter(value) if formatter else str(value)))
    return 0


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _content(inline: str, file_path: Path | None) -> str:
    if file_path is None:
        return inline
    if inline:
        raise ValueError("Use either --content or --file, not both.")
    return file_path.expanduser().resolve().read_text(encoding="utf-8")


def _project_target(workspace: Path, name: str, raw_path: str | None) -> Path:
    if raw_path:
        path = Path(raw_path).expanduser()
        return path.resolve() if path.is_absolute() else (workspace / path).resolve()
    directory_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", name).strip(". ") or "novel-project"
    return (workspace / directory_name).resolve()


def _project_info_text(project: Any, counts: dict[str, int]) -> str:
    return (
        f"Project: {project.name}\n"
        f"  id: {project.project_id}\n"
        f"  workspace: {project.workspace}\n"
        f"  documents: {counts['documents']}\n"
        f"  canonical facts: {counts['canonical_facts']}\n"
        f"  ideas: {counts['ideas']}\n"
        f"  pending memory proposals: {counts['open_proposals']}"
    )


def _document_line(document: Any) -> str:
    sequence = f"#{document.sequence} " if document.sequence is not None else ""
    return f"[{document.kind}/{document.state}] {sequence}{document.slug}: {document.title} (v{document.active_version_number or '-'})"


def _document_detail(document: Any) -> str:
    tags = ", ".join(document.tags) or "none"
    return (
        f"{document.title}\n"
        f"  id/slug: {document.id} / {document.slug}\n"
        f"  kind/state: {document.kind} / {document.state}\n"
        f"  parent/sequence: {document.parent_id or '-'} / {document.sequence if document.sequence is not None else '-'}\n"
        f"  active version: {document.active_version_number or '-'}\n"
        f"  tags: {tags}\n\n{document.content}"
    )


def _version_line(version: Any) -> str:
    return f"v{version.version_number} [{version.state}] {version.created_at} - {version.reason}"


def _version_detail(document: Any, version: Any) -> str:
    return f"{document.slug} v{version.version_number} [{version.state}]\nReason: {version.reason}\n\n{version.content}"


def _fact_line(fact: Any) -> str:
    tags = f" tags={','.join(fact.tags)}" if fact.tags else ""
    return f"#{fact.id} [{fact.state}] {fact.category}/{fact.key} = {fact.value}{tags}"


def _fact_detail(fact: Any) -> str:
    return _fact_line(fact) + f"\n  source: {fact.source_document_id or '-'}@{fact.source_version_id or '-'}\n  reason: {fact.rationale or '-'}"


def _proposal_line(proposal: Any) -> str:
    return f"#{proposal.id} [{proposal.status}] {proposal.category}/{proposal.key} = {proposal.value} ({proposal.rationale or 'no rationale'})"


def _scene_line(scene: Any) -> str:
    return (
        f"[{scene.status}/{scene.plan_state}] {scene.document.slug}: {scene.document.title} "
        f"chapter={scene.chapter_id or '-'} seq={scene.document.sequence if scene.document.sequence is not None else '-'}"
    )


def _scene_detail(scene: Any) -> str:
    return (
        f"{_scene_line(scene)}\n"
        f"  POV/tense: {scene.pov or '-'} / {scene.narrative_tense or '-'}\n"
        f"  time/location: {scene.time_label or '-'} / {scene.location or '-'}\n"
        f"  characters: {', '.join(scene.characters) or '-'}\n"
        f"  goal: {scene.goal or '-'}\n"
        f"  conflict: {scene.conflict or '-'}\n"
        f"  reveal: {scene.required_information or '-'}\n"
        f"  emotion: {scene.emotional_change or '-'}\n"
        f"  end: {scene.end_state or '-'}\n"
        f"  words: {scene.word_min or '-'}..{scene.word_max or '-'}\n\n"
        f"Plan\n{scene.plan or '(not planned)'}\n\n"
        f"Active text\n{scene.document.content or '(no accepted text)'}"
    )


def _timeline_line(entry: Any) -> str:
    order = f"{entry.sort_key} " if entry.sort_key else ""
    return f"#{entry.id} [{entry.state}] {order}{entry.label}: {entry.event}"


def _foreshadowing_line(item: Any) -> str:
    payoff = f" -> {item.payoff_note}" if item.payoff_note else ""
    return f"#{item.id} [{item.state}] {item.title}: {item.setup_note}{payoff}"


def _issue_line(issue: Any) -> str:
    actions = " | ".join(issue.suggested_actions)
    return f"[{issue.severity.upper()}] {issue.category}: {issue.message}\n    evidence: {issue.evidence}\n    options: {actions or 'review manually'}"


def _review_heading(errors: int, warnings: int) -> str:
    return f"Consistency doubts: {errors} error(s), {warnings} warning(s). No text was modified."
