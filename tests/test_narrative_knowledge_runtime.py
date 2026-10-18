"""Real Runtime/MCP knowledge records, author approval and registered-only views."""
import asyncio
import io
import ipaddress
import json
import os
import socket
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from lg_cli.backend import Backend
from lg_cli.generation_sources import canonical_sha
from lg_cli.init_project import init_workspace
from lg_cli.main import main
from lg_cli.project_store import ProjectStore
from tests.helpers import make_config
from tests.test_anthropic_bridge import message_events
from tests.test_decision_review_runtime import objects


class NarrativeKnowledgeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def journey(self, engine):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.enterContext(patch.dict(os.environ, {'LITERARYGIANT_REGISTRY_HOME': str(root/'registry')}))
        self.enterContext(patch('lg_cli.credentials.read_profiles', return_value={'active': None, 'profiles': {}}))
        init_workspace(root)
        store = ProjectStore(root)
        store.create_document(kind='chapter', slug='one', title='正文', sequence=1,
            content='正文前缀保持。\n林砚站在城门前。\n正文后缀保持。', state='accepted')
        lin = store.create_document(kind='character', slug='lin', title='林砚', content='林砚的人物设定。', state='accepted')
        rival = store.create_document(kind='character', slug='rival', title='对手', content='对手的人物设定。', state='accepted')
        store.set_fact(category='world', key='father', value='CANONICAL_SECRET：父亲仍然活着。')
        source = store.create_document(kind='note', slug='evidence', title='旧证据', content='旧信件内容。', state='accepted')

        def proposal(slug, holder, content, *, audience='character', start=1, sources=None):
            return {'slug': slug, 'title': slug, 'topic': {'audience': audience,
                'holder_document_id': holder, 'key': slug, 'from_position': start},
                'statement': {'content': content, 'stance': 'believes', 'until_position': None,
                    'sources': [] if sources is None else sources},
                'rationale': '作者独立声明这个视角，不将其提升为世界真相。', 'expected_version_id': 0}

        # Explicit author setup creates contrasting records; the model only emits MCP calls.
        for arguments in (proposal('rival-private', rival.id, 'RIVAL_PRIVATE'),
                proposal('reader-reveal', None, 'READER_REVEAL', audience='reader'),
                proposal('lin-future', lin.id, 'LIN_FUTURE', start=20),
                proposal('lin-stale', lin.id, 'STALE_SECRET', sources=[{'document_id': source.id,
                    'version_id': source.active_version_id}])):
            seeded = store.propose_knowledge(**arguments)
            store.confirm_knowledge(seeded['slug'], proposal_version_number=seeded['proposal_version_number'],
                expected_version_id=seeded['version_id'])
        store.edit_active_document(source.id, content='作者改写后的信件。', reason='合成作者修改证据',
            expected_version_id=source.active_version_id)
        main_arguments = proposal('lin-belief', lin.id, 'LIN_FALSE_BELIEF：林砚相信父亲已经死去。')
        phase = {'name': '', 'step': 0}
        observed, errors, calls = {}, [], []
        record = None

        def last_result(payload):
            parts = [part for message in payload['messages'] if isinstance(message.get('content'), list)
                for part in message['content'] if isinstance(part, dict) and part.get('type') == 'tool_result']
            self.assertTrue(parts)
            return parts[-1]

        def response(payload):
            name, step = phase['name'], phase['step']
            phase['step'] += 1
            if name == 'propose' and step == 0:
                marker, arguments = 'Save a provisional narrative knowledge statement', main_arguments
            elif name == 'propose' and step == 1:
                value = next(value for value in reversed(list(objects(payload['messages'])))
                    if {'document_id', 'proposal_version_number', 'confirmed'} <= value.keys() and value.get('slug') == 'lin-belief')
                observed['proposal'] = value
                marker, arguments = 'Confirm exactly one saved narrative knowledge proposal', {
                    'slug': value['slug'], 'proposal_version_number': value['proposal_version_number'],
                    'expected_version_id': value['version_id']}
            elif name in {'decline', 'confirm'} and step == 0:
                marker, arguments = 'Confirm exactly one saved narrative knowledge proposal', {
                    'slug': record['slug'], 'proposal_version_number': record['proposal_version_number'],
                    'expected_version_id': record['version_id']}
            elif name in {'query', 'after-withdraw'} and step == 0:
                marker, arguments = 'Project confirmed narrative knowledge at one reading/story position', {
                    'audience': 'character', 'position': 10, 'holder_document': 'lin', 'limit': 100}
            elif name == 'query' and step == 1:
                observed['view'] = next(value for value in reversed(list(objects(payload['messages'])))
                    if value.get('schema') == 'lg.narrative-view.v1')
                marker, arguments = 'Read one versioned character/reader knowledge record', {'slug': 'lin-belief'}
            elif name == 'query' and step == 2:
                observed['knowledge'] = next(value for value in reversed(list(objects(payload['messages'])))
                    if {'document_id', 'proposal_version_number', 'confirmed'} <= value.keys() and value.get('slug') == 'lin-belief')
                marker, arguments = 'Withdraw the current confirmed narrative knowledge statement', {
                    'slug': record['slug'], 'expected_version_id': record['version_id']}
            elif name == 'withdraw' and step == 0:
                marker, arguments = 'Withdraw the current confirmed narrative knowledge statement', {
                    'slug': record['slug'], 'expected_version_id': record['version_id']}
            else:
                observed[name+'-last'] = last_result(payload)
                if name == 'after-withdraw':
                    observed['after'] = next(value for value in reversed(list(objects(payload['messages'])))
                        if value.get('schema') == 'lg.narrative-view.v1')
                return list(message_events(delta={'type': 'text_delta', 'text':
                    '仅登记并投影作者确认的认知；不改变世界事实，也不宣称隔离模型全部输入。'}))
            tool = next(tool for tool in payload['tools'] if marker in tool.get('description', ''))
            calls.append((name, marker))
            return list(message_events(block={'type': 'tool_use', 'id': 'knowledge-'+str(len(calls)),
                'name': tool['name'], 'input': {}}, delta={'type': 'input_json_delta',
                'partial_json': json.dumps(arguments, ensure_ascii=False)}, stop='tool_use'))

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(handler):
                try:
                    self.assertEqual(handler.path, '/v1/messages')
                    payload = json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))
                    events = response(payload)
                    handler.send_response(200)
                    handler.send_header('Content-Type', 'text/event-stream')
                    handler.end_headers()
                    for event in events:
                        if event['type'] == 'message_start':
                            event['message'].update(id='local-message', type='message', role='assistant', content=[],
                                model=payload['model'], stop_reason=None, stop_sequence=None)
                        handler.wfile.write(('event: '+event['type']+'\ndata: '+json.dumps(event, ensure_ascii=False)+'\n\n').encode())
                    handler.wfile.flush()
                except Exception as exc:
                    errors.append(type(exc).__name__+': '+str(exc))
                    try:
                        handler.send_error(500, 'Synthetic response contract failed')
                    except (BrokenPipeError, ConnectionResetError):
                        pass

        original_connect = socket.socket.connect
        def local_connect(connection, address):
            if connection.family in {socket.AF_INET, socket.AF_INET6} and not ipaddress.ip_address(address[0]).is_loopback:
                raise AssertionError('Narrative knowledge tests only permit loopback model sockets')
            return original_connect(connection, address)

        self.enterContext(patch.object(socket.socket, 'connect', local_connect))
        server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), worker.join(timeout=5)))
        config = replace(make_config(root), runtime_engine=engine, provider='stepfun', protocol='anthropic',
            api_key='synthetic-local-only', default_model='narrative-knowledge-'+engine,
            environment='narrative-knowledge-fixture-no-profile', timeout_seconds=60, enable_reference=False,
            base_url=f'http://127.0.0.1:{server.server_port}',
            runtime_manifest=Path(os.environ['LG_TEST_RUNTIME_MANIFEST']) if engine == 'codex' else None)
        events = []
        backend = Backend(config, events.append)
        self.addAsyncCleanup(backend.close)
        key = (await backend.dispatch('session/create', {}))['sessionId']
        runtime = backend.session(key)['agent'].runtime
        runtime.update(mode='full_access', creative_mode='ask')

        def protected():
            documents = [document for document in store.list_documents(limit=None) if document.kind != 'narrative_knowledge']
            return {'documents': [asdict(document) for document in documents],
                'versions': {document.id: [asdict(version) for version in store.list_versions(document.id)] for document in documents},
                'facts': [asdict(fact) for fact in store.list_facts(limit=None)]}

        baseline = protected()

        async def start(name, definition, prompt='执行宿主本轮精确操作；保存提案不授权确认，读取不授权写入。'):
            phase.update(name=name, step=0)
            return await backend.run(key, prompt, task=definition)

        async def finish(started):
            await asyncio.wait_for(backend.tasks[started['runId']][1], 60)
            complete = next(event['params'] for event in reversed(events)
                if event['method'] == 'run/completed' and event['params']['runId'] == started['runId'])
            self.assertEqual(complete['status'], 'completed', complete.get('error'))
            self.assertEqual(errors, [])
            self.assertEqual(protected(), baseline)

        async def approve(started, decision, tool):
            for _ in range(600):
                if backend.approvals.pending or backend.tasks[started['runId']][1].done():
                    break
                await asyncio.sleep(.02)
            self.assertTrue(backend.approvals.pending, 'Operation full_access does not authorize creative confirmation')
            identity = next(iter(backend.approvals.pending))
            shown = next(event['params'] for event in reversed(events)
                if event['method'] == 'approval/requested' and event['params']['approvalId'] == identity)
            self.assertEqual(shown['kind'], 'creative')
            self.assertEqual(shown['details']['tool'], tool)
            self.assertEqual(shown['details']['preview']['knowledge'], record)
            if tool == 'studio_knowledge_confirm':
                self.assertEqual(shown['details']['preview']['proposal_version_id'], record['proposal_version_id'])
                self.assertEqual(shown['details']['preview']['proposed_confirmation']['content'], main_arguments['statement']['content'])
            self.assertEqual(len([event for event in events if event['method'] == 'approval/requested'
                and event['params'].get('runId') == started['runId']]), 1)
            await backend.dispatch('approval/resolve', {'approvalId': identity, 'result': {'decision': decision}})
            await finish(started)
            return identity

        proposed = await start('propose', {'kind': 'setting'})
        await finish(proposed)
        record = store.knowledge_snapshot('lin-belief')
        self.assertEqual(record, observed['proposal'])
        self.assertIsNone(record['confirmed'])
        self.assertIn('outside the author task scope', json.dumps(observed['propose-last']))
        self.assertFalse(any(event['method'] == 'approval/requested' and event['params'].get('runId') == proposed['runId'] for event in events))
        async def action_context(action, expected_task):
            detail = await backend.dispatch('library/read', {'topic': 'pending', 'key': 'knowledge:'+str(record['document_id'])})
            self.assertIn(action, detail['actions'])
            self.assertIn(main_arguments['statement']['content'], detail['body'])
            self.assertIn('认知可以错误或不确定', detail['body'])
            self.assertIn('记录投影不等于 Agent 输入隔离', detail['body'])
            prepared = await backend.dispatch('library/actionContext', {'topic': 'pending',
                'key': detail['key'], 'action': action, 'fingerprint': detail['fingerprint']})
            self.assertEqual(prepared['task'], expected_task)
            self.assertLessEqual(len(prepared['prompt'].encode('utf-16-le'))//2, 30000)
            binding = json.loads(prepared['prompt'][prepared['prompt'].index('{'):])
            arguments = {'slug': record['slug'], 'expected_version_id': record['version_id']}
            confirm = action == 'confirm-knowledge'
            if confirm:
                arguments['proposal_version_number'] = record['proposal_version_number']
                authoritative = store.knowledge_confirmation_preview(**arguments)
            else:
                authoritative = store.knowledge_withdrawal_preview(**arguments)
            self.assertEqual(binding['arguments'], arguments)
            target = authoritative['proposed_confirmation'] if confirm else record['confirmed']
            self.assertEqual(binding['preview'], {'action': 'confirm' if confirm else 'withdraw',
                'topic': record['topic'], 'current_confirmed_proposal_version_id':
                    record['confirmed']['proposal_version_id'] if record['confirmed'] else None,
                'target': {**{field: target[field] for field in ('proposal_version_id', 'proposal_version_number',
                    'stance', 'until_position', 'sources')}, 'statement_sha256': canonical_sha(target),
                    'full_content_in_creative_approval': True}})
            return prepared

        definition = {'kind': 'knowledge', 'knowledgeAction': 'confirm', 'document': record['slug'],
            'sourceVersion': record['version_id'], 'versionNumber': record['proposal_version_number']}
        prepared = await action_context('confirm-knowledge', definition)
        declined = await approve(await start('decline', prepared['task'], prepared['prompt']), 'decline', 'studio_knowledge_confirm')
        self.assertEqual(store.knowledge_snapshot('lin-belief'), record)
        with self.assertRaisesRegex(ValueError, 'expired|resolved'):
            await backend.dispatch('approval/resolve', {'approvalId': declined, 'result': {'decision': 'accept'}})
        prepared = await action_context('confirm-knowledge', definition)
        await approve(await start('confirm', prepared['task'], prepared['prompt']), 'accept', 'studio_knowledge_confirm')
        record = store.knowledge_snapshot('lin-belief')
        self.assertEqual(record['status'], 'confirmed')
        self.assertEqual(record['confirmed']['content'], main_arguments['statement']['content'])
        self.assertEqual(record['confirmed']['proposal_version_id'], observed['proposal']['version_id'])

        before_reads = store.list_knowledge_snapshots(limit=None)
        read_run = await start('query', {'kind': 'discussion'})
        await finish(read_run)
        expected = store.knowledge_view(audience='character', position=10, holder_document='lin', limit=100)
        self.assertEqual(observed['view'], expected)
        self.assertEqual(observed['knowledge'], record)
        self.assertEqual([statement['content'] for statement in expected['statements']], [main_arguments['statement']['content']])
        self.assertEqual(expected['statements'][0]['stance'], 'believes')
        self.assertEqual(expected['view'], {'audience': 'character', 'axis': 'story', 'position': 10,
            'holder_document_id': lin.id, 'scope': 'registered_records_only',
            'registered_filter_applied': True, 'model_input_isolation': False})
        self.assertEqual(expected['coverage'], 'partial')
        self.assertTrue(expected['unknown_reasons'])
        self.assertEqual(len(expected['withheld']), 1)
        self.assertEqual(set(expected['withheld'][0]), {'record_id', 'source_status', 'reason'})
        for marker in ('CANONICAL_SECRET', 'RIVAL_PRIVATE', 'READER_REVEAL', 'LIN_FUTURE', 'STALE_SECRET'):
            self.assertNotIn(marker, json.dumps(observed['view'], ensure_ascii=False))
        self.assertIn('outside the author task scope', json.dumps(observed['query-last']))
        self.assertFalse(any(event['method'] == 'approval/requested' and event['params'].get('runId') == read_run['runId'] for event in events))
        self.assertEqual(runtime.read()['task']['grants'], 0)
        self.assertEqual(store.list_knowledge_snapshots(limit=None), before_reads)
        arguments = {'audience': 'character', 'position': 10, 'holder_document': 'lin', 'limit': 100}
        self.assertEqual(await backend.dispatch('studio/call', {'operation': 'knowledge-view', 'arguments': arguments}), expected)
        self.assertEqual(await backend.dispatch('studio/call', {'operation': 'knowledge', 'arguments': {'slug': 'lin-belief'}}), record)
        cli_input = root/'knowledge-cli-input.json'
        cli_input.write_text(json.dumps(arguments))
        def cli():
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch('lg_cli.main.load_config', return_value=config), redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(['-C', str(root), 'studio', 'knowledge-view', '--input', str(cli_input), '--json'])
            self.assertEqual(code, 0, stderr.getvalue()+stdout.getvalue())
            return json.loads(stdout.getvalue())
        self.assertEqual(await asyncio.to_thread(cli), expected)
        self.assertEqual(store.list_knowledge_snapshots(limit=None), before_reads)

        prepared = await action_context('withdraw-knowledge', {'kind': 'knowledge', 'knowledgeAction': 'withdraw',
            'document': record['slug'], 'sourceVersion': record['version_id']})
        await approve(await start('withdraw', prepared['task'], prepared['prompt']), 'accept', 'studio_knowledge_withdraw')
        record = store.knowledge_snapshot('lin-belief')
        self.assertEqual(record['status'], 'withdrawn')
        self.assertIsNone(record['confirmed'])
        await finish(await start('after-withdraw', {'kind': 'discussion'}))
        self.assertEqual(observed['after'], store.knowledge_view(**arguments))
        self.assertEqual(observed['after']['statements'], [])
        self.assertFalse(observed['after']['view']['model_input_isolation'])
        current = runtime.read()
        self.assertEqual((current['engine'], current['mode'], current['creative_mode']), (engine, 'full_access', 'ask'))
        self.assertTrue(current['thread_id'])
        checkpoint = root/'.literarygiant'/'runtime'/'native'/(key+'.json')
        core_history = any((root/'.literarygiant'/'codex-home'/'sessions').rglob('*.jsonl'))
        if engine == 'native':
            native = json.loads(checkpoint.read_text())
            self.assertEqual(native['schema'], 'lg.native-runtime.v1')
            self.assertEqual(native['thread'], current['thread_id'])
            self.assertFalse(core_history)
        else:
            self.assertTrue(core_history)
            self.assertFalse(checkpoint.exists())
        self.assertEqual(len(calls), 9)

    async def test_native_registered_knowledge_and_independent_author_confirmation(self):
        await self.journey('native')

    @unittest.skipUnless(os.environ.get('LG_TEST_RUNTIME_MANIFEST'), 'set LG_TEST_RUNTIME_MANIFEST for pinned Core')
    async def test_codex_registered_knowledge_and_independent_author_confirmation(self):
        await self.journey('codex')
