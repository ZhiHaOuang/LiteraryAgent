"""Fail-closed, single-call approvals for the direct app-server client."""
from __future__ import annotations

import asyncio
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
    'book_library', 'book_entry', 'book_action_context'})
WRITE_TOOLS = frozenset({'create_chapter', 'edit_manuscript', 'update_book_overview',
    'restore_manuscript', 'propose_setting', 'studio_initialize', 'studio_save',
    'studio_draft', 'studio_review', 'studio_accept', 'studio_reference',
    'studio_adopt_reference', 'studio_accept_proposal'})


def project_fingerprint(workspace: Path) -> str:
    """Detect saved project changes while a write waits for an author decision."""
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
            before = project_fingerprint(self.workspace)
            details = json.dumps({'tool': name, 'arguments': arguments}, ensure_ascii=False, indent=2)
            response = await self.get_context().elicit(
                message='LG 写作操作：请确认以下准确内容。拒绝或取消不会写入。\n' + details,
                schema=WriteConfirmation)
            if response.action != 'accept' or response.data is None or not response.data.confirm:
                raise ValueError('Operation was not approved; no changes were made.')
            if project_fingerprint(self.workspace) != before:
                raise ValueError('Project changed during approval; review and request a new operation.')
            return await super().call_tool(name, arguments)
