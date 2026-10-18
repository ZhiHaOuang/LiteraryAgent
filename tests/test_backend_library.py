"""Read existing literary assets without starting a model or adopting content."""
import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import jsonschema

from lg_cli.backend import Backend
from lg_cli.project_store import ProjectStore
from lg_cli.workspace_library import library
from tests.helpers import make_config


class BackendLibraryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ProjectStore(self.root)
        self.store.initialize(name='统一作品资料')
        self.chapter = self.store.create_document(kind='chapter', slug='one', title='原稿',
            content='作者已采纳的原稿。', state='accepted')
        self.candidate = self.store.create_version('one', content='未采用的候选。',
            expected_version_id=self.chapter.active_version_id)
        self.config = replace(make_config(self.root), api_key=None)
        def unavailable(*args, **kwargs):
            self.fail('Reading literary assets must not construct an Agent Runtime.')
        self.backend = Backend(self.config, agent_factory=unavailable)

    async def asyncTearDown(self):
        await self.backend.close()
        self.tmp.cleanup()

    async def test_book_and_library_are_available_without_model_credentials(self):
        initialized = await self.backend.dispatch('initialize', {})
        self.assertEqual(initialized['protocolVersion'], 'lg.backend.v1')
        self.assertEqual(initialized['workspace'], str(self.root))
        self.assertIn('bookLibrary', initialized['capabilities'])
        dashboard = await self.backend.dispatch('book/dashboard', {})
        self.assertEqual(dashboard['name'], '统一作品资料')
        result = await self.backend.dispatch('library/list', {'topic': 'chapters'})
        self.assertEqual(result, library(self.config, 'chapters'))
        detail = await self.backend.dispatch('library/read', {
            'topic': 'chapters', 'key': result['entries'][0]['key']})
        self.assertIn('作者已采纳的原稿。', detail['body'])
        self.assertEqual(self.store.get_document('one'), self.chapter)
        self.assertEqual(self.backend.store.list(), [])

    async def test_adoption_context_only_prepares_and_rejects_stale_preview(self):
        before = self.store.summary()
        pending = await self.backend.dispatch('library/list', {'topic': 'pending'})
        key = next(e['key'] for e in pending['entries'] if e['status'] == 'candidate')
        request = {'topic': 'pending', 'key': key, 'action': 'adopt', 'fingerprint': pending['fingerprint']}
        prepared = await self.backend.dispatch('library/actionContext', request)
        self.assertIn('studio_accept', prepared['prompt'])
        self.assertEqual(prepared['fingerprint'], pending['fingerprint'])
        self.assertEqual(self.store.summary(), before)
        self.assertEqual(self.store.get_version('one', self.candidate.version_number).state, 'candidate')
        self.assertFalse(self.backend.tasks)
        self.store.edit_active_document('one', content='作者已修改。',
            expected_version_id=self.chapter.active_version_id, reason='作者保存')
        with self.assertRaisesRegex(ValueError, 'changed'):
            await self.backend.dispatch('library/actionContext', request)
        self.assertEqual(self.store.get_document('one').content, '作者已修改。')

    async def test_request_bounds_and_cross_project_keys_are_rejected(self):
        for method, params in (
            ('library/list', {'topic': 'credentials'}),
            ('library/list', {'topic': 'chapters', 'query': 'x' * 121}),
            ('library/list', {'topic': 'chapters', 'offset': -1}),
            ('library/read', {'topic': 'chapters', 'key': '../another-book'}),
            ('library/actionContext', {'topic': 'pending', 'key': 'candidate:one:2',
                'action': 'adopt', 'fingerprint': 'invalid'}),
            ('book/dashboard', {'workspace': '/another-book'}),
        ):
            with self.subTest(method=method, params=params):
                with self.assertRaises((ValueError, jsonschema.ValidationError)):
                    await self.backend.dispatch(method, params)
        self.assertEqual(self.store.get_document('one'), self.chapter)

    async def test_collection_generation_context_preserves_existing_ui_action(self):
        page = await self.backend.dispatch('library/list', {'topic': 'world'})
        prepared = await self.backend.dispatch('library/actionContext', {
            'topic': 'world', 'key': '', 'action': 'generate', 'fingerprint': page['fingerprint']})
        self.assertIn('studio_reference', prepared['prompt'])
        self.assertEqual(self.store.get_document('one'), self.chapter)
        self.assertFalse(self.backend.tasks)

    async def test_slow_library_read_does_not_block_other_rpc_requests(self):
        started = asyncio.Event()
        finished = threading.Event()
        loop = asyncio.get_running_loop()
        def slow_read(*args):
            loop.call_soon_threadsafe(started.set)
            if not finished.wait(5):
                raise TimeoutError('Test reader was not released')
            return {'entries': []}
        with patch('lg_cli.workspace_library.library', slow_read):
            pending = asyncio.create_task(self.backend.dispatch('library/list', {'topic': 'chapters'}))
            try:
                await asyncio.wait_for(started.wait(), 2)
                reply = await asyncio.wait_for(self.backend.dispatch('initialize', {}), 1)
                self.assertEqual(reply['protocolVersion'], 'lg.backend.v1')
                self.assertFalse(pending.done())
            finally:
                finished.set()
                await pending

    async def test_real_stdio_transport_serves_book_data_without_a_turn(self):
        requests = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'book/dashboard', 'params': {}},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'library/list', 'params': {'topic': 'pending'}},
        ]
        source = Path(__file__).resolve().parents[1]
        process = await asyncio.create_subprocess_exec(
            sys.executable, '-m', 'lg_cli.backend_stdio', '--workspace', str(self.root),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, 'PYTHONPATH': str(source / 'lg-cli')})
        try:
            process.stdin.write(''.join(json.dumps(r) + '\n' for r in requests).encode())
            await process.stdin.drain()
            rows = []
            for _ in requests:
                line = await asyncio.wait_for(process.stdout.readline(), 20)
                self.assertTrue(line, 'Backend closed before replying')
                rows.append(json.loads(line))
            process.stdin.close()
            await asyncio.wait_for(process.wait(), 10)
            self.assertEqual(process.returncode, 0, (await process.stderr.read()).decode())
        finally:
            if process.returncode is None:
                process.terminate()
                await process.wait()
        by_id = {r['id']: r for r in rows}
        self.assertEqual(set(by_id), {1, 2, 3})
        self.assertEqual(by_id[2]['result']['name'], '统一作品资料')
        self.assertTrue(by_id[3]['result']['entries'])
        self.assertFalse(self.backend.tasks)
        self.assertEqual(self.backend.store.list(), [])
