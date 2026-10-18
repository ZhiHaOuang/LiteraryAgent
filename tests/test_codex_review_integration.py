"""Pinned Core delegates to product experts through the real Backend lifecycle."""
import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from lg_cli.anthropic_bridge import AnthropicBridge, translate_request
from lg_cli.backend import Backend
from lg_cli.codex_reviews import CodexReviews
from lg_cli.conversation_store import ConversationStore
from lg_cli.init_project import init_workspace
from lg_cli.live_agent import LiveAgent
from lg_cli.project_store import ProjectStore
from lg_cli.session_runtime import SessionRuntime
from tests.helpers import make_config
from tests.test_anthropic_bridge import message_events
from tests.test_execution_adapter import specimen
from tests.test_native_runtime import answer


COLLABORATION = {'spawn_agent', 'send_message', 'followup_task', 'wait_agent', 'interrupt_agent', 'list_agents'}


@unittest.skipUnless(os.environ.get('LG_TEST_RUNTIME_MANIFEST'), 'set LG_TEST_RUNTIME_MANIFEST for pinned Core')
class CodexReviewIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        init_workspace(self.root)
        self.store = ProjectStore(self.root)
        self.original = self.store.create_document(kind='chapter', slug='one', title='原稿',
            content='作者已采纳的原稿。', state='accepted', sequence=1)
        self.candidate = self.store.create_version('one', content='候选：林砚先隔离电源，再救出工友。',
            expected_version_id=self.original.active_version_id)
        self.definition = {'kind': 'review', 'document': 'one', 'sourceVersion': self.original.active_version_id,
                          'versionNumber': self.candidate.version_number, 'allowRevision': True}
        self.config = replace(make_config(self.root), provider='stepfun', protocol='anthropic',
            default_model='step-3.5-flash', critic_model='expert-model', timeout_seconds=30,
            environment='codex-expert-fixture', runtime_manifest=Path(os.environ['LG_TEST_RUNTIME_MANIFEST']))
        self.events, self.payloads, self.names, self.experts, self.handoffs = [], [], [], [], []
        self.backend = Backend(self.config, self.events.append)
        self.key = (await self.backend.dispatch('session/create', {}))['sessionId']
        self.enterContext(patch('lg_cli.credentials.read_profiles', return_value={'active': None, 'profiles': {}}))
        self.enterContext(patch('lg_cli.live_agent.provider_bridge', side_effect=lambda _: AnthropicBridge(
            self.config, stream_factory=self.response)))

        def capture(request, max_tokens):
            payload, mapping = translate_request(request, max_tokens)
            self.names.append({spec['name'] for spec in mapping.values()})
            return payload, mapping

        self.enterContext(patch('lg_cli.anthropic_bridge.translate_request', side_effect=capture))
        original_call = CodexReviews.call

        async def record_call(reviews, params):
            value = await original_call(reviews, params)
            self.handoffs.append(value)
            return value

        self.enterContext(patch.object(CodexReviews, 'call', record_call))

    async def asyncTearDown(self):
        await self.backend.close()

    def response(self, payload):
        self.payloads.append(payload)
        if any(part.get('type') == 'tool_result' for part in payload['messages'][-1]['content']):
            yield from message_events(delta={'type': 'text_delta', 'text': '主 Agent 综合临时审读意见。'})
            return
        tool = next(tool['name'] for tool in payload['tools']
            if 'Request provisional, tool-free expert analysis' in tool.get('description', ''))
        yield from message_events(block={'type': 'tool_use', 'id': f'review-{len(self.payloads)}',
            'name': tool, 'input': {}}, delta={'type': 'input_json_delta',
            'partial_json': json.dumps({'experts': ['continuity', 'critic']})}, stop='tool_use')

    async def run_review(self):
        run = await self.backend.run(self.key, '审读指定候选；保留作者决定权。', task=self.definition)
        await asyncio.wait_for(self.backend.tasks[run['runId']][1], 40)
        return next(event['params'] for event in reversed(self.events)
            if event['method'] == 'run/completed' and event['params']['runId'] == run['runId'])

    async def test_real_core_experts_and_cold_resume_preserve_source_authority_and_root(self):
        overlap = asyncio.Event()

        async def model(_model, request, delta):
            self.experts.append(request)
            if len(self.experts) % 2 == 0:
                overlap.set()
            await asyncio.wait_for(overlap.wait(), 2)
            delta('PRIVATE_EXPERT_DELTA')
            return answer(json.dumps(specimen(request['text']['format']['schema']), ensure_ascii=False))

        roots, turns = [], []
        before = {str(path): path.read_bytes() for path in (self.root / 'ReferenceLibrary').rglob('*') if path.is_file()}
        self.backend.store.append(self.key, 'user', 'PRIVATE_PARENT_HISTORY')
        with patch('lg_cli.native_model.NativeModel.generate', model):
            for index in range(2):
                self.assertEqual((await self.run_review())['status'], 'completed')
                runtime = self.backend.session(self.key)['agent']
                roots.append(runtime.thread_id)
                saved = runtime.runtime.read()
                turns.append(saved['codex_delegations']['parentTurnId'])
                self.assertTrue(runtime.agent.dynamic_tools_registered)
                self.assertEqual(saved['codex_tool_contract']['rootThreadId'], runtime.thread_id)
                self.assertEqual(saved['task']['grants'], 0)
                self.assertEqual(saved['task']['revisionGrants'], 0)
                self.assertFalse(runtime.delegations.pending)
                self.assertEqual(len(saved['codex_delegations']['calls']), 1)
                self.assertTrue(all(entry['settled'] for entry in saved['codex_delegations']['calls'].values()))
                if index == 0:
                    await self.backend.close()
                    self.backend = Backend(self.config, self.events.append)
                    await self.backend.open(self.key)
                    self.assertEqual(len(self.experts), 2)
                    overlap.clear()
        self.assertEqual(roots[0], roots[1])
        self.assertNotEqual(turns[0], turns[1])
        self.assertEqual(len(self.experts), 4)
        self.assertEqual(len(self.handoffs), 2)
        for handoff in self.handoffs:
            self.assertTrue(handoff['success'])
            value = json.loads(handoff['contentItems'][0]['text'])
            self.assertTrue(value['provisional'])
            self.assertTrue(value['sourceCurrent'])
            self.assertEqual({child['expert'] for child in value['experts']}, {'continuity', 'critic'})
        for request in self.experts:
            self.assertEqual(request['tools'], [])
            self.assertEqual(request['model'], 'expert-model')
            self.assertNotIn('PRIVATE_PARENT_HISTORY', json.dumps(request))
            self.assertIn(self.candidate.content, json.dumps(request, ensure_ascii=False))
        for names in self.names:
            self.assertFalse(names & COLLABORATION, names)
            self.assertTrue({'delegate_review', 'edit_manuscript', 'studio_review'} <= names)
        self.assertEqual(self.store.get_document('one').active_version_id, self.original.active_version_id)
        self.assertEqual(len(self.store.list_versions('one')), 2)
        self.assertEqual(self.store.list_facts(state='canonical'), [])
        self.assertEqual(before, {str(path): path.read_bytes() for path in (self.root / 'ReferenceLibrary').rglob('*') if path.is_file()})
        self.assertNotIn('PRIVATE_EXPERT_DELTA', json.dumps(self.events))
        notices = [event['params']['item'] for event in self.events if event['method'] == 'client/event'
            and event['params'].get('type') == 'item.updated' and event['params']['item'].get('structured', {}).get('task')]
        self.assertEqual({item['title'] for item in notices if item['state'] == 'completed'}, {'continuity', 'critic'})

    async def test_cancel_drains_host_experts_and_reopen_does_not_replay(self):
        started, stopped = asyncio.Event(), []

        async def model(_model, request, delta):
            self.experts.append(request)
            if len(self.experts) == 2:
                started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(request['model'])

        with patch('lg_cli.native_model.NativeModel.generate', model):
            run = await self.backend.run(self.key, '审读候选', task=self.definition)
            try:
                await asyncio.wait_for(started.wait(), 15)
                runtime = self.backend.session(self.key)['agent']
                root = runtime.thread_id
                await asyncio.wait_for(self.backend.cancel(self.key), 10)
                self.assertEqual(len(stopped), 2)
                self.assertFalse(runtime.delegations.pending)
                self.assertIsNone(runtime.agent.process)
                saved = SessionRuntime(self.root, self.key).read()['codex_delegations']
                self.assertTrue(all(entry['settled'] and entry['interrupted'] for entry in saved['calls'].values()))
                self.assertTrue(all(child['state'] == 'cancelled' for entry in saved['calls'].values()
                                    for child in entry['record']['children']))
                new = self.backend.session(self.key)['agent']
                await new.start()
                self.assertEqual(new.thread_id, root)
                self.assertEqual(len(self.experts), 2)
                self.assertTrue(all(not handoff['success'] for handoff in self.handoffs))
            finally:
                task = self.backend.tasks[run['runId']][1]
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_legacy_root_keeps_history_and_reports_missing_expert_capability(self):
        def reply(payload):
            self.payloads.append(payload)
            yield from message_events(delta={'type': 'text_delta', 'text': '主 Agent 完成审读。'})

        with patch('lg_cli.live_agent.provider_bridge', side_effect=lambda _: AnthropicBridge(self.config, stream_factory=reply)), \
                patch('lg_cli.native_model.NativeModel.generate', side_effect=AssertionError('Legacy expert call')):
            # Create an actual old-style root with no dynamic-tool contract.
            legacy = LiveAgent(self.config, lambda *_: None, conversation=self.key)
            await self.backend.close()
            try:
                await legacy.start()
                root = legacy.thread_id
                await legacy.run('Remember LEGACY_REVIEW_HISTORY.')
            finally:
                await legacy.close()
            self.backend = Backend(self.config, self.events.append)
            await self.backend.open(self.key)
            self.assertEqual((await self.run_review())['status'], 'completed')
            runtime = self.backend.session(self.key)['agent']
            self.assertEqual(runtime.thread_id, root)
            self.assertFalse(runtime.agent.dynamic_tools_registered)
            self.assertNotIn('codex_tool_contract', runtime.runtime.read())
            self.assertNotIn('codex_delegations', runtime.runtime.read())
            self.assertNotIn('delegate_review', self.names[-1])
            self.assertFalse(self.names[-1] & COLLABORATION)
            self.assertIn('LEGACY_REVIEW_HISTORY', json.dumps(self.payloads[-1]))
            self.assertTrue(any('独立专家审读需要新建会话' in event['params'].get('item', {}).get('text', '')
                                for event in self.events if event['method'] == 'client/event'))

    async def test_registered_tool_without_review_scope_cannot_request_an_expert(self):
        with patch('lg_cli.native_model.NativeModel.generate', side_effect=AssertionError('Unauthorized expert call')):
            run = await self.backend.run(self.key, '讨论作品', task={'kind': 'discussion'})
            await asyncio.wait_for(self.backend.tasks[run['runId']][1], 30)
        self.assertEqual(len(self.handoffs), 1)
        self.assertFalse(self.handoffs[0]['success'])
        runtime = self.backend.session(self.key)['agent']
        self.assertEqual(runtime.runtime.read()['codex_delegations']['calls'], {})
