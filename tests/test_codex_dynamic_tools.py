"""Dynamic tools bind to a saved root contract and a live transport turn."""
import asyncio
import copy
import json
import os
import tempfile
import unittest
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from lg_cli.agent_runtime import RuntimePolicyError
from lg_cli.anthropic_bridge import AnthropicBridge
from lg_cli.codex_policy import WritingLaunchPolicy
from lg_cli.conversation_store import ConversationStore
from lg_cli.init_project import init_workspace
from lg_cli.live_agent import CodexSession, dynamic_tools_sha256
from tests.helpers import make_config
from tests.test_anthropic_bridge import message_events


TOOLS = [{'type': 'function', 'name': 'delegate_review', 'description': 'Transport dynamic fixture',
          'inputSchema': {'type': 'object', 'properties': {'focus': {'type': 'string'}},
                          'additionalProperties': False}, 'deferLoading': False}]
HANDOFF = {'contentItems': [{'type': 'inputText', 'text': '{"provisional":true}'}], 'success': True}


class Context:
    def __init__(self):
        self.options = []

    def instructions(self, **kwargs):
        self.options.append(kwargs)
        return 'Only perform the supplied transport fixture task.'

    def history(self, text):
        return text


class DynamicContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        init_workspace(self.root)
        self.key = ConversationStore(self.root).create()
        self.config = make_config(self.root)
        self.context = Context()
        self.policy = SimpleNamespace(prepare=lambda *a, **k: SimpleNamespace(overrides=(), validate=lambda _: None))
        self.sessions = []
        self.spawns = self.enterContext(patch('lg_cli.live_agent.asyncio.create_subprocess_exec', new=AsyncMock(
            side_effect=lambda *a, **k: SimpleNamespace(returncode=None))))
        self.enterContext(patch('lg_cli.live_agent.discover_core_commands', return_value=[SimpleNamespace(available=True, command=['fixed-core'])]))
        self.enterContext(patch('lg_cli.live_agent.provider_bridge', side_effect=lambda _: nullcontext(None)))
        self.enterContext(patch('lg_cli.live_agent._adapter_env', return_value={}))

    async def asyncTearDown(self):
        for session in self.sessions:
            await asyncio.gather(*(t for t in (session._reader, session._stderr) if t), return_exceptions=True)
            session._resources.close()
            if session.runtime:
                session.runtime.release()

    def session(self, *, tools=TOOLS, review_only=False, root='root'):
        session = CodexSession(self.config, lambda *_: None, context_provider=self.context,
            launch_policy=self.policy, conversation=self.key, dynamic_tools=tools, review_only=review_only)
        session._read = AsyncMock()
        session._drain_stderr = AsyncMock()
        session._send = AsyncMock()
        session.close = AsyncMock()

        async def request(method, params):
            return {'thread': {'id': root, 'parentThreadId': None}} if method in {'thread/start', 'thread/resume'} else {}

        session.request = AsyncMock(side_effect=request)
        self.sessions.append(session)
        return session

    async def test_start_saves_one_frozen_contract_and_resume_never_reregisters(self):
        supplied = copy.deepcopy(TOOLS)
        first = self.session(tools=supplied)
        first.review_delegation = True
        first.allow_core_delegation = False
        supplied[0]['inputSchema']['properties']['injected'] = {'type': 'string'}
        with patch.object(self.policy, 'prepare', wraps=self.policy.prepare) as prepare:
            await first.start()
        self.assertEqual(prepare.call_args.kwargs, {'control_socket': None, 'review_only': False,
                                                  'allow_core_delegation': False})
        sent = next(call.args[1] for call in first.request.await_args_list if call.args[0] == 'thread/start')
        self.assertEqual(sent['dynamicTools'], TOOLS)
        self.assertTrue(first.dynamic_tools_registered)
        self.assertEqual(self.context.options[-1], {'control_socket': None, 'core_delegation': False,
                                                  'review_delegation': True})
        contract = first.runtime.read()['codex_tool_contract']
        self.assertEqual(contract, {'schema': 'lg.codex-tools.v1', 'rootThreadId': 'root',
                                    'sha256': dynamic_tools_sha256(TOOLS)})
        first.runtime.release()
        resumed = self.session()
        resumed.review_delegation = True
        await resumed.start()
        self.assertTrue(resumed.dynamic_tools_registered)
        calls = [(call.args[0], call.args[1]) for call in resumed.request.await_args_list]
        self.assertTrue(any(method == 'thread/resume' and params['threadId'] == 'root' for method, params in calls))
        self.assertFalse(any('dynamicTools' in params for _, params in calls))
        self.assertFalse(any(method == 'thread/start' for method, _ in calls))

    async def test_legacy_root_stays_legacy_and_disabled_clients_do_not_claim_tools(self):
        legacy = self.session(tools=None)
        await legacy.start()
        self.assertNotIn('codex_tool_contract', legacy.runtime.read())
        legacy.runtime.release()
        resumed = self.session()
        resumed.review_delegation = True
        with patch.object(self.policy, 'prepare', wraps=self.policy.prepare) as prepare:
            await resumed.start()
        self.assertEqual(prepare.call_args.kwargs, {'control_socket': None, 'review_only': False})
        self.assertFalse(resumed.dynamic_tools_registered)
        self.assertNotIn('review_delegation', self.context.options[-1])
        self.assertFalse(any(call.args[0] == 'thread/start' for call in resumed.request.await_args_list))
        resumed.runtime.release()
        # A valid saved registration remains readable by clients without this tool surface.
        resumed.runtime.acquire()
        resumed.runtime.update(codex_tool_contract={'schema': 'lg.codex-tools.v1', 'rootThreadId': 'root',
                                                   'sha256': dynamic_tools_sha256(TOOLS)})
        resumed.runtime.release()
        for options in ({'tools': None}, {'review_only': True}):
            reader = self.session(**options)
            await reader.start()
            self.assertFalse(reader.dynamic_tools_registered)
            reader.runtime.release()

    async def test_contract_mismatch_fails_before_spawn_and_keeps_saved_identity(self):
        first = self.session()
        await first.start()
        saved = first.runtime.read()
        first.runtime.release()
        changed = copy.deepcopy(TOOLS)
        changed[0]['name'] = 'another_tool'
        for contract in (saved['codex_tool_contract'], {**saved['codex_tool_contract'], 'rootThreadId': 'foreign'}):
            first.runtime.acquire()
            first.runtime.update(codex_tool_contract=contract)
            first.runtime.release()
            bad = self.session(tools=changed if contract['rootThreadId'] == 'root' else TOOLS)
            before = self.spawns.await_count
            with self.assertRaises(RuntimePolicyError):
                await bad.start()
            self.assertEqual(self.spawns.await_count, before)
            self.assertFalse(bad.dynamic_tools_registered)
            self.assertEqual(bad.runtime.read()['thread_id'], 'root')
            bad.runtime.release()

    async def test_failed_or_foreign_start_never_commits_contract(self):
        for fail in ('request', 'subagent'):
            session = self.session()
            if fail == 'request':
                original = session.request.side_effect

                async def request(method, params):
                    if method == 'thread/start':
                        raise ValueError('Synthetic start rejection')
                    return await original(method, params)

                session.request.side_effect = request
            else:
                session.request.side_effect = lambda method, params: ({'thread': {'id': 'child', 'parentThreadId': 'parent'}}
                    if method == 'thread/start' else {})
            with self.assertRaises((ValueError, RuntimePolicyError)):
                await session.start()
            self.assertFalse(session.dynamic_tools_registered)
            self.assertNotIn('codex_tool_contract', session.runtime.read())
            session.runtime.release()


class DynamicRequestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = CodexSession(make_config(Path(self.tmp.name)), lambda *_: None,
            context_provider=Context(), launch_policy=SimpleNamespace(), dynamic_tools=TOOLS)
        self.session.process = object()
        self.session.thread_id = 'root'
        self.session.dynamic_tools_registered = True
        self.session._send = AsyncMock()

    def bind(self):
        self.session.turn_id = 'turn'
        self.session._completion = asyncio.get_running_loop().create_future()
        self.session._ready.set()

    def call(self):
        return {'id': 'rpc', 'method': 'item/tool/call', 'params': {
            'threadId': 'root', 'turnId': 'turn', 'callId': 'call', 'namespace': None,
            'tool': 'delegate_review', 'arguments': {}}}

    async def test_early_request_waits_for_real_turn_before_returning_wire_result(self):
        entered = asyncio.Event()

        async def handler(method, params):
            entered.set()
            self.assertEqual(await self.session.wait_for_turn(), params['turnId'])
            return HANDOFF

        self.session.on_request = handler
        task = asyncio.create_task(self.session._server_request(self.call()))
        await entered.wait()
        self.session._send.assert_not_awaited()
        self.bind()
        await task
        self.session._send.assert_awaited_once_with({'id': 'rpc', 'result': HANDOFF},
            turn_guard=(self.session.process, 'root', 'turn'))

    async def test_turn_guard_rechecks_after_waiting_for_wire_lock(self):
        self.bind()
        writer = SimpleNamespace(write=lambda _: self.fail('Late response reached the wire'), drain=AsyncMock())
        process = self.session.process = SimpleNamespace(returncode=None, stdin=writer)
        await self.session._write_lock.acquire()
        task = asyncio.create_task(CodexSession._send(self.session, {'id': 'old', 'result': HANDOFF},
            turn_guard=(process, 'root', 'turn')))
        await asyncio.sleep(0)
        self.session.turn_id = 'new-turn'
        self.session._write_lock.release()
        await task
        writer.drain.assert_not_awaited()

    async def test_late_turn_or_replaced_process_never_receives_success(self):
        for change in ('turn', 'process', 'completed'):
            self.bind()
            self.session._send.reset_mock()

            async def handler(*args):
                if change == 'turn':
                    self.session.turn_id = 'next-turn'
                elif change == 'process':
                    self.session.process = object()
                else:
                    self.session._completion.set_result({})
                return HANDOFF

            self.session.on_request = handler
            await self.session._server_request(self.call())
            self.session._send.assert_not_awaited()

    async def test_private_handler_failure_is_sanitized_and_cancellation_sends_no_reply(self):
        self.bind()
        self.session.on_request = AsyncMock(side_effect=TypeError('PRIVATE_TOOL_BODY'))
        await self.session._server_request(self.call())
        reply = self.session._send.await_args.args[0]
        self.assertEqual(reply['error'], {'code': -32000, 'message': 'Request rejected by host policy'})
        self.assertNotIn('PRIVATE', json.dumps(reply))
        self.session._send.reset_mock()
        self.session.on_request = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await self.session._server_request(self.call())
        self.session._send.assert_not_awaited()

    async def test_unregistered_or_child_requests_fail_before_host_handler(self):
        self.bind()
        handler = self.session.on_request = AsyncMock(return_value=HANDOFF)
        for child in (False, True):
            self.session.dynamic_tools_registered = child
            record = self.call()
            if child:
                record['params']['threadId'] = 'child'
            await self.session._server_request(record)
        handler.assert_not_awaited()
        self.assertEqual(self.session._send.await_count, 2)

    async def test_turn_wait_is_bounded_and_rejects_completed_turn(self):
        self.session.config = replace(self.session.config, timeout_seconds=.01)
        with self.assertRaises(ValueError):
            await self.session.wait_for_turn()
        self.bind()
        self.session._completion.set_result({})
        with self.assertRaises(ValueError):
            await self.session.wait_for_turn()


