"""Finite literary tasks and evidence queries through the shared Backend."""
import asyncio
import json
from pathlib import Path


def add_parsers(sub):
    task = sub.add_parser('task', help='Preview, run and resume a bounded literary task.')
    commands = task.add_subparsers(dest='task_command', required=True)
    for name in ('preview', 'start'):
        child = commands.add_parser(name)
        child.add_argument('--input', type=Path, required=True, help='Task options JSON; never credentials.')
        child.add_argument('--confirm', action='store_true')
        child.add_argument('--json', action='store_true')
    for name in ('show', 'advance', 'select'):
        child = commands.add_parser(name)
        child.add_argument('task_id')
        if name == 'select': child.add_argument('option_id')
        child.add_argument('--confirm', action='store_true')
        child.add_argument('--json', action='store_true')
    clarify = commands.add_parser('clarify', help='Print a new-task options draft; never execute or resume.')
    clarify.add_argument('task_id')
    clarify.add_argument('--guidance', required=True, help='Specific author clarification appended to the original request.')
    clarify.add_argument('--json', action='store_true')
    narrative = sub.add_parser('narrative', help='Preview or run a fresh registered-knowledge inference.')
    commands = narrative.add_subparsers(dest='narrative_command', required=True)
    for name in ('preview', 'run'):
        child = commands.add_parser(name)
        child.add_argument('--input', type=Path, required=True)
        child.add_argument('--confirm', action='store_true')
        child.add_argument('--json', action='store_true')
    identity = sub.add_parser('identity', help='Read imported package and runtime provenance.')
    identity.add_argument('--verify-manifest', action='store_true', help='Verify local binary hash/pin without running it.')
    identity.add_argument('--json', action='store_true')
    operation = sub.add_parser('operation', help='Query a formal operation commit receipt; never replay it.')
    operation.add_argument('operation_id')
    operation.add_argument('--json', action='store_true')
    sources = sub.add_parser('sources', help='Inspect known direct source impact and explicit re-review actions.')
    sources.add_argument('document')
    sources.add_argument('--version', type=int)
    sources.add_argument('--json', action='store_true')


def dispatch(args, config):
    from .cli_backend import _with_client
    from .terminal_backend import TerminalBackendClient

    async def execute():
        reader = TerminalBackendClient.readonly(config)
        try:
            if args.command == 'identity':
                value = await reader.backend.dispatch('runtime/identity', {'verifyManifest': args.verify_manifest})
            elif args.command == 'operation':
                value = await reader.backend.dispatch('operation/read', {'operationId': args.operation_id})
            elif args.command == 'sources':
                value = await reader.backend.dispatch('sources/check', {'document':args.document,
                    **({'version':args.version} if args.version is not None else {})})
            elif args.command == 'task' and args.task_command == 'show':
                value = await reader.backend.dispatch('task/read', {'taskId':args.task_id})
            elif args.command == 'task' and args.task_command == 'clarify':
                from .literary_tasks import clarified_options
                task = await reader.backend.dispatch('task/read', {'taskId':args.task_id})
                value = clarified_options(task,args.guidance)
            else:
                kind = args.command
                name = args.task_command if kind == 'task' else args.narrative_command
                if name in {'preview', 'start', 'run'}:
                    options = json.loads(args.input.read_text(encoding='utf-8'))
                    if not isinstance(options, dict): raise ValueError('Expected a task options object.')
                    value = await reader.backend.dispatch(kind + '/preview', {'options':options})
                    params = {'options':options, 'expectedFingerprint':value.get('fingerprint')}
                    method = 'task/start' if kind == 'task' else 'narrative/run'
                else:
                    value = await reader.backend.dispatch('task/read', {'taskId':args.task_id})
                    params = {'taskId':args.task_id, 'expectedFingerprint':value['fingerprint']}
                    method = 'task/' + name
                    if name == 'select': params['optionId'] = args.option_id
                if name == 'preview' or value.get('status') in {'unsupported','insufficient_context','stopped'}:
                    print(json.dumps(value, ensure_ascii=False, indent=2))
                    return 0 if name == 'preview' else 2
                expected = params['expectedFingerprint']
                if method == 'task/select':
                    decision = value.get('decision') or {}
                    shown = await reader.backend.dispatch('studio/preview', {'operation':'select-decision', 'arguments':{
                        'slug':decision['slug'], 'options_version_number':decision['optionsVersionNumber'],
                        'option_id':args.option_id, 'expected_version_id':decision['expectedVersionId']}})
                    expected = shown['fingerprint']
                if not args.confirm and not getattr(args, 'approve_fingerprint', None):
                    print(json.dumps({'status':'confirmation_required', 'fingerprint':expected, 'preview':value,
                        'message':'Review the exact task, then repeat with --confirm or --approve-fingerprint.'}, ensure_ascii=False, indent=2))
                    return 2
                if getattr(args, 'approve_fingerprint', None) not in {None, expected}:
                    raise ValueError('Approved fingerprint differs from the current preview.')
                async def action(console):
                    finished = await console.client._owned_call(method, params, confirm_fingerprint=expected)
                    if console.required: return 2
                    if finished['status'] != 'completed':
                        print(json.dumps(finished, ensure_ascii=False)); return 130 if finished['status']=='cancelled' else 1
                    result = finished['result']
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                    return 0 if result.get('status') not in {'failed','stopped','stale','unsupported','insufficient_context'} else 2
                return await _with_client(config, args, action)
            print(json.dumps(value, ensure_ascii=False, indent=2))
            return 0
        finally:
            await reader.close()
    return asyncio.run(execute())
