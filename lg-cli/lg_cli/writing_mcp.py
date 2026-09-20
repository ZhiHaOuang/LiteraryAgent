"""Project-scoped writing tools. All protocol output is owned by the MCP SDK."""

from __future__ import annotations

import argparse
import difflib
from dataclasses import asdict
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .config import LGConfig, load_config
from .knowledge import KnowledgeGateway
from .project_store import ProjectStore


def create_server(config: LGConfig) -> FastMCP:
    server = FastMCP("LiteraryGiant writing tools", log_level="ERROR")
    store = ProjectStore(config.workspace)

    @server.tool()
    def project_memory() -> dict:
        """Read shared project progress, canonical facts, and chapter index."""
        return {
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
    def create_chapter(slug: str, title: str, content: str, sequence: int) -> dict:
        """Create a chapter requested by the author, retaining its initial version."""
        return asdict(
            store.create_document(
                kind="chapter",
                slug=slug,
                title=title,
                content=content,
                sequence=sequence,
            )
        )

    @server.tool()
    def edit_manuscript(
        slug: str, content: str, expected_version_id: int, reason: str
    ) -> dict:
        """Apply an author-directed prose edit immediately with version backup. Reject stale edits."""
        document = store.get_document(slug)
        if document.kind not in {"chapter", "scene", "note"}:
            raise ValueError(
                "This tool only edits prose. Discuss setting changes separately."
            )
        return asdict(
            store.edit_active_document(
                slug,
                content=content,
                expected_version_id=expected_version_id,
                reason=reason,
            )
        )

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
    def propose_setting(category: str, key: str, value: str, rationale: str) -> dict:
        """Record a setting proposal for discussion; never overwrite Canonical facts."""
        return asdict(
            store.propose_fact(
                category=category, key=key, value=value, rationale=rationale
            )
        )

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    create_server(load_config(args.workspace)).run(transport="stdio")


if __name__ == "__main__":
    main()