@unittest.skipUnless(os.environ.get('LG_TEST_RUNTIME_MANIFEST'), 'set LG_TEST_RUNTIME_MANIFEST for pinned Core')
class CoreDynamicTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_tool_call_and_cold_resume_use_same_registered_root(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            init_workspace(root)
            config = replace(make_config(root), provider='stepfun', protocol='anthropic',
                default_model='step-3.5-flash', timeout_seconds=60, environment='dynamic-transport-fixture',
                runtime_manifest=Path(os.environ['LG_TEST_RUNTIME_MANIFEST']))
            key = ConversationStore(root).create()
            requests, handled = [], []

            def response(payload):
                if any(part.get('type') == 'tool_result' for part in payload['messages'][-1]['content']):
                    yield from message_events(delta={'type': 'text_delta', 'text': 'Transport complete.'})
                    return
                tool = next(tool['name'] for tool in payload['tools'] if tool.get('description') == TOOLS[0]['description'])
                yield from message_events(block={'type': 'tool_use', 'id': 'transport-call', 'name': tool, 'input': {}},
                    delta={'type': 'input_json_delta', 'partial_json': '{}'}, stop='tool_use')

            original = CodexSession.request

            async def record(session, method, params):
                requests.append((method, copy.deepcopy(params)))
                return await original(session, method, params)

            thread_id = None
            with patch('lg_cli.live_agent.provider_bridge', side_effect=lambda _: AnthropicBridge(config, stream_factory=response)), \
                    patch('lg_cli.credentials.read_profiles', return_value={'active': None, 'profiles': {}}), \
                    patch.object(CodexSession, 'request', record):
                for _ in range(2):
                    session = CodexSession(config, lambda *_: None, conversation=key, context_provider=Context(),
                        launch_policy=WritingLaunchPolicy(),
                        dynamic_tools=TOOLS)

                    async def handler(method, params):
                        self.assertEqual(method, 'item/tool/call')
                        self.assertEqual(params['threadId'], session.thread_id)
                        self.assertEqual(params['turnId'], await session.wait_for_turn())
                        self.assertIsNone(params.get('namespace'))
                        self.assertEqual(params['tool'], 'delegate_review')
                        handled.append((params['threadId'], params['turnId'], params['callId']))
                        return HANDOFF

                    session.on_request = handler
                    try:
                        await session.start()
                        self.assertTrue(session.dynamic_tools_registered)
                        thread_id = thread_id or session.thread_id
                        self.assertEqual(session.thread_id, thread_id)
                        result = await session.run('Use the supplied dynamic fixture once, then reply.')
                        self.assertEqual(result['status'], 'completed')
                    finally:
                        await session.close()
            self.assertEqual(len(handled), 2)
            self.assertEqual(len({turn for _, turn, _ in handled}), 2)
            self.assertEqual(len([1 for method, _ in requests if method == 'thread/start']), 1)
            self.assertEqual(len([1 for method, _ in requests if method == 'thread/resume']), 1)
            self.assertTrue(all('dynamicTools' not in params for method, params in requests if method == 'thread/resume'))
