"""Actual CLI entry then Backend resume, using the pinned runtime and local provider."""
import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from lg_cli.anthropic_bridge import AnthropicBridge
from lg_cli.backend import Backend
from lg_cli.conversation_store import ConversationStore
from lg_cli.init_project import init_workspace
from lg_cli.main import interactive_loop
from lg_cli.terminal_input import LiteraryInput
from tests.helpers import make_config
from tests.test_anthropic_bridge import message_events
from tests.intent_fixture import current_user,routed_reply


@unittest.skipUnless(os.environ.get('LG_TEST_RUNTIME_MANIFEST'), 'set LG_TEST_RUNTIME_MANIFEST for pinned-runtime integration')
class TerminalRuntimeTests(unittest.TestCase):
    def test_actual_cli_and_extension_service_resume_one_runtime_thread(self):
        with tempfile.TemporaryDirectory() as raw, create_pipe_input() as pipe:
            root = Path(raw)
            init_workspace(root)
            config = replace(make_config(root), provider='stepfun', protocol='anthropic',
                default_model='step-3.5-flash', runtime_manifest=Path(os.environ['LG_TEST_RUNTIME_MANIFEST']),
                timeout_seconds=60)
            session = LiteraryInput(root / 'history', input=pipe, output=DummyOutput(), usage_path=root / 'usage.json')
            payloads = []
            def response(payload):
                payloads.append(payload)
                routed=routed_reply(current_user(payload.get('messages',[])))
                yield from message_events(delta={'type': 'text_delta', 'text': json.dumps(routed,ensure_ascii=False) if routed is not None else '星港的灯亮了，林砚开始检查闸门。'})
            def run(handler, *_args, **_kwargs):
                async def exercise():
                    try:
                        await handler('记住 CLI_SHARED_THREAD_MARKER；只简短回答，不使用工具。')
                        self.assertIn('星港的灯亮了', '\n'.join(text for _, text in session._blocks))
                    finally:
                        await session.shutdown_handler()
                    store = ConversationStore(root)
                    key = store.list()[0][0]
                    self.assertEqual([m['role'] for m in store.read(key)], ['user', 'assistant'])
                    from lg_cli.session_runtime import SessionRuntime
                    thread = SessionRuntime(root, key).read()['thread_id']
                    backend = Backend(config)
                    try:
                        opened = await backend.dispatch('session/open', {'sessionId': key})
                        self.assertEqual(opened['thread_id'], thread)
                        snapshot = await backend.dispatch('session/snapshot', {'sessionId': key})
                        self.assertEqual([i['kind'] for i in snapshot['items'] if i['kind']!='notice'], ['user', 'assistant'])
                        self.assertEqual(SessionRuntime(root,key).read()['task']['definition']['kind'],'discussion')
                        self.assertIn('CLI_SHARED_THREAD_MARKER', snapshot['items'][0]['text'])
                        reply = await backend.dispatch('run/start', {'sessionId': key,
                            'text': '继续；只简短回答，不使用工具。'})
                        await backend.tasks[reply['runId']][1]
                        final = backend.journal.read(key)[-1]
                        self.assertEqual(final['params']['status'], 'completed')
                        self.assertEqual(backend.session(key)['agent'].thread_id, thread)
                        self.assertIn('CLI_SHARED_THREAD_MARKER', json.dumps(payloads[-1], ensure_ascii=False))
                        self.assertEqual([m['role'] for m in store.read(key)], ['user', 'assistant', 'user', 'assistant'])
                    finally:
                        await backend.close()
                    return 0
                return asyncio.run(exercise())
            with patch('lg_cli.main.LiteraryInput', return_value=session), \
                 patch.object(session, 'run', run), \
                 patch('lg_cli.main.load_config', return_value=config), \
                 patch('lg_cli.live_agent.provider_bridge', side_effect=lambda _: AnthropicBridge(config, stream_factory=response)):
                self.assertEqual(interactive_loop(config), 0)
