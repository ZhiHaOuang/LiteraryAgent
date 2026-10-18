"""Bounded expert requests follow the real parent-turn and cleanup lifecycle."""
import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from lg_cli.agent_runtime import runtime_factory
from lg_cli.conversation_store import ConversationStore
from lg_cli.session_runtime import SessionRuntime
from tests.helpers import make_config
from tests.test_native_runtime import answer
from tests.test_review_policy import Context, PreparedExpertPolicy


class ParentAgent:
    def __init__(self, config, event, *, conversation, on_request, dynamic_tools, **kwargs):
        self.runtime = SessionRuntime(config.workspace, conversation)
        self.event, self.on_request = event, on_request
        self.dynamic_tools_registered = bool(dynamic_tools)
        self.thread_id = self.turn_id = None
        self.closed = False
        self.stop_calls = 0
        self.run_entered = asyncio.Event()
        self.bind_allowed = asyncio.Event()
        self.ready = asyncio.Event()
        self.wait_entered = asyncio.Event()
        self.parent_done = asyncio.Event()
        self.interrupt_entered = asyncio.Event()
        self.interrupt_release = asyncio.Event()
        self.interrupt_cancelled = asyncio.Event()
        self.interrupt_release.set()
        self.order = []

    async def start(self, **kwargs):
        self.runtime.acquire()
        self.thread_id = self.runtime.read().get('thread_id') or 'root-thread'
        self.runtime.update(thread_id=self.thread_id)

    async def run(self, text, **kwargs):
        self.run_entered.set()
        try:
            await self.bind_allowed.wait()
            self.turn_id = 'parent-turn'
            self.event('lg.turnBound', {'threadId': self.thread_id, 'turnId': self.turn_id})
            self.ready.set()
            await self.parent_done.wait()
            return {'id': self.turn_id, 'status': 'completed',
                    'items': [{'type': 'agentMessage', 'text': 'Durable parent answer.'}]}
        finally:
            self.turn_id = None
            self.ready.set()

    async def wait_for_turn(self):
        self.wait_entered.set()
        await self.ready.wait()
        if self.turn_id is None:
            raise ValueError('No active parent turn')
        return self.turn_id

    async def interrupt(self):
        self.interrupt_entered.set()
        try:
            await self.interrupt_release.wait()
        except asyncio.CancelledError:
            self.interrupt_cancelled.set()
            raise

    async def stop_execution(self):
        self.order.append('stop')
        self.stop_calls += 1
        self.closed = True
        self.parent_done.set()

    async def close(self):
        await self.stop_execution()
        self.runtime.release()


class CodexRuntimeReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = replace(make_config(self.root), protocol='responses', default_model='parent-model',
                              critic_model='expert-model', timeout_seconds=5)
        self.policy = PreparedExpertPolicy()
        self.events, self.requests, self.tasks = [], [], []
        self.expert_entered = asyncio.Event()
        self.expert_cancelled = asyncio.Event()
        self.expert_release = asyncio.Event()
        self.block_expert = False
        outer = self

        class Model:
            def __init__(self, config):
                self.config = config

            async def generate(self, request, delta):
                outer.requests.append(request)
                outer.expert_entered.set()
                if outer.block_expert:
                    try:
                        await asyncio.Future()
                    except asyncio.CancelledError:
                        outer.expert_cancelled.set()
                        while not outer.expert_release.is_set():
                            try:
                                await outer.expert_release.wait()
                            except asyncio.CancelledError:
                                pass
                        outer.runtime.agent.order.append('expert-exited')
                        return answer('{"summary":"PRIVATE_LATE_EXPERT"}')
                return answer('{"summary":"Provisional expert observation"}')

        key = ConversationStore(self.root).create()
        self.runtime = runtime_factory(self.config, lambda method, params: self.events.append((method, params)),
            agent_factory=ParentAgent, conversation=key, context_provider=Context(),
            expert_policy=self.policy, model_factory=Model)
        await self.runtime.start()

    async def asyncTearDown(self):
        self.expert_release.set()
        self.runtime.agent.interrupt_release.set()
        self.runtime.agent.bind_allowed.set()
        self.runtime.agent.parent_done.set()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.runtime.close()
        self.tmp.cleanup()

    def spawn(self, operation):
        task = asyncio.create_task(operation)
        self.tasks.append(task)
        return task

    def params(self, call_id='expert-call'):
        return {'threadId': 'root-thread', 'turnId': 'parent-turn', 'callId': call_id,
                'namespace': None, 'tool': 'delegate_review', 'arguments': {'experts': ['critic']}}

    async def begin_parent(self):
        self.runtime.agent.bind_allowed.set()
        parent = self.spawn(self.runtime.run('Review host-selected evidence.'))
        await asyncio.wait_for(self.runtime.agent.ready.wait(), 1)
        return parent

    async def begin_blocked_expert(self):
        self.block_expert = True
        parent = await self.begin_parent()
        request = self.spawn(self.runtime.agent.on_request('item/tool/call', self.params()))
        await asyncio.wait_for(self.expert_entered.wait(), 1)
        return parent, request

    async def test_missing_interrupt_ack_preserves_the_callers_timeout(self):
        parent = await self.begin_parent()
        self.runtime.agent.interrupt_release.clear()
        interrupt = self.spawn(asyncio.wait_for(self.runtime.interrupt(), .02))
        await asyncio.wait_for(self.runtime.agent.interrupt_entered.wait(), 1)
        done, _ = await asyncio.wait({interrupt}, timeout=.5)
        self.assertIn(interrupt, done, 'Missing Core ack defeated the host interrupt deadline')
        with self.assertRaises(asyncio.TimeoutError):
            await interrupt
        self.assertTrue(self.runtime.agent.interrupt_cancelled.is_set())
        self.assertFalse(self.runtime.delegations.pending)
        self.runtime.agent.parent_done.set()
        await asyncio.wait_for(parent, 1)

    async def test_early_request_waits_and_only_a_real_turn_binding_begins_the_budget(self):
        parent = self.spawn(self.runtime.run('Review host-selected evidence.'))
        await asyncio.wait_for(self.runtime.agent.run_entered.wait(), 1)
        request = self.spawn(self.runtime.agent.on_request('item/tool/call', self.params()))
        await asyncio.wait_for(self.runtime.agent.wait_entered.wait(), 1)
        self.runtime.agent.event('lg.turnBound', {'threadId': 'foreign-root', 'turnId': 'parent-turn'})
        self.assertIsNone(self.runtime.delegations.reviews.parent_turn)
        self.assertFalse(request.done())
        self.assertEqual(self.policy.calls, [])
        self.assertEqual(self.requests, [])
        self.runtime.agent.bind_allowed.set()
        self.assertTrue((await asyncio.wait_for(request, 1))['success'])
        self.assertEqual(self.runtime.delegations.reviews.parent_turn, 'parent-turn')
        self.runtime.agent.event('lg.turnBound', {'threadId': 'root-thread', 'turnId': 'parent-turn'})
        repeated = await self.runtime.agent.on_request('item/tool/call', self.params('repeat-expert'))
        self.assertFalse(repeated['success'])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.policy.calls), 1)
        self.runtime.agent.parent_done.set()
        self.assertEqual((await asyncio.wait_for(parent, 1)).status, 'completed')
        self.assertEqual(self.runtime.agent.stop_calls, 0)

    async def test_parent_completion_revokes_experts_and_waits_before_retiring_core(self):
        parent, request = await self.begin_blocked_expert()
        self.runtime.agent.parent_done.set()
        await asyncio.wait_for(self.expert_cancelled.wait(), 1)
        self.assertFalse(parent.done())
        self.assertFalse(self.runtime._run_active)
        self.assertFalse(self.runtime.delegations.reviews.live)
        self.assertEqual(self.runtime.agent.stop_calls, 0)
        with self.assertRaisesRegex(ValueError, 'already active'):
            await self.runtime.run('Do not overlap the previous turn cleanup.')
        self.expert_release.set()
        result = await asyncio.wait_for(parent, 1)
        await asyncio.gather(request, return_exceptions=True)
        self.assertEqual((result.status, result.text), ('completed', 'Durable parent answer.'))
        self.assertTrue(self.runtime.execution_retired)
        self.assertEqual(self.runtime.agent.order, ['expert-exited', 'stop'])
        self.assertFalse(self.runtime.delegations.pending)
        saved = self.runtime.runtime.read()['codex_delegations']['calls']['expert-call']
        self.assertTrue(saved['interrupted'])
        self.assertIsNone(saved['record']['children'][0]['result'])
        self.assertNotIn('PRIVATE_LATE_EXPERT', json.dumps(self.events))

    async def test_repeated_parent_cancellation_cannot_skip_expert_drain(self):
        parent, request = await self.begin_blocked_expert()
        self.runtime.agent.parent_done.set()
        await asyncio.wait_for(self.expert_cancelled.wait(), 1)
        for _ in range(2):
            parent.cancel()
            await asyncio.sleep(0)
            self.assertFalse(parent.done())
            self.assertEqual(self.runtime.agent.stop_calls, 0)
        self.expert_release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(parent, 1)
        await asyncio.gather(request, return_exceptions=True)
        self.assertEqual(self.runtime.agent.order, ['expert-exited', 'stop'])
        self.assertTrue(self.runtime.execution_retired)
        self.assertFalse(self.runtime.delegations.pending)
