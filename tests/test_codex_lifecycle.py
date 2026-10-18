"""A completed parent must not leave invisible Core workers running."""
import asyncio
import json
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from lg_cli.agent_runtime import runtime_factory
from lg_cli.anthropic_bridge import AnthropicBridge
from lg_cli.backend import Backend
from lg_cli.conversation_store import ConversationStore
from lg_cli.init_project import init_workspace
from lg_cli.project_store import ProjectStore
from lg_cli.session_runtime import SessionRuntime
from tests.helpers import make_config
from tests.test_anthropic_bridge import message_events


class FakeAgent:
    def __init__(self, config, event, **kwargs):
        self.event = event
        self.runtime = None
        self.thread_id = 'parent'
        self.turn_id = None
        self.closed = False
        self.collaborate = False
        self.error = None

    async def run(self, text, **kwargs):
        self.turn_id = 'turn'
        self.event('lg.turnBound', {'threadId': self.thread_id, 'turnId': self.turn_id})
        if self.collaborate:
            self.event('item/started', {'threadId': self.thread_id, 'turnId': self.turn_id,
                'item': {'type': 'subAgentActivity', 'id': 'spawn', 'kind': 'started',
                         'agentThreadId': 'child', 'agentPath': '/parent/critic'}})
        if self.error:
            raise self.error
        return {'id': self.turn_id, 'status': 'completed', 'items': []}

    async def stop_execution(self):
        self.closed = True

    async def close(self):
        await self.stop_execution()


class CodexLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = runtime_factory(make_config(Path(self.tmp.name)), lambda *_: None,
                                       agent_factory=FakeAgent)

    async def test_success_without_children_reuses_connection(self):
        await self.runtime.run('ordinary task')
        self.assertFalse(self.runtime.agent.closed)

    async def test_parent_completion_closes_workers_before_publishing_cancellation(self):
        closed_at_publication = []
        self.runtime.presentation.emit = lambda _, params: closed_at_publication.append(
            self.runtime.agent.closed) if params.get('item', {}).get('state') == 'cancelled' else None
        self.runtime.agent.collaborate = True
        await self.runtime.run('parent completes')
        self.assertTrue(self.runtime.agent.closed)
        self.assertEqual(closed_at_publication, [True])

    async def test_exception_timeout_and_external_cancellation_close_execution(self):
        for error in (ValueError('failed'), TimeoutError(), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                self.runtime.agent.closed = False
                self.runtime.agent.error = error
                with self.assertRaises(type(error)):
                    await self.runtime.run('aborted')
                self.assertTrue(self.runtime.agent.closed)

    async def test_cancelling_during_cleanup_waits_until_execution_is_stopped(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def stop():
            entered.set()
            await release.wait()
            self.runtime.agent.closed = True

        self.runtime.agent.stop_execution = stop
        self.runtime.agent.collaborate = True
        task = asyncio.create_task(self.runtime.run('finish and clean up'))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.runtime.agent.closed)
        self.assertTrue(self.runtime.execution_retired)


@unittest.skipUnless(os.environ.get('LG_TEST_RUNTIME_MANIFEST'), 'set LG_TEST_RUNTIME_MANIFEST for pinned Core')
class CoreChildLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_parent_completion_stops_late_child_tool_and_preserves_root_resume(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            init_workspace(root)
            config = replace(make_config(root), provider='stepfun', protocol='anthropic',
                default_model='step-3.5-flash', timeout_seconds=60,
                runtime_manifest=Path(os.environ['LG_TEST_RUNTIME_MANIFEST']))
            key = ConversationStore(root).create()
            child_started, release_child, child_called_again = (threading.Event() for _ in range(3))
            main_calls = 0
            payloads, events = [], []

            def response(payload):
                nonlocal main_calls
                payloads.append(payload)
                user_text = [part.get('text', '') for message in payload['messages'] if message['role'] == 'user'
                             for part in message['content'] if isinstance(part, dict) and part.get('type') == 'text']
                if any(text.strip().endswith('CHILD_LIFECYCLE_PROBE') for text in user_text):
                    if child_started.is_set():
                        child_called_again.set()
                        yield from message_events(delta={'type': 'text_delta', 'text': 'Child read finished.'})
                        return
                    child_started.set()
                    if not release_child.wait(20):
                        raise AssertionError('Child release timed out')
                    tool = next(t['name'] for t in payload['tools'] if 'Read shared project progress' in t.get('description', ''))
                    yield from message_events(block={'type': 'tool_use', 'id': 'late-child-read', 'name': tool, 'input': {}},
                        delta={'type': 'input_json_delta', 'partial_json': '{}'}, stop='tool_use')
                    return
                main_calls += 1
                if main_calls == 1:
                    tool = next(t['name'] for t in payload['tools'] if 'task_name' in t.get('input_schema', {}).get('properties', {}))
                    yield from message_events(block={'type': 'tool_use', 'id': 'spawn-child', 'name': tool, 'input': {}},
                        delta={'type': 'input_json_delta', 'partial_json': json.dumps({'task_name': 'critic',
                            'message': 'Read project memory, then report. CHILD_LIFECYCLE_PROBE', 'fork_turns': 'none'})},
                        stop='tool_use')
                else:
                    if main_calls == 2 and not child_started.wait(10):
                        raise AssertionError('Child did not start')
                    yield from message_events(delta={'type': 'text_delta', 'text': 'Parent task complete.'})

            runtime = runtime_factory(config, lambda method, params: events.append((method, params)), conversation=key)
            with patch('lg_cli.live_agent.provider_bridge', side_effect=lambda _: AnthropicBridge(config, stream_factory=response)):
                try:
                    await runtime.start()
                    process = runtime.agent.process
                    thread = runtime.thread_id
                    result = await runtime.run('Start a child, then finish this parent turn. ROOT_LIFECYCLE_PROBE')
                    self.assertEqual(result.status, 'completed')
                    self.assertTrue(child_started.is_set())
                    release_child.set()
                    for _ in range(100):
                        if child_called_again.is_set():
                            break
                        await asyncio.sleep(.02)
                    self.assertFalse(child_called_again.is_set(), 'Child executed a tool after the parent completed')
                    self.assertIsNone(runtime.agent.process)
                    self.assertIsNotNone(process.returncode)
                    runtime.runtime.update(mode='ask')
                    competitor = SessionRuntime(root, key)
                    try:
                        with self.assertRaisesRegex(ValueError, 'in use'):
                            competitor.acquire()
                    finally:
                        competitor.release()
                    self.assertTrue(any(params.get('item', {}).get('state') == 'cancelled' for _, params in events))
                    await runtime.start()
                    self.assertEqual(runtime.thread_id, thread)
                    history = await runtime.agent.request('thread/read', {'threadId': thread, 'includeTurns': True})
                    saved = next(turn for turn in history['thread']['turns'] if turn['id'] == result.result_id)
                    self.assertEqual(saved['status'], 'completed')
                    self.assertIn('Parent task complete.', json.dumps(saved['items']))
                    self.assertEqual((await runtime.run('Continue the saved parent conversation.')).status, 'completed')
                    self.assertEqual(main_calls, 3)
                    self.assertIn('ROOT_LIFECYCLE_PROBE', json.dumps(payloads[-1]))
                finally:
                    release_child.set()
                    await runtime.close()

    async def test_pending_child_write_is_revoked_without_cancelling_workflow_approval(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            init_workspace(root)
            config = replace(make_config(root), provider='stepfun', protocol='anthropic',
                default_model='step-3.5-flash', timeout_seconds=60,
                runtime_manifest=Path(os.environ['LG_TEST_RUNTIME_MANIFEST']))
            store = ProjectStore(root)
            document = store.create_document(kind='chapter', slug='one', title='One', content='Original.', state='accepted')
            requested = threading.Event()
            events, child_payloads = [], []
            main_calls = 0

            def event(record):
                events.append(record)
                if record['method'] == 'approval/requested' and record['params']['details'].get('tool') == 'edit_manuscript':
                    requested.set()

            def response(payload):
                nonlocal main_calls
                user_text = [part.get('text', '') for message in payload['messages'] if message['role'] == 'user'
                             for part in message['content'] if isinstance(part, dict) and part.get('type') == 'text']
                if any(text.strip().endswith('CHILD_WRITE_PROBE') for text in user_text):
                    child_payloads.append(payload)
                    if len(child_payloads) > 1:
                        yield from message_events(delta={'type': 'text_delta', 'text': 'Write rejected.'})
                        return
                    tool = next(t['name'] for t in payload['tools'] if 'Save an Agent-generated prose revision' in t.get('description', ''))
                    args = {'slug': 'one', 'content': 'Unapproved change.', 'expected_version_id': document.active_version_id,
                            'reason': 'test', 'book_overview': '维修师调查浮城停摆的真相。'}
                    yield from message_events(block={'type': 'tool_use', 'id': 'pending-child-write', 'name': tool, 'input': {}},
                        delta={'type': 'input_json_delta', 'partial_json': json.dumps(args)}, stop='tool_use')
                    return
                main_calls += 1
                if main_calls == 1:
                    tool = next(t['name'] for t in payload['tools'] if 'task_name' in t.get('input_schema', {}).get('properties', {}))
                    yield from message_events(block={'type': 'tool_use', 'id': 'spawn-write-child', 'name': tool, 'input': {}},
                        delta={'type': 'input_json_delta', 'partial_json': json.dumps({'task_name': 'critic',
                            'message': 'Try a candidate revision. CHILD_WRITE_PROBE', 'fork_turns': 'none'})}, stop='tool_use')
                else:
                    if not requested.wait(10):
                        raise AssertionError('Child write approval did not arrive')
                    yield from message_events(delta={'type': 'text_delta', 'text': 'Parent finished before child approval.'})

            backend = Backend(config, event)
            key = (await backend.dispatch('session/create', {}))['sessionId']
            state = backend.session(key)
            other = asyncio.create_task(state['control'].approval_handler({'purpose': 'Independent workflow'}, run_id='workflow-other'))
            with patch('lg_cli.live_agent.provider_bridge', side_effect=lambda _: AnthropicBridge(config, stream_factory=response)):
                try:
                    run = await backend.run(key, 'Start a child revision and finish before approving it.')
                    await asyncio.wait_for(backend.tasks[run['runId']][1], 30)
                    completion = [e for e in events if e['method'] == 'run/completed' and e['params']['runId'] == run['runId']][-1]
                    self.assertEqual(completion['params']['status'], 'completed', completion)
                    old = next(e['params']['approvalId'] for e in events if e['method'] == 'approval/requested'
                               and e['params']['details'].get('tool') == 'edit_manuscript')
                    with self.assertRaises(ValueError):
                        backend.approvals.resolve(old, {'decision': 'accept'})
                    self.assertFalse(other.done())
                    independent = next(e['params']['approvalId'] for e in events if e['method'] == 'approval/requested'
                                       and e['params'].get('runId') == 'workflow-other')
                    backend.approvals.resolve(independent, {'decision': 'decline'})
                    self.assertEqual(await other, {'decision': 'decline'})
                    self.assertEqual(len(store.list_versions('one')), 1)
                    self.assertEqual(store.get_document('one').content, 'Original.')
                    self.assertIsNone(state['control']._server)
                    self.assertFalse(state['agent_requests'])
                    await backend.dispatch('permissions/set', {'sessionId': key, 'mode': 'full_access'})
                    with self.assertRaisesRegex(ValueError, 'no longer active'):
                        await state['control'].approval_handler({'tool': 'edit_manuscript'})
                finally:
                    other.cancel()
                    await asyncio.gather(other, return_exceptions=True)
                    await backend.close()


class BackendRetirementTests(unittest.IsolatedAsyncioTestCase):
    async def test_revoke_before_slow_stop_and_finish_despite_repeated_cancellation(self):
        from tests.test_backend import FakeAgent as BackendAgent
        stop_entered, stop_release = asyncio.Event(), asyncio.Event()
        finish_entered, finish_release = asyncio.Event(), asyncio.Event()
        owners, requests, events = {}, [], []

        class SlowStopAgent(BackendAgent):
            async def run(self, text, **kwargs):
                self.turn_id = 'turn-1'
                self.event('lg.turnBound', {'threadId': self.thread_id, 'turnId': self.turn_id})
                self.event('item/started', {'threadId': self.thread_id, 'turnId': self.turn_id,
                    'item': {'id': 'spawn', 'type': 'subAgentActivity', 'kind': 'started',
                             'agentThreadId': 'child', 'agentPath': '/parent/critic'}})
                requests.append(asyncio.create_task(owners['state']['control'].approval_handler({'tool': 'edit_manuscript'})))
                for _ in range(200):
                    if any(e['method'] == 'approval/requested' and e['params']['details'].get('tool') == 'edit_manuscript' for e in events):
                        break
                    await asyncio.sleep(.01)
                return await super().run('Durable parent answer.')

            async def stop_execution(self):
                stop_entered.set()
                await stop_release.wait()
                self.closed = True

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            init_workspace(root)
            backend = Backend(make_config(root), events.append, agent_factory=SlowStopAgent)
            key = (await backend.dispatch('session/create', {}))['sessionId']
            state = owners['state'] = backend.session(key)
            original = state['control'].retire_endpoint
            calls = 0

            async def delayed_retire():
                nonlocal calls
                calls += 1
                if calls == 2:
                    finish_entered.set()
                    await finish_release.wait()
                await original()

            state['control'].retire_endpoint = delayed_retire
            independent = asyncio.create_task(state['control'].approval_handler({'purpose': 'Workflow'}, run_id='workflow-other'))
            try:
                run = await backend.run(key, 'Finish while a child awaits approval')
                task = backend.tasks[run['runId']][1]
                await asyncio.wait_for(stop_entered.wait(), 3)
                old = next(e['params']['approvalId'] for e in events if e['method'] == 'approval/requested'
                           and e['params']['details'].get('tool') == 'edit_manuscript')
                with self.assertRaises(ValueError):
                    backend.approvals.resolve(old, {'decision': 'accept'})
                self.assertTrue(requests[0].cancelled())
                self.assertFalse(independent.done())
                self.assertIsNone(state['agent_run_id'])
                stop_release.set()
                await asyncio.wait_for(finish_entered.wait(), 3)
                for _ in range(2):
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                finish_release.set()
                await asyncio.wait_for(task, 3)
                self.assertIsNone(state['control']._server)
                self.assertIn('Durable parent answer.', backend.store.context(key))
                self.assertFalse(independent.done())
                other = next(e['params']['approvalId'] for e in events if e['method'] == 'approval/requested'
                             and e['params'].get('runId') == 'workflow-other')
                backend.approvals.resolve(other, {'decision': 'decline'})
                self.assertEqual(await independent, {'decision': 'decline'})
                state['agent'].runtime.update(mode='ask')
            finally:
                stop_release.set()
                finish_release.set()
                independent.cancel()
                await asyncio.gather(independent, *requests, return_exceptions=True)
                await backend.close()
