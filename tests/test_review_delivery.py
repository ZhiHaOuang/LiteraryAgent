"""Delivery journeys: real Backend, Native stages, SQLite and localhost HTTP.

No model/stage/backend mock. The server is a deterministic synthetic provider;
these tests demonstrate protocol/state boundaries, not literary quality.
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from lg_cli.backend import Backend
from lg_cli.book_analysis import BookAnalysisStore
from lg_cli.project_store import ProjectStore
from lg_cli.supervision import DEFAULT_CATEGORIES, configure_supervision
from tests.helpers import make_config
from tests.test_execution_adapter import specimen


class ReviewDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ProjectStore(self.root)
        # Domain-basic acceptance intentionally has no supervision policy file.
        self.store.initialize(name='Synthetic delivery book')
        self.original = self.store.create_document(kind='chapter', slug='one', title='One',
            content='Prefix. SELECTED. Suffix.', state='accepted', sequence=1)
        self.requests, self.events = [], []
        self.quiet = False
        self.narrative_output = None
        self.request_arrived = threading.Event()
        self.release_response = threading.Event()
        owner = self

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                if self.path != '/responses':
                    self.send_response(404); self.end_headers(); return
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append(request)
                owner.request_arrived.set()
                if owner.quiet:
                    owner.release_response.wait(timeout=5)
                schema = request.get('text', {}).get('format', {}).get('schema')
                properties = (schema or {}).get('properties', {})
                if 'options' in properties:
                    value = {'options': [{'id': str(i), 'title': 'Choice ' + str(i),
                        'content': 'Distinct provisional direction ' + str(i)} for i in range(1, 4)]}
                elif 'chapter' in properties:
                    value = {'chapter': 'A new chapter following the exact chosen direction.',
                        'summary': 'Provisional chapter ready.', 'risks': []}
                elif 'revision_markdown' in properties:
                    value = specimen(schema)
                    value.update(revision_markdown='REVISED.', memory_proposals=[], change_log=['Polished selection.'],
                        risks=[], book_overview='维修师在浮城寻找失灵闸门背后的真相。')
                elif 'findings' in properties or 'entries' in properties:
                    value = {'summary': 'Checked exact candidate source.', 'entries': [], 'findings': []}
                elif 'evidence' in properties:
                    statement=json.loads(request['input'][0]['content'])['visible_statements'][0]
                    value=owner.narrative_output or {'inference':'Provisional inference from '+statement['content'],
                        'evidence':[{'record_id':statement['record_id'],'quote':statement['content']}],
                        'uncertainty':'Only registered observations are available.'}
                else:
                    value = None
                text = json.dumps(value) if value is not None else 'Provisional inference from NEEDED_EARLY_CLUE.'
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                events = [{'type': 'response.output_text.delta', 'delta': text},
                    {'type': 'response.completed', 'response': {'status': 'completed',
                        'output': [{'type': 'message', 'role': 'assistant',
                            'content': [{'type': 'output_text', 'text': text}]}],
                        'usage': {'input_tokens': 20, 'output_tokens': 15}}}]
                try:
                    for event in events:
                        self.wfile.write(('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n').encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError): pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        self.provider_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.provider_thread.start()
        self.config = replace(make_config(self.root), runtime_engine='native', protocol='responses',
            provider='openai', default_model='synthetic-delivery', writer_model='', critic_model='',
            base_url=f'http://127.0.0.1:{self.server.server_port}', timeout_seconds=8,
            max_stage_context_chars=200000, project_instructions='INSTRUCTIONS_SECRET')
        self.backend = Backend(self.config, self.events.append)
        self.session = (await self.backend.dispatch('session/create', {}))['sessionId']

    async def asyncTearDown(self):
        self.release_response.set()
        await self.backend.close()
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        self.provider_thread.join(timeout=2)
        self.temporary.cleanup()

    async def approval(self, started, kind):
        async def wait():
            while True:
                for record in self.events:
                    params = record['params']
                    if (record['method'] == 'approval/requested' and params.get('runId') == started['runId']
                            and params['approvalId'] in self.backend.approvals.pending):
                        self.assertEqual(params['kind'], kind)
                        return params
                if self.backend.tasks[started['runId']][1].done():
                    self.fail('Task ended without expected approval: ' + repr(self.events[-3:]))
                await asyncio.sleep(.01)
        return await asyncio.wait_for(wait(), 5)

    async def answer(self, approval, decision='accept'):
        await self.backend.dispatch('approval/resolve', {'approvalId': approval['approvalId'],
            'result': {'decision': decision}})

    async def finish(self, started, status='completed'):
        await asyncio.wait_for(self.backend.tasks[started['runId']][1], 12)
        result = next(record['params'] for record in reversed(self.events)
            if record['method'] == 'run/completed' and record['params']['runId'] == started['runId'])
        self.assertEqual(result['status'], status, result)
        return result

    async def approved(self, method, params, *, kind='operation', decision='accept', status='completed'):
        started = await self.backend.dispatch(method, {'sessionId': self.session, **params})
        approval = await self.approval(started, kind)
        await self.answer(approval, decision)
        return await self.finish(started, status), approval

    async def start_polish(self):
        options = {'strategy': 'polish', 'document': 'one', 'passage': 'SELECTED.',
            'request': 'Polish only this selected passage.'}
        preview = await self.backend.dispatch('task/preview', {'options': options})
        finished, _ = await self.approved('task/start', {'options': options,
            'expectedFingerprint': preview['fingerprint']})
        self.assertEqual(finished['result']['status'], 'awaiting_review', finished)
        return finished['result']

    async def test_disagreement_choice_restart_candidate_and_separate_adoption(self):
        await self.disagreement_adoption_journey(review=False)

    async def test_disagreement_restart_and_real_native_supervision_before_adoption(self):
        await self.disagreement_adoption_journey(review=True)

    async def disagreement_adoption_journey(self, *, review):
        if review:
            configure_supervision(self.store)
        options = {'strategy': 'chapter', 'document': 'one', 'plot_disagreement': True,
            'request': 'Present three different rescue plans before drafting.'}
        preview = await self.backend.dispatch('task/preview', {'options': options})
        started = await self.backend.dispatch('task/start', {'sessionId': self.session,
            'options': options, 'expectedFingerprint': preview['fingerprint']})
        approval = await self.approval(started, 'operation')
        self.assertEqual(self.requests, [])
        await self.answer(approval)
        task = (await self.finish(started))['result']
        self.assertEqual(task['status'], 'waiting_author', task)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.store.list_versions('one')), 1)
        params = {'taskId': task['runId'], 'optionId': '2', 'expectedFingerprint': task['fingerprint']}
        await self.approved('task/select', params, kind='creative', decision='decline', status='cancelled')
        self.assertIsNone(self.store.decision_snapshot(task['decision']['slug'])['selection'])
        _, choice_approval = await self.approved('task/select', params, kind='creative')
        selected = self.store.decision_snapshot(task['decision']['slug'])['selection']
        self.assertEqual(selected['option_id'], '2')
        self.assertEqual(len(self.requests), 1)
        await self.backend.close()
        self.backend = Backend(self.config, self.events.append)
        await self.backend.dispatch('session/open', {'sessionId': self.session})
        restored = await self.backend.dispatch('task/read', {'taskId': task['runId']})
        self.assertEqual(restored['status'], 'waiting_author')
        finished, advance_approval = await self.approved('task/advance', {'taskId': task['runId'],
            'expectedFingerprint': restored['fingerprint']})
        candidate = finished['result']['candidate']
        self.assertEqual(finished['result']['status'], 'awaiting_review', finished)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.store.get_document('one').active_version_id, self.original.active_version_id)
        if review:
            # Only inject the synthetic configuration in the real child worker.
            # ReviewService, NativeStageExecutor, HTTP and SQLite remain real.
            control = self.backend.session(self.session)['control']
            original_command = control.session.run_command
            source = f'''
import sys
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
import lg_cli.acceptance_review as review
sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
from tests.helpers import make_config
root = Path(sys.argv[sys.argv.index('-C') + 1])
config = replace(make_config(root), runtime_engine='native', protocol='responses',
    provider='openai', default_model='synthetic-delivery', writer_model='', critic_model='',
    base_url={self.config.base_url!r}, timeout_seconds=8,
    max_stage_context_chars=200000, project_instructions='INSTRUCTIONS_SECRET')
with patch('lg_cli.acceptance_review.load_config', return_value=config):
    raise SystemExit(review.main(sys.argv[1:]))
'''
            async def local_review(argv, **options):
                self.assertEqual(self.store.get_document('one').active_version_id, self.original.active_version_id)
                result = await original_command([sys.executable, '-c', source, *argv[4:]], **options)
                self.assertEqual(self.store.get_document('one').active_version_id, self.original.active_version_id)
                return result
            control.session.run_command = local_review
        started = await self.backend.dispatch('creative/apply', {'sessionId': self.session,
            'action': 'acceptVersion', 'document': 'one', 'version': candidate['versionNumber'], 'review': review})
        approval = await self.approval(started, 'creative')
        self.assertNotIn(approval['approvalId'], [choice_approval['approvalId'], advance_approval['approvalId']])
        self.assertEqual(len(self.requests), 2, 'Review and activation must await separate creative approval')
        self.assertEqual(self.store.get_document('one').active_version_id, self.original.active_version_id)
        await self.answer(approval)
        adopted = await self.finish(started)
        self.assertEqual(self.store.get_document('one').active_version_id, candidate['versionId'])
        receipt = await self.backend.dispatch('operation/read', {'operationId': approval['details']['operationId']})
        self.assertEqual(receipt['status'], 'committed', adopted)
        self.assertFalse(receipt['replayAllowed'])
        self.assertEqual(len(self.requests), 2 + (len(DEFAULT_CATEGORIES) if review else 0))
        if review:
            self.assertEqual(len(DEFAULT_CATEGORIES), 5)
            reports = BookAnalysisStore(self.store).list()
            self.assertEqual(len(reports), 5)
            self.assertTrue(all(report['status'] == 'current' for report in reports), reports)
            self.assertTrue(all(not request.get('tools') for request in self.requests[2:]))

    async def test_polish_one_http_preserves_prefix_suffix_and_fresh_approval_cannot_rebase(self):
        task = await self.start_polish()
        candidate = self.store.get_version('one', task['candidate']['versionNumber'])
        self.assertEqual(candidate.content, 'Prefix. REVISED. Suffix.')
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.store.get_document('one').content, self.original.content)
        changed = self.store.edit_active_document('one', content='Author changed the base.',
            expected_version_id=self.original.active_version_id, reason='author')
        finished, approval = await self.approved('creative/apply', {'action': 'acceptVersion',
            'document': 'one', 'version': candidate.version_number, 'review': False}, kind='creative', status='failed')
        self.assertIn('older active version', finished['error'])
        self.assertEqual(self.store.get_document('one').content, 'Author changed the base.')
        receipt = await self.backend.dispatch('operation/read', {'operationId': approval['details']['operationId']})
        self.assertEqual(receipt['status'], 'not_committed', receipt)
        self.assertEqual(len(self.requests), 1)

    def registered_knowledge(self):
        holder = self.store.create_document(kind='character', slug='holder', title='PROFILE_SECRET',
            content='PROFILE_SECRET', state='accepted')
        for slug, text, start in [('known', 'NEEDED_EARLY_CLUE', 0), ('future', 'FUTURE_SECRET', 20)]:
            record = self.store.propose_knowledge(slug=slug, title='TITLE_SECRET',
                topic={'audience': 'character', 'holder_document_id': holder.id, 'key': slug, 'from_position': start},
                statement={'content': text, 'stance': 'believes', 'until_position': None, 'sources': []},
                rationale='RATIONALE_SECRET', expected_version_id=0)
            self.store.confirm_knowledge(slug, proposal_version_number=record['proposal_version_number'],
                expected_version_id=record['version_id'])
        self.backend.store.append(self.session, 'user', 'HISTORY_SECRET future twist already discussed.')
        return {'audience': 'character', 'holder_document': holder.id, 'position': 5,
            'operation': 'character_next_action'}

    async def test_backend_narrative_wire_excludes_old_history_and_includes_needed_record(self):
        options = self.registered_knowledge()
        preview = await self.backend.dispatch('narrative/preview', {'options': options})
        finished, _ = await self.approved('narrative/run', {'options': options,
            'expectedFingerprint': preview['fingerprint']})
        self.assertEqual(finished['result']['status'], 'completed', finished)
        self.assertEqual(finished['result']['evidenceValidation']['status'],'validated')
        self.assertEqual(finished['result']['analysis']['evidence'][0]['quote'],'NEEDED_EARLY_CLUE')
        self.assertEqual(len(self.requests), 1)
        wire = json.dumps(self.requests[0])
        self.assertIn('NEEDED_EARLY_CLUE', wire)
        for secret in ('HISTORY_SECRET', 'FUTURE_SECRET', 'PROFILE_SECRET', 'TITLE_SECRET',
                       'RATIONALE_SECRET', 'INSTRUCTIONS_SECRET'):
            self.assertNotIn(secret, wire)
        self.assertEqual(self.requests[0]['tools'], [])
        self.assertEqual(len(self.requests[0]['input']), 1)

    async def test_backend_invalid_narrative_evidence_never_publishes_model_text(self):
        options=self.registered_knowledge()
        self.narrative_output={'inference':'UNVERIFIED_OUTPUT_SECRET',
            'evidence':[{'record_id':999999,'quote':'NEEDED_EARLY_CLUE'}],
            'uncertainty':'UNVERIFIED_OUTPUT_SECRET'}
        finished,_=await self.approved('narrative/run',{'options':options})
        result=finished['result']
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['reasons'],['invalid_evidence'])
        self.assertFalse(result['resultReleased'])
        self.assertNotIn('text',result)
        self.assertNotIn('analysis',result)
        self.assertNotIn('UNVERIFIED_OUTPUT_SECRET',json.dumps(self.events))
        self.assertEqual(len(self.requests),1)

    async def test_cancel_quiet_worker_retains_ownership_until_http_settles(self):
        options = self.registered_knowledge()
        self.quiet = True
        started = await self.backend.dispatch('narrative/run', {'sessionId': self.session, 'options': options})
        await self.answer(await self.approval(started, 'operation'))
        self.assertTrue(await asyncio.to_thread(self.request_arrived.wait, 3))
        cancelling = asyncio.create_task(self.backend.dispatch('run/cancel', {'sessionId': self.session}))
        await asyncio.sleep(.1)
        self.assertFalse(cancelling.done(), 'Cancellation must not claim a quiet worker already stopped')
        self.release_response.set()
        await asyncio.wait_for(cancelling, 10)
        await self.finish(started, status='cancelled')
        bounded = [event for event in self.events if event['method'] == 'client/event'
            and event['params'].get('item', {}).get('id') == 'bounded-task:' + started['runId']]
        self.assertEqual(bounded, [], 'Cancelled inference must not publish its result')
        self.assertEqual(self.store.get_document('one').active_version_id, self.original.active_version_id)

    async def test_actual_cli_identity_preview_and_confirmed_start_use_same_backend(self):
        options = self.root / 'task-options.json'
        options.write_text(json.dumps({'strategy': 'polish', 'document': 'one', 'passage': 'SELECTED.',
            'request': 'Polish only this selected passage.'}))
        source = '''
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import lg_cli.main as cli
from tests.helpers import make_config
root = Path(sys.argv[1])
config = replace(make_config(root), runtime_engine='native', provider='openai', protocol='responses',
    default_model='synthetic-cli-delivery', writer_model='', critic_model='', base_url=sys.argv[2],
    timeout_seconds=8, max_stage_context_chars=200000)
cli._load_runtime()
with patch('lg_cli.main.load_config', return_value=config):
    raise SystemExit(cli.main(['-C', str(root), '--runtime-engine', 'native', *sys.argv[3:]]))
'''
        repo = Path(__file__).resolve().parents[1]
        async def invoke(*args, expected=0):
            result = await asyncio.to_thread(subprocess.run, [sys.executable, '-c', source,
                str(self.root), self.config.base_url, *args], cwd=self.root,
                env={**os.environ, 'PYTHONPATH': str(repo / 'lg-cli') + ':' + str(repo),
                    'LITERARYGIANT_REGISTRY_HOME': str(self.root / 'registry')},
                capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, expected, result.stderr + '\n' + result.stdout)
            decoder = json.JSONDecoder()
            text, values = result.stdout.strip(), []
            while text:
                value, end = decoder.raw_decode(text)
                values.append(value)
                text = text[end:].lstrip()
            return values
        identity = (await invoke('identity', '--json'))[-1]
        self.assertEqual(identity['runtime']['engine'], 'native')
        self.assertEqual(identity['package']['path'], str(repo / 'lg-cli/lg_cli'))
        preview = (await invoke('task', 'preview', '--input', str(options), '--json'))[-1]
        self.assertEqual(preview['status'], 'ready')
        waiting = (await invoke('task', 'start', '--input', str(options), '--json', expected=2))[-1]
        self.assertEqual(waiting['status'], 'confirmation_required')
        self.assertEqual(self.requests, [])
        result = (await invoke('task', 'start', '--input', str(options), '--confirm', '--json'))[-1]
        self.assertEqual(result['status'], 'awaiting_review', result)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.store.get_version('one', result['candidate']['versionNumber']).content,
            'Prefix. REVISED. Suffix.')
        self.assertEqual(self.store.get_document('one').active_version_id, self.original.active_version_id)
