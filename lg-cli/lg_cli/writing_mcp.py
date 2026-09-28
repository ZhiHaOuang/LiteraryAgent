"""Project-scoped writing tools. All protocol output is owned by the MCP SDK."""

from __future__ import annotations

import argparse
import difflib
import functools
import inspect
from dataclasses import asdict
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .config import LGConfig, load_config
from .knowledge import KnowledgeGateway
from .project_store import ProjectStore


def create_server(config: LGConfig, *, control_socket: str | None = None) -> FastMCP:
    server = FastMCP("LiteraryGiant writing tools", log_level="ERROR")
    store = ProjectStore(config.workspace)

    def approved(function):
        @functools.wraps(function)
        def call(*args, **kwargs):
            if control_socket:
                from .workflow_control import control_request
                details = dict(inspect.signature(function).bind(*args, **kwargs).arguments)
                result = control_request(control_socket, 'approval', details={
                    'tool': function.__name__, 'arguments': details})
                if result.get('decision') != 'accept':
                    raise ValueError('Operation was not approved; no changes were made.')
            return function(*args, **kwargs)
        return call


    @server.tool()
    def project_memory() -> dict:
        """Read shared project progress, canonical facts, and chapter index."""
        from .book_overview import read_overview
        return {
            "book_overview": read_overview(config.workspace),
            "summary": store.summary(),
            "facts": [asdict(f) for f in store.list_facts()],
            "documents": [
                {
                    "id": d.id,
                    "slug": d.slug,
                    "title": d.title,
                    "kind": d.kind,
                    "active_version_id": d.active_version_id,
                }
                for d in store.list_documents()
            ],
        }

    @server.tool()
    def read_document(slug: str) -> dict:
        """Read active manuscript and its version ID before drafting or editing."""
        return asdict(store.get_document(slug))

    @server.tool()
    @approved
    def create_chapter(slug: str, title: str, content: str, sequence: int, book_overview: str,
        story_index: dict | None = None) -> dict:
        """Create a chapter requested by the author. Include story_index describing explicit characters/events/relationships/links in this exact version, not inferred future facts."""
        from .book_overview import validate_overview, save_overview
        validate_overview(book_overview)
        if story_index is not None:
            from .story_index import validate_graph
            validate_graph(story_index)
        result = asdict(
            store.create_document(
                kind="chapter",
                slug=slug,
                title=title,
                content=content,
                sequence=sequence,
                metadata={"story_index": story_index} if story_index is not None else {},
            )
        )
        save_overview(config.workspace, book_overview, source={'document': slug, 'version': result['active_version_id']})
        return result

    @server.tool()
    @approved
    def edit_manuscript(
        slug: str, content: str, expected_version_id: int, reason: str, book_overview: str,
        story_index: dict | None = None,
    ) -> dict:
        """Apply an author-directed prose edit immediately with version backup. Reject stale edits."""
        from .book_overview import validate_overview, save_overview
        validate_overview(book_overview)
        document = store.get_document(slug)
        if document.kind not in {"chapter", "scene", "note"}:
            raise ValueError(
                "This tool only edits prose. Discuss setting changes separately."
            )
        result = asdict(
            store.edit_active_document(
                slug,
                content=content,
                expected_version_id=expected_version_id,
                reason=reason,
                story_index=story_index,
            )
        )
        save_overview(config.workspace, book_overview, source={'document': slug, 'version': result['id']})
        return result

    @server.tool()
    @approved
    def update_book_overview(text: str) -> dict:
        """Save one concise Chinese sentence about the whole book after generating prose through other tools. Use known book context, not a chapter recap; this does not promote draft facts to canon."""
        from .book_overview import save_overview, read_overview
        save_overview(config.workspace, text, source={'tool': 'update_book_overview'})
        return {'book_overview': read_overview(config.workspace)}

    @server.tool()
    def document_history(slug: str) -> list[dict]:
        """Read saved manuscript revisions for comparison or an author-requested undo."""
        return [asdict(v) for v in store.list_versions(slug)]

    @server.tool()
    def document_diff(slug: str, before_version: int, after_version: int) -> str:
        """Show exact differences between two saved version numbers."""
        before = store.get_version(slug, before_version)
        after = store.get_version(slug, after_version)
        return "".join(
            difflib.unified_diff(
                before.content.splitlines(keepends=True),
                after.content.splitlines(keepends=True),
                fromfile=f"version-{before_version}",
                tofile=f"version-{after_version}",
            )
        )

    @server.tool()
    @approved
    def restore_manuscript(
        slug: str, version_number: int, expected_version_id: int
    ) -> dict:
        """Undo by activating an exact copy of a saved prose revision; keep all history."""
        document = store.get_document(slug)
        if document.kind not in {"chapter", "scene", "note"}:
            raise ValueError("This tool only restores prose, not settings.")
        previous = store.get_version(slug, version_number)
        return asdict(
            store.edit_active_document(
                slug,
                content=previous.content,
                expected_version_id=expected_version_id,
                reason=f"Author requested restore of version {version_number}",
            )
        )

    @server.tool()
    def reference_search(query: str) -> dict:
        """Retrieve references through KnowledgeGateway; raw source text is disabled."""
        context = KnowledgeGateway(config).search(query, allow_raw=False)
        return {"context": context.to_prompt_text(), "metadata": context.to_metadata()}

    @server.tool()
    @approved
    def propose_setting(category: str, key: str, value: str, rationale: str) -> dict:
        """Record a setting proposal for discussion; never overwrite Canonical facts."""
        return asdict(
            store.propose_fact(
                category=category, key=key, value=value, rationale=rationale
            )
        )

    if control_socket:
        from .workflow_control import control_request

        @server.tool()
        def workflow_status() -> dict:
            """Main agent: inspect the current UI-owned command before directing it."""
            return control_request(control_socket, "status")

        @server.tool()
        def stop_workflow(job_id: str) -> dict:
            """Main agent: stop the identified active workflow when the author requests it."""
            return control_request(control_socket, "stop", job_id=job_id)

        @server.tool()
        def retry_workflow(job_id: str, guidance: str = "") -> dict:
            """Main agent: retry a failed/stopped workflow on author request. Optional author guidance reaches every model stage. To redirect active work, stop it first, then retry with guidance. Never retry successful writes."""
            return control_request(control_socket, "retry", job_id=job_id, guidance=guidance)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--control-socket")
    args = parser.parse_args()
    create_server(load_config(args.workspace), control_socket=args.control_socket).run(transport="stdio")


if __name__ == "__main__":
    main()
