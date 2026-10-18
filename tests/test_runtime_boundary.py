"""Prepared context and public presentation survive execution and session recovery."""
import copy
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from lg_cli.agent_runtime import runtime_factory
from lg_cli.anthropic_bridge import AnthropicBridge
from lg_cli.backend import Backend
from lg_cli.book_overview import save_overview
from lg_cli.conversation_store import ConversationStore
from lg_cli.codex_policy import WritingLaunchPolicy
from lg_cli.init_project import init_workspace
from lg_cli.live_agent import CodexSession
from lg_cli.native_events import NativeEvents
from lg_cli.project_store import ProjectStore
from tests.helpers import make_config
from tests.test_anthropic_bridge import message_events
from tests.test_native_runtime import answer


class PreparedContext:
    def __init__(self):
        self.revision = 1
        self.calls = []
        self.histories = []

    def instructions(self, *, control_socket=None, review_delegation=False):
        self.calls.append((control_socket, review_delegation))
        return f"HOST_PREPARED_{self.revision}"

    def history(self, text):
        self.histories.append(text)
        return "HOST_HISTORY: " + text


class RuntimeBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        init_workspace(self.root)
        self.config = replace(make_config(self.root), runtime_engine='native', protocol='responses',
                              default_model='synthetic-model', timeout_seconds=15)

    async def test_native_consumes_injected_context_once_per_turn_and_keeps_checkpoint_history(self):
        key = ConversationStore(self.root).create()
        context = PreparedContext()
        requests = []

        async def model(_model, request, delta):
            requests.append(copy.deepcopy(request))
            if len(requests) == 1:
                context.revision = 2
                return {'status': 'completed', 'output': [{'type': 'function_call', 'call_id': 'memory',
                    'name': 'project_memory', 'arguments': '{}'}]}
            delta('Public answer')
            return answer('Public answer')

        with patch('lg_cli.native_model.NativeModel.generate', model), \
                patch('lg_cli.runtime_context.writing_instructions', side_effect=AssertionError('Transport read literary policy')):
            runtime = runtime_factory(self.config, lambda *_: None, conversation=key, context_provider=context)
            self.assertEqual(context.calls, [])
            try:
                await runtime.start(context='IMPORTED_DISCUSSION')
                await runtime.run('First task')
                await runtime.run('Second task')
                identity = runtime.thread_id
            finally:
                await runtime.close()
            self.assertEqual([r['instructions'] for r in requests],
                             ['HOST_PREPARED_1', 'HOST_PREPARED_1', 'HOST_PREPARED_2'])
            self.assertEqual(len(context.calls), 2)
            self.assertEqual(context.histories, ['IMPORTED_DISCUSSION'])
            restored = runtime_factory(self.config, lambda *_: None, conversation=key, context_provider=context)
            try:
                await restored.start(context='MUST_NOT_REINJECT')
                await restored.run('Continue')
                self.assertEqual(restored.thread_id, identity)
            finally:
                await restored.close()
        history = json.dumps(requests[-1]['input'])
        self.assertEqual(history.count('IMPORTED_DISCUSSION'), 1)
        self.assertNotIn('MUST_NOT_REINJECT', history)
        self.assertEqual(context.histories, ['IMPORTED_DISCUSSION'])

    async def test_product_context_reads_latest_book_after_task_and_controls_are_bound(self):
        store = ProjectStore(self.root)
        chapter = store.create_document(kind='chapter', slug='chapter', title='Chapter',
                                        content='Accepted', state='accepted')
        candidate = store.create_version(chapter.id, content='Candidate')
        save_overview(self.root, '旧概况：旅人寻找故乡。', source='test')
        backend = Backend(self.config)
        with patch('lg_cli.runtime_context.writing_instructions', side_effect=AssertionError('Too early')):
            key = (await backend.dispatch('session/create', {}))['sessionId']
        save_overview(self.root, '新概况：维修师调查星港停摆。', source='test')
        requests = []

        async def model(_model, request, delta):
            requests.append(request)
            return answer()

        try:
            with patch('lg_cli.native_model.NativeModel.generate', model):
                run = await backend.run(key, 'Review candidate', task={'kind': 'review', 'document': chapter.slug,
                    'sourceVersion': chapter.active_version_id, 'versionNumber': candidate.version_number})
                await backend.tasks[run['runId']][1]
            self.assertEqual(backend.journal.read(key)[-1]['params']['status'], 'completed')
        finally:
            await backend.close()
        self.assertIn('新概况', requests[0]['instructions'])
        self.assertNotIn('旧概况', requests[0]['instructions'])
        self.assertIn('workflow_status', requests[0]['instructions'])
        self.assertIn('Use delegate_review', requests[0]['instructions'])
        self.assertIn('delegate_review', [tool['name'] for tool in requests[0]['tools']])

    async def test_review_context_never_loads_main_book_instructions_or_write_tools(self):
        requests = []

        async def model(_model, request, delta):
            requests.append(request)
            return answer()

        with patch('lg_cli.runtime_context.writing_instructions', side_effect=AssertionError('Reviewer loaded main context')), \
                patch('lg_cli.native_model.NativeModel.generate', model):
            reviewer = runtime_factory(self.config, lambda *_: None, review_only=True)
            try:
                await reviewer.start(context='MAIN_DISCUSSION_MUST_NOT_LEAK')
                await reviewer.run('Review only this evidence')
            finally:
                await reviewer.close()
        self.assertIn('untrusted data', requests[0]['instructions'])
        self.assertNotIn('MAIN_DISCUSSION', json.dumps(requests))
        self.assertEqual({t['name'] for t in requests[0]['tools']},
                         {'project_memory', 'read_document', 'reference_search', 'document_history',
                          'document_diff', 'book_dashboard', 'studio_status', 'studio_review_context',
                          'book_library', 'book_entry', 'book_action_context', 'studio_decision', 'studio_source_impact',
                          'studio_knowledge', 'studio_knowledge_view'})


