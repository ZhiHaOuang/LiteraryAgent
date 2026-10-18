"""Freeze paged history without models, lost Unicode, or mixed revisions."""
import asyncio
import base64
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from lg_cli.backend import Backend
from lg_cli.backend_history import PAGE_BYTES, SnapshotPages
from lg_cli.project_store import ProjectStore
from tests.helpers import make_config


class BackendHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ProjectStore(self.root).initialize(name='长历史')
        def unavailable(*args, **kwargs):
            self.fail('Reading history must not create a Runtime')
        self.backend = Backend(make_config(self.root), agent_factory=unavailable)
        self.key = self.backend.store.create()

    async def asyncTearDown(self):
        await self.backend.close()
        self.tmp.cleanup()

    async def collect(self, first):
        page, chunks = first, []
        while True:
            chunks.append(base64.b64decode(page['data'], validate=True))
            self.assertLess(len(json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': page}).encode()), 512 * 1024)
            if page['nextPage'] is None:
                break
            page = await self.backend.dispatch('session/snapshotPage', {'sessionId': self.key,
                'snapshotId': first['snapshotId'], 'page': page['nextPage']})
            self.assertEqual((page['cursor'], page['sha256']), (first['cursor'], first['sha256']))
        data = b''.join(chunks)
        self.assertEqual(hashlib.sha256(data).hexdigest(), first['sha256'])
        return json.loads(data)

    async def test_single_item_larger_than_transport_limit_is_restored_exactly(self):
        original = '中文正文。🖋\n"引号与反斜杠\\"' * 110000
        self.backend.store.append(self.key, 'assistant', original)
        legacy = await self.backend.dispatch('session/snapshot', {'sessionId': self.key})
        self.assertGreater(len(json.dumps(legacy, ensure_ascii=False).encode()), 4 * 1024 * 1024)
        first = await self.backend.dispatch('session/snapshotPage', {'sessionId': self.key})
        self.assertGreater(first['pages'], 1)
        restored = await self.collect(first)
        self.assertEqual(restored, legacy)
        self.assertEqual(restored['items'][0]['text'], original)
        self.assertEqual(self.backend.sessions, {})

    async def test_later_pages_keep_original_snapshot_after_new_messages_and_events(self):
        self.backend.store.append(self.key, 'user', '旧历史' * PAGE_BYTES)
        first = await self.backend.dispatch('session/snapshotPage', {'sessionId': self.key})
        self.backend.store.append(self.key, 'assistant', '读取分页期间的新消息')
        self.backend.emit(self.key, 'client/event', {'type': 'run.started', 'sessionId': self.key,
            'runId': 'new-run', 'text': '读取期间的新任务'})
        old = await self.collect(first)
        self.assertEqual(len(old['items']), 1)
        self.assertEqual(old['cursor'], 0)
        fresh = await self.backend.dispatch('session/snapshotPage', {'sessionId': self.key})
        self.assertNotEqual(fresh['snapshotId'], first['snapshotId'])
        current = await self.collect(fresh)
        self.assertGreater(current['cursor'], old['cursor'])
        self.assertIn('读取分页期间的新消息', [i['text'] for i in current['items']])

    async def test_frozen_snapshot_preserves_all_active_run_owners(self):
        for run in ('workflow','agent'):
            self.backend.emit(self.key,'client/event',{'type':'run.started','sessionId':self.key,'runId':run,'text':run})
        first = await self.backend.dispatch('session/snapshotPage',{'sessionId':self.key})
        self.backend.emit(self.key,'client/event',{'type':'run.finished','sessionId':self.key,'runId':'agent','status':'completed'})
        frozen = await self.collect(first)
        self.assertEqual([run['id'] for run in frozen['activeRuns']],['workflow','agent'])
        current = await self.backend.dispatch('session/snapshot',{'sessionId':self.key})
        self.assertEqual(current['activeRuns'],[{'id':'workflow','state':'running'}])
        self.assertGreater(current['cursor'],frozen['cursor'])

    async def test_expired_wrong_session_and_invalid_page_requests_fail_explicitly(self):
        now = [0.0]
        self.backend.history = SnapshotPages(clock=lambda: now[0])
        first = await self.backend.dispatch('session/snapshotPage', {'sessionId': self.key})
        foreign = self.backend.store.create()
        with self.assertRaisesRegex(ValueError, 'another session'):
            await self.backend.dispatch('session/snapshotPage', {'sessionId': foreign, 'snapshotId': first['snapshotId']})
        with self.assertRaisesRegex(ValueError, 'page index'):
            await self.backend.dispatch('session/snapshotPage', {'sessionId': self.key, 'snapshotId': first['snapshotId'], 'page': 99})
        with self.assertRaisesRegex(ValueError, 'start at zero'):
            await self.backend.dispatch('session/snapshotPage', {'sessionId': self.key, 'page': 1})
        now[0] = 181
        with self.assertRaisesRegex(ValueError, 'expired'):
            await self.backend.dispatch('session/snapshotPage', {'sessionId': self.key, 'snapshotId': first['snapshotId']})

    async def test_cache_eviction_is_explicit_and_old_api_is_preserved(self):
        self.backend.store.append(self.key, 'user', '原有接口')
        legacy = await self.backend.dispatch('session/snapshot', {'sessionId': self.key})
        first = await self.backend.dispatch('session/snapshotPage', {'sessionId': self.key})
        self.assertEqual(await self.collect(first), legacy)
        for _ in range(4):
            await self.backend.dispatch('session/snapshotPage', {'sessionId': self.key})
        with self.assertRaisesRegex(ValueError, 'expired'):
            await self.backend.dispatch('session/snapshotPage', {'sessionId': self.key, 'snapshotId': first['snapshotId']})
        self.assertEqual(self.backend.store.read(self.key)[0]['text'], '原有接口')

    async def test_collection_rejects_messages_changed_during_read(self):
        from lg_cli.conversation_store import ConversationStore
        original = ConversationStore.read
        def concurrent_change(store, key):
            messages = original(store, key)
            store.append(key, 'assistant', '并发保存')
            return messages
        with patch.object(ConversationStore, 'read', concurrent_change):
            with self.assertRaisesRegex(ValueError, 'changed while reading'):
                await self.backend.dispatch('session/snapshotPage', {'sessionId': self.key})
        self.assertEqual(len(self.backend.history.snapshots), 0)

    async def test_snapshot_preparation_keeps_rpc_loop_available(self):
        from lg_cli.backend_history import prepare_snapshot
        loop = asyncio.get_running_loop()
        started, release = asyncio.Event(), threading.Event()
        def slow_prepare(*args):
            loop.call_soon_threadsafe(started.set)
            if not release.wait(5):
                raise TimeoutError('Snapshot worker was not released')
            return prepare_snapshot(*args)
        with patch('lg_cli.backend.prepare_snapshot', slow_prepare):
            task = asyncio.create_task(self.backend.dispatch('session/snapshotPage', {'sessionId': self.key}))
            try:
                await asyncio.wait_for(started.wait(), 2)
                response = await asyncio.wait_for(self.backend.dispatch('initialize', {}), 1)
                self.assertIn('pagedSnapshots', response['capabilities'])
                self.assertFalse(task.done())
            finally:
                release.set()
                await task

    async def test_snapshot_reader_does_not_lock_event_writer(self):
        from lg_cli.backend_events import snapshot_items
        loop = asyncio.get_running_loop()
        started, release = asyncio.Event(), threading.Event()
        def slow_items(*args):
            loop.call_soon_threadsafe(started.set)
            if not release.wait(5):
                raise TimeoutError('Snapshot reader was not released')
            return snapshot_items(*args)
        with patch('lg_cli.backend_history.snapshot_items', slow_items):
            task = asyncio.create_task(self.backend.dispatch('session/snapshotPage', {'sessionId': self.key}))
            try:
                await asyncio.wait_for(started.wait(), 2)
                self.backend.journal.db.execute('PRAGMA busy_timeout=0')
                self.backend.emit(self.key, 'client/event', {'type': 'run.started', 'sessionId': self.key,
                    'runId': 'new-run', 'text': '读取期间的新任务'})
                self.assertFalse(release.is_set(), 'Writer must not wait for snapshot reconstruction')
            finally:
                release.set()
                with self.assertRaisesRegex(ValueError, 'changed while reading'):
                    await task
