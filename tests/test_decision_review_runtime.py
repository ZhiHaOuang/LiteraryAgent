"""Official creative choices bind real Runtime/MCP reviews and separate adoption."""
import asyncio
import ipaddress
import json
import os
import socket
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from lg_cli.backend import Backend
from lg_cli.book_analysis import BookAnalysisStore, CATEGORIES, fingerprint
from lg_cli.init_project import init_workspace
from lg_cli.project_store import ProjectStore
from lg_cli.supervision import configure_supervision
from tests.helpers import make_config, reference_card
from tests.test_anthropic_bridge import message_events


def objects(value):
    if isinstance(value, str):
        try:
            yield from objects(json.loads(value))
        except (ValueError, TypeError):
            pass
    elif isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)


class DecisionReviewRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def journey(self, engine):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.enterContext(patch.dict(os.environ, {'LITERARYGIANT_REGISTRY_HOME': str(root/'registry')}))
        self.enterContext(patch('lg_cli.credentials.read_profiles', return_value={'active': None, 'profiles': {}}))
        init_workspace(root)
        saved = ProjectStore(root)
        original = saved.create_document(kind='chapter', slug='one', title='第一章', sequence=1,
            content='前文保持不动。\n\n作者采纳的原稿。\n\n后文保持不动。', state='accepted')
        other = saved.create_document(kind='chapter', slug='two', title='无关章节', sequence=2,
            content='无关章节必须保持原样。', state='accepted')
        candidate = saved.create_version('one', content='前文保持不动。\n\n候选：林砚先核验通行证。\n\n后文保持不动。',
            expected_version_id=original.active_version_id)
        saved.set_fact(category='world', key='gate', value='守卫必须核验通行证。')
        canonical = saved.list_facts(state='canonical')
        configure_supervision(saved)
        decision = saved.save_decision_options(slug='gate-choice', title='城门方向',
            topic={'scope':'work', 'document_id':None, 'key':'gate'},
            options=[{'id':key, 'title':key, 'content':text} for key, text in (
                ('A', 'CHOICE_A：主角请求守卫核验通行证。'),
                ('B', 'CHOICE_B：主角等待领队携证前来。'),
                ('C', 'CHOICE_C：主角先向登记官说明来意。'))],
            sources=[], request='比较三种入城方向，由作者逐次选择。', expected_version_id=0)

        def choose(option):
            current = saved.decision_snapshot(decision['slug'])
            return saved.select_decision_option(decision['slug'], options_version_number=1,
                option_id=option, expected_version_id=current['version_id'])

        choose('A')
        records = BookAnalysisStore(saved)
        phase = {'name':'', 'step':0}
        contexts, calls, responses, errors = {}, [], {}, []
        context_ready, release_save = threading.Event(), threading.Event()
        serial = 0

        def response(payload):
            nonlocal serial
            name, step = phase['name'], phase['step']
            phase['step'] += 1
            responses.setdefault(name, []).extend(objects(payload.get('messages', [])))
            if name.startswith('review') and step == 0:
                marker = 'Read the exact source snapshot'
                arguments = {'slug':'one', 'version_number':candidate.version_number}
            elif name.startswith('review') and step == 1:
                context = next(value for value in reversed(list(objects(payload['messages'])))
                    if {'sourceFingerprint','snapshot','source_spans','schemas'} <= value.keys())
                contexts[name] = context
                if name == 'review-stale':
                    context_ready.set()
                    if not release_save.wait(30):
                        raise AssertionError('Host did not release the stale source submission')
                span = next(identity for identity, value in context['source_spans'].items()
                    if value['version_id'] == candidate.id and '候选：林砚先核验通行证。' in value['quote'])
                marker = 'Save source-validated reviews produced from studio_review_context'
                arguments = {'slug':'one', 'version_number':candidate.version_number,
                    'source_fingerprint':context['sourceFingerprint'],
                    'reports':{category:{'summary':name+'：仅审读此候选与当前作者方向。',
                        'entries':[{'key':'candidate-source', 'label':'准确候选证据',
                            'analysis':'依据本轮宿主提供的候选与正式方向审读。',
                            'instance_card':reference_card(category), 'evidence':[{'span_id':span}]}],
                        'findings':[]} for category in CATEGORIES}}
            elif name.startswith('adopt') and step == 0:
                marker = 'Explicitly adopt a reviewed candidate once'
                arguments = {'slug':'one', 'version_number':candidate.version_number,
                    'expected_version_id':original.active_version_id}
            else:
                return list(message_events(delta={'type':'text_delta', 'text':'本轮工具结果已核对；采用由作者独立决定。'}))
            tool = next(tool['name'] for tool in payload['tools'] if marker in tool.get('description', ''))
            calls.append((name, marker, arguments))
            serial += 1
            return list(message_events(block={'type':'tool_use', 'id':'decision-review-'+str(serial),
                'name':tool, 'input':{}}, delta={'type':'input_json_delta',
                'partial_json':json.dumps(arguments, ensure_ascii=False)}, stop='tool_use'))

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
                raise AssertionError('Decision review tests only permit local model sockets')
            return original_connect(connection, address)

        self.enterContext(patch.object(socket.socket, 'connect', local_connect))
        server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), worker.join(timeout=5)))
        config = replace(make_config(root), runtime_engine=engine, provider='stepfun', protocol='anthropic',
            api_key='synthetic-local-only', default_model='decision-review-'+engine,
            environment='decision-review-fixture-no-profile', timeout_seconds=60,
            base_url=f'http://127.0.0.1:{server.server_port}', enable_reference=False,
            runtime_manifest=Path(os.environ['LG_TEST_RUNTIME_MANIFEST']) if engine == 'codex' else None)
        events = []
        backend = Backend(config, events.append)
        self.addAsyncCleanup(backend.close)
        key = (await backend.dispatch('session/create', {}))['sessionId']
        backend.session(key)['agent'].runtime.update(mode='full_access', creative_mode='ask')
        review_task = {'kind':'review', 'document':'one', 'sourceVersion':original.active_version_id,
            'versionNumber':candidate.version_number}
        adopt_task = {**review_task, 'kind':'adopt'}

        async def start(name):
            phase.update(name=name, step=0)
            return await backend.run(key, '只执行本轮指定的审读或采用操作。',
                task=review_task if name.startswith('review') else adopt_task)

        async def finish(run):
            await asyncio.wait_for(backend.tasks[run['runId']][1], 60)
            complete = next(event['params'] for event in reversed(events)
                if event['method']=='run/completed' and event['params']['runId']==run['runId'])
            self.assertEqual(complete['status'], 'completed', complete.get('error'))
            self.assertEqual(errors, [])

        async def approval(run):
            for _ in range(600):
                if backend.approvals.pending or backend.tasks[run['runId']][1].done():
                    break
                await asyncio.sleep(.02)
            self.assertTrue(backend.approvals.pending, 'Real creative approval must remain separate from operation full_access')
            identity = next(iter(backend.approvals.pending))
            shown = next(event['params'] for event in reversed(events)
                if event['method']=='approval/requested' and event['params']['approvalId']==identity)
            self.assertEqual(shown['kind'], 'creative')
            self.assertEqual(shown['details']['tool'], 'studio_accept')
            self.assertEqual(shown['details']['preview']['version_id'], candidate.id)
            return identity

        async def resolve(identity, decision):
            await backend.dispatch('approval/resolve', {'approvalId':identity, 'result':{'decision':decision}})

        def report_bytes():
            return {category:records._path('one', category, candidate_id=candidate.id).read_bytes() for category in CATEGORIES}

        def unadopted():
            self.assertEqual(saved.get_document('one'), original)
            self.assertEqual(saved.get_document('two'), other)
            self.assertEqual(saved.list_facts(state='canonical'), canonical)
            self.assertEqual(len(saved.list_versions('one')), 2)

        def checked_context(name, option):
            context = contexts[name]
            self.assertEqual(context['snapshot']['documents'][0]['version_id'], candidate.id)
            self.assertEqual(context['snapshot']['documents'][0]['content'], candidate.content)
            choices = context['snapshot']['creative_decisions']
            self.assertEqual(len(choices), 1)
            self.assertEqual(choices[0]['document_id'], decision['document_id'])
            self.assertEqual(choices[0]['selection']['option_id'], option)
            self.assertEqual(context['sourceFingerprint'], fingerprint(context['snapshot']))
            return context

        # The model actually reads and saves all five reports through MCP.
        await finish(await start('review-A'))
        context_a = checked_context('review-A', 'A')
        old_reports = report_bytes()
        for raw in old_reports.values():
            report = json.loads(raw)
            self.assertEqual(report['fingerprint'], context_a['sourceFingerprint'])
            self.assertEqual(report['report']['entries'][0]['evidence'][0]['version_id'], candidate.id)
        unadopted()

        run = await start('review-stale')
        try:
            for _ in range(1000):
                if context_ready.is_set() or backend.tasks[run['runId']][1].done():
                    break
                await asyncio.sleep(.02)
            self.assertTrue(context_ready.is_set())
            choose('B')  # Explicit host/author action while the model holds the old context.
        finally:
            release_save.set()
        await finish(run)
        checked_context('review-stale', 'A')
        self.assertIn('Reviewed source changed', json.dumps(responses['review-stale'], ensure_ascii=False))
        self.assertEqual(report_bytes(), old_reports)
        unadopted()

        run = await start('adopt-old-reports')
        await resolve(await approval(run), 'accept')
        await finish(run)
        self.assertIn('candidate review', json.dumps(responses['adopt-old-reports'], ensure_ascii=False))
        self.assertEqual(report_bytes(), old_reports)
        unadopted()

        await finish(await start('review-B'))
        context_b = checked_context('review-B', 'B')
        self.assertNotEqual(context_b['sourceFingerprint'], context_a['sourceFingerprint'])
        reports_b = report_bytes()
        self.assertTrue(all(json.loads(raw)['fingerprint']==context_b['sourceFingerprint'] for raw in reports_b.values()))

        run = await start('adopt-declined')
        declined = await approval(run)
        await resolve(declined, 'decline')
        await finish(run)
        self.assertIn('not approved', json.dumps(responses['adopt-declined'], ensure_ascii=False))
        with self.assertRaisesRegex(ValueError, 'expired|resolved'):
            await resolve(declined, 'accept')
        self.assertEqual(report_bytes(), reports_b)
        unadopted()

        run = await start('adopt-race')
        pending = await approval(run)
        choose('C')
        await resolve(pending, 'accept')
        await finish(run)
        self.assertIn('Project changed during approval', json.dumps(responses['adopt-race'], ensure_ascii=False))
        self.assertEqual(report_bytes(), reports_b)
        unadopted()

        await finish(await start('review-C'))
        context_c = checked_context('review-C', 'C')
        self.assertNotEqual(context_c['sourceFingerprint'], context_b['sourceFingerprint'])
        self.assertTrue(all(json.loads(raw)['fingerprint']==context_c['sourceFingerprint'] for raw in report_bytes().values()))
        run = await start('adopt-current')
        await resolve(await approval(run), 'accept')
        await finish(run)
        self.assertEqual(saved.get_document('one').active_version_id, candidate.id)
        self.assertEqual(saved.get_document('one').content, candidate.content)
        self.assertEqual(saved.get_document('two'), other)
        self.assertEqual(saved.list_facts(state='canonical'), canonical)
        self.assertEqual(len(saved.list_versions('one')), 2)
        published = records.list('one')
        self.assertEqual({report['library'] for report in published}, set(CATEGORIES))
        self.assertTrue(all(report['status']=='current' for report in published))
        self.assertTrue(all(report['fingerprint']==fingerprint(records.snapshot('one')) for report in published))
        current = backend.session(key)['agent'].runtime.read()
        self.assertEqual(current['engine'], engine)
        self.assertEqual((current['mode'], current['creative_mode']), ('full_access', 'ask'))
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
        self.assertEqual(len(calls), 12)
        self.assertEqual({call[0] for call in calls}, {'review-A','review-stale','review-B','review-C',
            'adopt-old-reports','adopt-declined','adopt-race','adopt-current'})

    async def test_native_review_choice_source_and_author_adoption(self):
        await self.journey('native')

    @unittest.skipUnless(os.environ.get('LG_TEST_RUNTIME_MANIFEST'), 'set LG_TEST_RUNTIME_MANIFEST for pinned Core')
    async def test_codex_review_choice_source_and_author_adoption(self):
        await self.journey('codex')