@unittest.skipUnless(os.environ.get('LG_TEST_RUNTIME_MANIFEST'), 'set LG_TEST_RUNTIME_MANIFEST for pinned Core')
class CodexContextBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_core_receives_prepared_context_and_resume_avoids_history_reinjection(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            init_workspace(root)
            config = replace(make_config(root), provider='stepfun', protocol='anthropic',
                default_model='step-3.5-flash', timeout_seconds=60,
                runtime_manifest=Path(os.environ['LG_TEST_RUNTIME_MANIFEST']))
            key = ConversationStore(root).create()
            context = PreparedContext()
            payloads, starts = [], []
            original = CodexSession.request

            async def record(session, method, params):
                if method in {'thread/start', 'thread/resume'}:
                    starts.append((method, copy.deepcopy(params)))
                return await original(session, method, params)

            def response(payload):
                payloads.append(payload)
                yield from message_events(delta={'type': 'text_delta', 'text': 'Prepared context received.'})

            with patch('lg_cli.live_agent.provider_bridge', side_effect=lambda _: AnthropicBridge(config, stream_factory=response)), \
                    patch.object(CodexSession, 'request', record), \
                    patch('lg_cli.runtime_context.writing_instructions', side_effect=AssertionError('Transport read literary policy')):
                for index in range(2):
                    context.revision = index + 1
                    session = CodexSession(config, lambda *_: None, conversation=key, context_provider=context, launch_policy=WritingLaunchPolicy())
                    try:
                        await session.start(context='INITIAL_DISCUSSION' if index == 0 else 'MUST_NOT_REINJECT')
                        await session.start(context='MUST_NOT_RESTART')
                        await session.run('Return a short reply without tools.')
                    finally:
                        await session.close()
            self.assertEqual([method for method, _ in starts], ['thread/start', 'thread/resume'])
            self.assertIn('HOST_PREPARED_1', starts[0][1]['baseInstructions'])
            self.assertIn('HOST_HISTORY: INITIAL_DISCUSSION', starts[0][1]['baseInstructions'])
            self.assertEqual(starts[1][1]['baseInstructions'], 'HOST_PREPARED_2')
            self.assertEqual(len(context.calls), 2)
            self.assertEqual(context.histories, ['INITIAL_DISCUSSION'])
            self.assertIn('HOST_PREPARED_1', json.dumps(payloads[0]))
            self.assertIn('HOST_PREPARED_2', json.dumps(payloads[-1]))
            self.assertNotIn('MUST_NOT_', json.dumps(payloads))
            self.assertNotIn("You are LiteraryGiant (LG)", json.dumps(payloads))


class NativePresentationBoundaryTests(unittest.TestCase):
    def test_tool_result_keeps_diff_artifact_and_json_fallback_without_mutation(self):
        records = []
        events = NativeEvents(lambda method, params: records.append((method, params)))
        value = {'artifact': 'ReferenceLibrary/drafts/candidate.md',
                 'diff': {'path': 'chapter.md', 'before': 'old', 'after': 'candidate', 'applied': False}}
        data = {'content': [{'type': 'text', 'text': json.dumps(value)}]}
        before = copy.deepcopy(data)
        events.tool_started('call', 'edit_manuscript', {'slug': 'chapter'})
        events.tool_completed('call', 'edit_manuscript', {'slug': 'chapter'}, data)
        item = records[-1][1]['item']
        self.assertEqual(item['diff'], value['diff'])
        self.assertEqual(item['files'], [value['artifact']])
        self.assertEqual(item['structured']['result'], value)
        self.assertEqual([p['item']['state'] for _, p in records], ['running', 'completed'])
        self.assertEqual(data, before)

    def test_expert_presentation_does_not_publish_private_result_or_error(self):
        records = []
        events = NativeEvents(lambda method, params: records.append((method, params)))
        events.review_task({'id': 'expert', 'expert': 'critic', 'state': 'failed',
                            'result': {'private': 'PRIVATE_RESULT'}, 'error': 'PRIVATE_ERROR'})
        self.assertEqual(records[0][1]['item']['kind'], 'notice')
        self.assertEqual(records[0][1]['item']['structured']['task']['details'], [])
        self.assertNotIn('PRIVATE_', json.dumps(records))
