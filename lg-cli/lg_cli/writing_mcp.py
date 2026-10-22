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


def create_server(config: LGConfig, *, control_socket: str | None = None, require_elicitation: bool = False,approval_handler=None) -> FastMCP:
    if approval_handler is not None and (control_socket or require_elicitation):
        raise ValueError('Choose one LG approval transport')
    if require_elicitation:
        from .mcp_approval import ApprovingMCP
        server = ApprovingMCP(config.workspace)
    else:
        server = FastMCP("LiteraryGiant writing tools", log_level="ERROR")
    store = ProjectStore(config.workspace)

    def observed(function):
        @functools.wraps(function)
        def call(*args, **kwargs):
            if not control_socket:
                return function(*args, **kwargs)
            from .generation_sources import make_observation
            from .workflow_control import control_request
            try:
                lease = control_request(control_socket, 'source_begin', tool=function.__name__)
            except Exception:
                raise ValueError('Generation source capture is unavailable; no read result was delivered.') from None
            try:
                value = function(*args, **kwargs)
            except BaseException:
                try:
                    control_request(control_socket, 'source_aborted', lease=lease)
                except Exception:
                    pass
                raise
            try:
                observation = make_observation(function.__name__, value)
                control_request(control_socket, 'source_observed', lease=lease, observation=observation)
            except Exception:
                raise ValueError('Generation source capture failed; no read result was delivered.') from None
            return value
        return call

    def candidate_result(document, version):
        from .output_writer import write_workflow_output
        output = write_workflow_output(config.workspace, 'candidate', version.content)
        path = str(output.output_path.relative_to(config.workspace))
        return {**asdict(version), 'applied': False, 'artifact': path,
            'diff': {'path': path, 'before': document.content, 'after': version.content, 'applied': False}}

    def approved(function):
        if approval_handler is not None:
            @functools.wraps(function)
            async def local_call(*args,**kwargs):
                from .mcp_approval import operation_details,project_fingerprint
                bound=inspect.signature(function).bind(*args,**kwargs);bound.apply_defaults()
                details=operation_details(config.workspace,function.__name__,dict(bound.arguments))
                before=project_fingerprint(config.workspace)
                decision=await approval_handler(details)
                if decision.get('decision')!='accept':raise ValueError('Operation was not approved; no changes were made.')
                if project_fingerprint(config.workspace)!=before:
                    raise ValueError('Project changed during approval; review and request a new operation.')
                from .generation_sources import bind_generation_sources
                from .operation_receipts import operation_scope
                with bind_generation_sources(decision.get('generation_sources')), operation_scope(
                        config.workspace, decision.get('operationId'), details, before):
                    return function(*args,**kwargs)
            return local_call
        @functools.wraps(function)
        def call(*args, **kwargs):
            receipt = None
            result, details, before = {}, {}, ''
            if control_socket:
                from .workflow_control import control_request
                from .mcp_approval import operation_details, project_fingerprint
                arguments = dict(inspect.signature(function).bind(*args, **kwargs).arguments)
                details = operation_details(config.workspace, function.__name__, arguments)
                before = project_fingerprint(config.workspace)
                result = control_request(control_socket, 'approval', details={
                    **details})
                if result.get('decision') != 'accept':
                    raise ValueError('Operation was not approved; no changes were made.')
                if project_fingerprint(config.workspace) != before:
                    raise ValueError('Project changed during approval; review and request a new operation.')
                receipt = result.get('generation_sources')
            from .generation_sources import bind_generation_sources
            from .operation_receipts import operation_scope
            with bind_generation_sources(receipt), operation_scope(
                    config.workspace, result.get('operationId'), details, before):
                return function(*args, **kwargs)
        return call


    @server.tool()
    @observed
    def project_memory() -> dict:
        """Read shared project progress for the author, confirmed facts with source validity, separate ideas and a document index.
        This is not a character/reader knowledge view. Stale sources need review, not automatic fact deletion.
        """
        from .book_overview import read_overview
        facts = store.list_facts(state='canonical')
        ideas = store.list_facts(state='idea',limit=30)
        sources = store.fact_sources([*facts,*ideas])
        documents = store.list_documents()
        summary = store.summary()
        decisions = store.decision_context()
        return {
            "book_overview": read_overview(config.workspace),
            "summary": summary,
            "facts": [{**asdict(f),'source':sources[f.id]} for f in facts],
            "ideas": [{**asdict(f),'source':sources[f.id]} for f in ideas],
            "view": {'purpose':'author_global','knowledge_filter_applied':False,
                'excluded_fact_states':['archived','rejected'],
                'limits':{'facts':300,'ideas':30,'creative_decisions':decisions['limit']},
                'truncated':{'facts':summary['canonical_facts']>len(facts),
                    'ideas':summary['ideas']>len(ideas),'documents':summary['documents']>len(documents),
                    'creative_decisions':decisions['truncated']}},
            'creative_decisions': decisions['decisions'],
            "documents": [
                {
                    "id": d.id,
                    "slug": d.slug,
                    "title": d.title,
                    "kind": d.kind,
                    "active_version_id": d.active_version_id,
                }
                for d in documents
            ],
        }

    @server.tool()
    @observed
    def read_document(slug: str) -> dict:
        """Read active manuscript and its version ID before drafting or editing."""
        return asdict(store.get_document(slug))

    @server.tool()
    @approved
    def create_chapter(slug: str, title: str, content: str, sequence: int, book_overview: str,
        story_index: dict | None = None) -> dict:
        """Create a candidate chapter, never accepted prose. Include story_index for this exact candidate, not inferred future facts."""
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
                state="candidate",
                sequence=sequence,
                metadata={"story_index": story_index} if story_index is not None else {},
            )
        )
        save_overview(config.workspace, book_overview, source={'document': slug, 'version': result['active_version_id'], 'state': 'candidate'})
        return result

    @server.tool()
    @approved
    def edit_manuscript(
        slug: str, content: str, expected_version_id: int, reason: str, book_overview: str,
        story_index: dict | None = None,
    ) -> dict:
        """Save an Agent-generated prose revision as a candidate. Never activate it automatically."""
        from .book_overview import validate_overview, save_overview
        validate_overview(book_overview)
        document = store.get_document(slug)
        if document.kind not in {"chapter", "scene", "note"}:
            raise ValueError(
                "This tool only edits prose. Discuss setting changes separately."
            )
        version = store.create_version(
                slug,
                content=content,
                expected_version_id=expected_version_id,
                reason=reason,
                metadata={"story_index": story_index} if story_index is not None else {},
            )
        result = candidate_result(document, version)
        save_overview(config.workspace, book_overview, source={'document': slug, 'version': result['id'], 'state': 'candidate'})
        return result

    @server.tool()
    @approved
    def update_book_overview(text: str) -> dict:
        """Save one concise Chinese sentence about the whole book after generating prose through other tools. Use known book context, not a chapter recap; this does not promote draft facts to canon."""
        from .book_overview import save_overview, read_overview
        save_overview(config.workspace, text, source={'tool': 'update_book_overview'})
        return {'book_overview': read_overview(config.workspace)}

    @server.tool()
    @observed
    def document_history(slug: str) -> list[dict]:
        """Read saved manuscript revisions for comparison or an author-requested undo."""
        return [asdict(v) for v in store.list_versions(slug)]

    @server.tool()
    @observed
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
        """Propose restoration of a saved prose version as a candidate; preserve the active text."""
        document = store.get_document(slug)
        if document.kind not in {"chapter", "scene", "note"}:
            raise ValueError("This tool only restores prose, not settings.")
        previous = store.get_version(slug, version_number)
        from .generation_sources import bind_generation_sources, inherited_receipt
        with bind_generation_sources(inherited_receipt(previous.metadata, previous.content)):
            version = store.create_version(
                slug,
                content=previous.content,
                expected_version_id=expected_version_id,
                reason=f"Author requested restore of version {version_number}",
                metadata={"restored_from_version": previous.version_number,
                    **({"required_decision":previous.metadata['required_decision']}
                        if 'required_decision' in previous.metadata else {}),
                    **({"story_index": previous.metadata["story_index"]} if "story_index" in previous.metadata else {})},
            )
        return candidate_result(document, version)

    @server.tool()
    @observed
    def reference_search(query: str) -> dict:
        """Retrieve references through KnowledgeGateway; raw source text is disabled."""
        context = KnowledgeGateway(config).search(query, allow_raw=False)
        return {"context": context.to_prompt_text(), "metadata": context.to_metadata()}

    @server.tool()
    @approved
    def propose_setting(category: str, key: str, value: str, rationale: str,
        source_document: str | None = None, source_version: int | None = None) -> dict:
        """Record a setting proposal for discussion; never overwrite Canonical facts.
        Bind extracted facts to the exact source document/version. Omit sources only for independent author design ideas.
        """
        return asdict(
            store.propose_fact(
                category=category, key=key, value=value, rationale=rationale,
                source_document=source_document, source_version=source_version,
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

    from .studio import register_tools
    register_tools(server, config, approved, observed=observed)
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--control-socket")
    parser.add_argument("--require-elicitation", action="store_true")
    parser.add_argument("--environment")
    args = parser.parse_args()
    create_server(load_config(args.workspace, environment=args.environment), control_socket=args.control_socket, require_elicitation=args.require_elicitation).run(transport="stdio")


if __name__ == "__main__":
    main()
