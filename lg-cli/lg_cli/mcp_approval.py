"""Fail-closed, single-call approvals for the direct app-server client."""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field


class WriteConfirmation(BaseModel):
    # Pinned app-server 0.154.0 accepts title on properties, not on the root form.
    model_config = ConfigDict(json_schema_extra=lambda schema: schema.pop('title', None))
    confirm: bool = Field(description='Approve this exact LG writing operation once')


READ_TOOLS = frozenset({'project_memory', 'read_document', 'document_history',
    'document_diff', 'reference_search', 'book_dashboard', 'studio_status', 'studio_review_context',
    'book_library', 'book_entry', 'book_action_context', 'studio_decision', 'studio_source_impact',
    'studio_knowledge', 'studio_knowledge_view'})
WRITE_TOOLS = frozenset({'create_chapter', 'edit_manuscript', 'update_book_overview',
    'restore_manuscript', 'propose_setting', 'studio_initialize', 'studio_save',
    'studio_draft', 'studio_review', 'studio_accept', 'studio_reference',
    'studio_adopt_reference', 'studio_accept_proposal', 'studio_decision_options',
    'studio_select_decision', 'studio_withdraw_decision', 'studio_knowledge_propose',
    'studio_knowledge_confirm', 'studio_knowledge_withdraw'})


def operation_details(workspace: Path, name: str, arguments: dict) -> dict:
    """Bind approval to saved source text rather than trusting an Agent's description."""
    from dataclasses import asdict
    from .project_store import ProjectStore
    details = {'tool': name, 'arguments': arguments}
    store = ProjectStore(workspace)
    if name in {'studio_accept', 'studio_adopt_reference', 'restore_manuscript'}:
        document = store.get_document(arguments['slug'])
        version = store.get_version(arguments['slug'], arguments['version_number'])
        details['preview'] = {'title': document.title, 'active_version_id': document.active_version_id,
            'version_id': version.id, 'state': version.state,
            'content_sha256': hashlib.sha256(version.content.encode()).hexdigest(),
            'diff': ''.join(difflib.unified_diff(document.content.splitlines(keepends=True),
                version.content.splitlines(keepends=True), fromfile='active', tofile='candidate'))}
    elif name == 'studio_select_decision':
        details['preview'] = store.decision_selection_preview(arguments['slug'],
            options_version_number=arguments['options_version_number'], option_id=arguments['option_id'],
            expected_version_id=arguments['expected_version_id'])
    elif name == 'studio_withdraw_decision':
        details['preview'] = store.decision_withdrawal_preview(arguments['slug'],
            expected_version_id=arguments['expected_version_id'])
    elif name == 'studio_knowledge_confirm':
        details['preview'] = store.knowledge_confirmation_preview(arguments['slug'],
            proposal_version_number=arguments['proposal_version_number'],
            expected_version_id=arguments['expected_version_id'])
    elif name == 'studio_knowledge_withdraw':
        details['preview'] = store.knowledge_withdrawal_preview(arguments['slug'],
            expected_version_id=arguments['expected_version_id'])
    elif name == 'studio_accept_proposal':
        proposal = store.get_proposal(arguments['proposal_id'])
        if proposal.status!='proposed':
            raise ValueError('Proposal is missing or no longer pending; refresh before approval.')
        details['preview'] = asdict(proposal)
        details['source'] = store.proposal_source_status(proposal.id)
        if details['source']['status'] not in {'current', 'unbound'}:
            raise ValueError('Proposal source is ' + details['source']['status'] + '; refresh and verify the source before approval.')
    return details


def project_fingerprint(workspace: Path) -> str:
    """Detect saved project changes while a write waits for an author decision."""
    for _ in range(3):
        try:
            return _project_fingerprint_pass(workspace)
        except FileNotFoundError:
            # SQLite can checkpoint and remove its WAL between exists and read.
            # Restart with the database too; retaining its earlier bytes is stale.
            continue
    raise ValueError('Project state changed while computing its fingerprint; refresh and retry.') from None


def _project_fingerprint_pass(workspace: Path) -> str:
    digest = hashlib.sha256()
    paths = [workspace / relative for relative in ('.literarygiant/story.sqlite3', '.literarygiant/story.sqlite3-wal',
                     '.literarygiant/project.json', '.literarygiant/config.toml',
                     '.literarygiant/supervision.json', 'ReferenceLibrary/bible/book-overview.json')]
    paths.extend(sorted((workspace / 'manuscript/chapters').glob('*.md')))
    for path in paths:
        if not path.resolve().is_relative_to(workspace.resolve()):
            raise ValueError('Project state points outside this workspace')
        digest.update(str(path.relative_to(workspace)).encode())
        data = path.read_bytes() if path.exists() else b'<missing>'
        digest.update(b'<missing>' if path.name.endswith('-wal') and data == b'' else data)
    return digest.hexdigest()


class ApprovingMCP(FastMCP):
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.write_lock = asyncio.Lock()
        super().__init__('LiteraryGiant writing tools', log_level='ERROR')

    async def call_tool(self, name, arguments):
        if name in READ_TOOLS:
            return await super().call_tool(name, arguments)
        if name not in WRITE_TOOLS:
            raise ValueError('Tool has no declared LG approval policy')
        # Serialize the complete approval/write cycle, not just the final mutation.
        async with self.write_lock:
            details = operation_details(self.workspace, name, arguments)
            before = project_fingerprint(self.workspace)
            response = await self.get_context().elicit(
                message='LG 写作操作：请确认以下准确内容。拒绝或取消不会写入。\n'
                    + json.dumps(details, ensure_ascii=False, indent=2),
                schema=WriteConfirmation)
            if response.action != 'accept' or response.data is None or not response.data.confirm:
                raise ValueError('Operation was not approved; no changes were made.')
            if project_fingerprint(self.workspace) != before:
                raise ValueError('Project changed during approval; review and request a new operation.')
            return await super().call_tool(name, arguments)
