"""Scriptable author-confirmed entry to the exact workbench tool surface."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .studio_operations import OPERATIONS,prepare


def add_parser(sub):
    parser = sub.add_parser('studio', help='Run the shared LG writing workbench operations.')
    parser.add_argument('operation', choices=OPERATIONS)
    parser.add_argument('--input', type=Path, help='JSON object containing the operation arguments.')
    parser.add_argument('--confirm', action='store_true', help='Explicitly approve this exact write once.')
    parser.add_argument('--json', action='store_true', help='Results are always JSON.')


def read_arguments(args):
    arguments = json.loads(args.input.read_text(encoding='utf-8')) if args.input else {}
    if not isinstance(arguments, dict):
        raise ValueError('Studio input must be a JSON object')
    return arguments


def confirmation(args,arguments,preview):
    return {'status':'confirmation_required','operation':args.operation,'arguments':arguments,
        'fingerprint':preview['fingerprint'],'preview':preview['details'],
        'message':'No write performed. Review this exact operation, then repeat with --confirm or --approve-fingerprint.'}


def dispatch(args, config):
    from .cli_backend import run_studio
    arguments=read_arguments(args)
    preview=asyncio.run(prepare(config,args.operation,arguments))
    if preview['write'] and not args.confirm and not getattr(args,'approve_fingerprint',None):
        print(json.dumps(confirmation(args,arguments,preview),ensure_ascii=False))
        return 2
    return run_studio(args,config,arguments,preview)
