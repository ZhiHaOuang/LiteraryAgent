"""Opt-in checks of the standalone upstream entry, without changing LG launchers."""
import asyncio
import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from lg_cli.anthropic_bridge import AnthropicBridge
from lg_cli.codex_policy import WritingLaunchPolicy
from lg_cli.core_adapter import _adapter_env
from lg_cli.init_project import init_workspace
from tests.helpers import make_config
from tests.test_anthropic_bridge import message_events


class StdioClient:
    def __init__(self, process):
        self.process = process
        self.sequence = 0
        self.events = []

    async def send(self, value):
        self.process.stdin.write((json.dumps(value) + '\n').encode())
        await self.process.stdin.drain()

    async def receive(self):
        line = await asyncio.wait_for(self.process.stdout.readline(), 30)
        if not line:
            raise AssertionError('Standalone app-server exited before its response')
        message = json.loads(line)
        if 'method' in message:
            if 'id' in message:
                raise AssertionError('Unexpected server authorization request in a text-only probe')
            self.events.append(message)
        return message

    async def request(self, method, params):
        self.sequence += 1
        identity = self.sequence
        await self.send({'id': identity, 'method': method, 'params': params})
        while True:
            message = await self.receive()
            if message.get('id') == identity:
                if 'error' in message:
                    raise AssertionError(f'{method} failed: {message["error"]}')
                return message['result']

    async def turn(self, thread, text):
        start = len(self.events)
        await self.request('turn/start', {'threadId': thread,
            'input': [{'type': 'text', 'text': text, 'text_elements': []}]})
        while not any(e['method'] == 'turn/completed' for e in self.events[start:]):
            await self.receive()
        return next(e['params']['turn'] for e in self.events[start:] if e['method'] == 'turn/completed')

    async def close(self):
        self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), 10)
        except asyncio.TimeoutError:
            self.process.terminate()
            await asyncio.wait_for(self.process.wait(), 10)


@unittest.skipUnless(os.environ.get('LG_TEST_APP_SERVER_BINARY'), 'set LG_TEST_APP_SERVER_BINARY for standalone checks')
class StandaloneRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_entry_validates_policy_streams_and_resumes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            init_workspace(root)
            config = replace(make_config(root), provider='stepfun', protocol='anthropic',
                             default_model='step-3.5-flash', timeout_seconds=45)
            payloads = []

            def response(payload):
                payloads.append(payload)
                yield from message_events(delta={'type': 'text_delta', 'text': 'Standalone runtime ready.'})

            plan = WritingLaunchPolicy().prepare(config, control_socket=None, review_only=True)
            overrides = [arg for key, value in plan.overrides for arg in ('-c', f'{key}={value}')]
            with AnthropicBridge(config, stream_factory=response) as bridge:
                env = _adapter_env(config)
                env['LG_BRIDGE_TOKEN'] = bridge.token
                env['NO_PROXY'] = env['no_proxy'] = '127.0.0.1,localhost,::1'
                command = [os.environ['LG_TEST_APP_SERVER_BINARY'], *bridge.codex_args(),
                           *overrides, '--listen', 'stdio://']

                async def connect():
                    process = await asyncio.create_subprocess_exec(*command, cwd=root, env=env,
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL, limit=4 * 1024 * 1024)
                    client = StdioClient(process)
                    try:
                        await client.request('initialize', {'clientInfo': {'name': 'lg-build-test', 'version': '0.1'},
                            'capabilities': {'experimentalApi': True}})
                        await client.send({'method': 'initialized', 'params': {}})
                        plan.validate(await client.request('config/read', {'cwd': str(root), 'includeLayers': True}))
                    except BaseException:
                        await client.close()
                        raise
                    return client

                params = {'cwd': str(root), 'model': config.default_model, 'approvalPolicy': 'never',
                          'sandbox': 'read-only', 'baseInstructions': 'Reply briefly without using tools.'}
                first = await connect()
                try:
                    thread = (await first.request('thread/start', params))['thread']['id']
                    result = await first.turn(thread, 'Remember STANDALONE_HISTORY_MARKER. Reply without tools.')
                    self.assertEqual(result['status'], 'completed')
                    self.assertTrue(any(e['method'] == 'item/agentMessage/delta' for e in first.events))
                finally:
                    await first.close()
                second = await connect()
                try:
                    resumed = await second.request('thread/resume', {**params, 'threadId': thread})
                    self.assertEqual(resumed['thread']['id'], thread)
                    self.assertEqual((await second.turn(thread, 'Continue without tools.'))['status'], 'completed')
                    self.assertIn('STANDALONE_HISTORY_MARKER', json.dumps(payloads[-1]))
                finally:
                    await second.close()

    async def test_direct_entry_retains_generic_patch_dispatch(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            patch = '*** Begin Patch\n*** Add File: candidate.txt\n+Draft candidate only.\n*** End Patch'
            result = await asyncio.to_thread(subprocess.run,
                [os.environ['LG_TEST_APP_SERVER_BINARY'], '--codex-run-as-apply-patch', patch],
                cwd=root, capture_output=True, text=True, timeout=20, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'candidate.txt').read_text(), 'Draft candidate only.\n')
