"""Scriptable author-confirmed entry to the exact workbench tool surface."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

OPERATIONS = {'status': 'studio_status', 'dashboard': 'book_dashboard',
    'initialize': 'studio_initialize', 'save': 'studio_save', 'draft': 'studio_draft',
    'review-context': 'studio_review_context', 'review': 'studio_review',
    'accept': 'studio_accept', 'overview': 'update_book_overview',
    'library': 'book_library', 'entry': 'book_entry', 'action-context': 'book_action_context',
    'reference': 'studio_reference', 'adopt-reference': 'studio_adopt_reference',
    'accept-proposal': 'studio_accept_proposal'}
READ_ONLY = {'status', 'dashboard', 'review-context', 'library', 'entry', 'action-context'}


def add_parser(sub):
    parser = sub.add_parser('studio', help='Run the shared LG writing workbench operations.')
    parser.add_argument('operation', choices=OPERATIONS)
    parser.add_argument('--input', type=Path, help='JSON object containing the operation arguments.')
    parser.add_argument('--confirm', action='store_true', help='Explicitly approve this exact write once.')
    parser.add_argument('--json', action='store_true', help='Results are always JSON.')


def dispatch(args, config):
    from .writing_mcp import create_server
    arguments = json.loads(args.input.read_text(encoding='utf-8')) if args.input else {}
    if not isinstance(arguments, dict):
        raise ValueError('Studio input must be a JSON object')
    if args.operation not in READ_ONLY and not args.confirm:
        print(json.dumps({'status': 'confirmation_required', 'operation': args.operation,
            'arguments': arguments, 'message': 'No write performed. Review these arguments, then repeat with --confirm.'}, ensure_ascii=False))
        return 2
    async def invoke():
        return await create_server(config).call_tool(OPERATIONS[args.operation], arguments)
    result = asyncio.run(invoke())
    # FastMCP returns (content blocks, structured result) for object-returning tools.
    if isinstance(result, tuple):
        result = result[1]
    else:
        result = json.loads(result[0].text)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
