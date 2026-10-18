"""CLI and Extension share ownership, approvals, history and cancellation."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from lg_cli.backend import Backend, ProcessHost
from lg_cli.conversation_store import ConversationStore
from lg_cli.project_store import ProjectStore
from lg_cli.intent_router import proposal
from lg_cli.terminal_backend import TerminalBackendClient
from lg_cli.workflow_control import WorkflowControl, control_request
from tests.helpers import make_config
from tests.test_backend import FakeAgent, start_waiting_run


class View:
    thread_id = None
    def __init__(self):
        self.events = []
    def __call__(self, method, params):
        self.events.append((method, params))


class TerminalBackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ProjectStore(self.root).initialize(name='终端统一会话')
        self.key = ConversationStore(self.root).create()
        self.config = make_config(self.root)
        self.view = View()
        self.control = WorkflowControl(ProcessHost(lambda *_: None))
        self.questions = []
        async def answer(method, params):
            self.questions.append((method, params))
            return {'decision': 'decline'}
        self.client = TerminalBackendClient(self.config, self.view, on_request=answer,
            control=self.control, conversation=self.key, agent_factory=FakeAgent,route_tasks=False)
        await self.client.start()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_one_service_saves_messages_once_and_extension_recovers_same_items(self):
        await self.client.run('继续第一章')
        messages = ConversationStore(self.root).read(self.key)
        self.assertEqual([m['text'] for m in messages], ['继续第一章', '继续第一章'])
        self.assertEqual([m['role'] for m in messages], ['user', 'assistant'])
        self.assertTrue(all(m.get('run_id') for m in messages))
        await self.client.close()
        extension = Backend(self.config, agent_factory=FakeAgent)
        try:
            await extension.dispatch('session/open', {'sessionId': self.key})
            snapshot = await extension.dispatch('session/snapshot', {'sessionId': self.key})
            self.assertEqual([i['kind'] for i in snapshot['items']], ['user', 'assistant'])
            self.assertEqual([i['text'] for i in snapshot['items']], ['继续第一章', '继续第一章'])
        finally:
            await extension.close()

    async def test_extension_cannot_open_a_cli_owned_session(self):
        extension = Backend(self.config, agent_factory=FakeAgent)
        try:
            with self.assertRaisesRegex(ValueError, 'in use'):
                await extension.dispatch('session/open', {'sessionId': self.key})
        finally:
            await extension.close()

    async def test_runtime_and_product_approval_wait_for_terminal_decision(self):
        await self.client.run('approve')
        self.assertEqual(self.questions[0][0], 'item/commandExecution/requestApproval')
        self.assertIn('decline', ConversationStore(self.root).read(self.key)[-1]['text'])
        await self.client.set_permissions('full_access', 'ask')
        await start_waiting_run(self.client.backend,self.key)
        endpoint = await self.control.endpoint()
        result = await asyncio.to_thread(control_request, endpoint, 'approval', details={'tool': 'studio_accept'})
        self.assertEqual(result, {'decision': 'decline'})
        self.assertEqual(self.questions[-1][0], 'lg/creativeApproval')
        result = await asyncio.to_thread(control_request, endpoint, 'approval', details={'tool': 'studio_draft'})
        self.assertEqual(result['decision'], 'accept')
        self.assertEqual(result['generation_sources']['run_id'], self.client.backend.session(self.key)['run_id'])
        self.assertEqual(result['generation_sources']['coverage'], 'unknown')
        self.assertEqual(result['generation_sources']['observations'], [])

    async def test_cancel_expires_dialog_and_releases_execution(self):
        opened, stopped = asyncio.Event(), asyncio.Event()
        async def answer(*_):
            opened.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()
        self.client.on_request = answer
        pending = asyncio.create_task(self.client.run('approve'))
        await asyncio.wait_for(opened.wait(), 2)
        await self.client.interrupt()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        await asyncio.wait_for(stopped.wait(), 2)
        self.assertFalse(self.client.backend.approvals.pending)
        await self.client.run('取消后继续')
        self.assertEqual(ConversationStore(self.root).read(self.key)[-1]['text'], '取消后继续')

    async def test_workflow_status_is_shared_but_not_saved_as_author_text(self):
        self.control.job = {'id': 'finished-job', 'command': '/outline 原计划', 'status': 'completed',
            'attempt': 1, 'guidance': '', 'returncode': 0}
        await self.client.run('修改计划方向')
        messages = ConversationStore(self.root).read(self.key)
        self.assertEqual(messages[0]['text'], '修改计划方向')
        self.assertIn('/outline 原计划', messages[1]['text'])
        self.assertIs(self.client.backend.session(self.key)['control'], self.control)

    async def test_dialog_failure_cannot_approve_operation(self):
        async def broken(*_):
            raise ValueError('Dialog unavailable')
        self.client.on_request = broken
        await self.client.run('approve')
        self.assertIn('cancel', ConversationStore(self.root).read(self.key)[-1]['text'])
        self.assertFalse(self.client.backend.approvals.pending)

    async def test_automatic_creative_rejection_reason_is_visible_without_dialog(self):
        notices = []
        self.client.on_notice = notices.append
        await self.client.set_permissions('full_access', 'auto_review')
        await start_waiting_run(self.client.backend,self.key)
        endpoint = await self.control.endpoint()
        with patch('lg_cli.creative_review.review_creative', AsyncMock(return_value={
            'decision': 'decline', 'reason': '来源不足，不能确认事实。'})):
            result = await asyncio.to_thread(control_request, endpoint, 'approval', details={'tool': 'studio_accept_proposal'})
        self.assertEqual(result['decision'], 'decline')
        self.assertIn('来源不足', notices[0])
        self.assertEqual(self.questions, [])


class TerminalPermissionEntryTests(unittest.TestCase):
    def test_explicit_runtime_options_survive_interactive_config_reload(self):
        import argparse
        from dataclasses import replace
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from lg_cli.main import interactive_loop
        from lg_cli.terminal_input import LiteraryInput
        with tempfile.TemporaryDirectory() as raw, create_pipe_input() as pipe:
            root = Path(raw)
            ProjectStore(root).initialize(name='Runtime 选择')
            configured = make_config(root)
            manifest = root/'selected-runtime.json'
            options = argparse.Namespace(runtime_engine='native',runtime_manifest=manifest)
            seen = []
            class Client(TerminalBackendClient):
                def __init__(self,config,*args,**kwargs):
                    seen.append((config.runtime_engine,config.runtime_manifest))
                    super().__init__(config,*args,**kwargs,agent_factory=FakeAgent,
                        task_router=AsyncMock(return_value=proposal()))
            session = LiteraryInput(root/'history',input=pipe,output=DummyOutput(),usage_path=root/'usage.json')
            def run(handler,*_args,**_kwargs):
                async def exercise():
                    try:
                        await handler('只讨论，不写正文')
                        await handler('继续讨论')
                    finally:
                        await session.shutdown_handler()
                    return 0
                return asyncio.run(exercise())
            with (
                patch('lg_cli.main.LiteraryInput',return_value=session),
                patch.object(session,'run',run),
                patch('lg_cli.main.load_config',return_value=configured),
                patch('lg_cli.terminal_backend.TerminalBackendClient',Client),
            ):
                self.assertEqual(interactive_loop(replace(configured,runtime_engine='native',runtime_manifest=manifest),
                    runtime_options=options),0)
            self.assertEqual(seen,[('native',manifest)])

    def test_permissions_entry_uses_shared_service_without_starting_model(self):
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from lg_cli.main import interactive_loop
        from lg_cli.session_runtime import SessionRuntime
        from lg_cli.terminal_input import LiteraryInput
        with tempfile.TemporaryDirectory() as raw, create_pipe_input() as pipe:
            root = Path(raw)
            ProjectStore(root).initialize(name='权限入口')
            config = make_config(root)
            session = LiteraryInput(root / 'history', input=pipe, output=DummyOutput(), usage_path=root / 'usage.json')
            def run(handler, *_args, **_kwargs):
                async def exercise():
                    try:
                        await handler('/permissions')
                        key = ConversationStore(root).list()[0][0]
                        settings = SessionRuntime(root, key).read()
                        self.assertEqual((settings['mode'], settings['creative_mode']), ('full_access', 'ask'))
                        self.assertIsNone(settings['thread_id'])
                    finally:
                        await session.shutdown_handler()
                    return 0
                return asyncio.run(exercise())
            with patch('lg_cli.main.LiteraryInput', return_value=session), \
                 patch.object(session, 'run', run), patch.object(session, 'ask', AsyncMock(side_effect=['full_access', 'ask'])), \
                 patch('lg_cli.live_agent.LiveAgent', FakeAgent), \
                 patch.object(FakeAgent, 'start', side_effect=AssertionError('Permission changes must not call a model')):
                self.assertEqual(interactive_loop(config), 0)
