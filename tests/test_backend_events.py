"""Runtime-independent streaming, snapshots and approval lifecycles."""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

from lg_cli.backend import Backend
from lg_cli.project_store import ProjectStore
from tests.helpers import make_config
from tests.test_backend import FakeAgent


class StreamingAgent(FakeAgent):
    async def start(self, **kwargs):
        await super().start(**kwargs)
        self.thread_id = 'thread-' + str(id(self))

    async def run(self, text, output_schema=None):
        self.turn_id = 'runtime-turn'
        p = {'threadId': self.thread_id, 'turnId': self.turn_id}
        self.event('item/started', {**p, 'item': {'id': 'private', 'type': 'reasoning', 'text': 'PRIVATE_REASONING'}})
        self.event('item/completed', {**p, 'threadId': 'child', 'item': {'id': 'child', 'type': 'agentMessage', 'text': 'CHILD_ONLY'}})
        self.event('item/agentMessage/delta', {**p, 'itemId': 'answer', 'delta': '星港的灯亮了。'})
        if text == 'question':
            answer = await self.on_request('item/tool/requestUserInput', {**p,
                'questions': [{'id': 'scope', 'question': '选择范围', 'options': [{'label': '第一章'}]}]})
            assert answer['answers']['scope']['answers'] == ['第一章']
        if text == 'wait':
            await asyncio.Future()
        diff = {'path': 'ReferenceLibrary/drafts/one.md', 'before': '正式正文', 'after': '候选正文', 'applied': False}
        self.event('item/completed', {**p, 'item': {'id': 'tool', 'type': 'mcpToolCall',
            'server': 'lg_writing', 'tool': 'edit_manuscript', 'arguments': {'slug': 'one'},
            'result': {'content': [{'type': 'text', 'text': json.dumps({'artifact': diff['path'], 'diff': diff})}]}}})
        self.event('item/completed', {**p, 'item': {'id': 'answer', 'type': 'agentMessage', 'text': '星港的灯亮了。'}})
        self.turn_id = None
        return {'id': 'runtime-turn', 'status': 'completed'}


class BackendEventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ProjectStore(self.root).initialize(name='展示事件')
        self.records = []
        self.backend = Backend(make_config(self.root), self.records.append, agent_factory=StreamingAgent)
        self.key = (await self.backend.open(self.backend.store.create()))['sessionId']

    async def asyncTearDown(self):
        await self.backend.close()
        self.tmp.cleanup()

    async def finish(self, request):
        await self.backend.tasks[request['runId']][1]

    async def test_events_use_lg_run_id_and_keep_diff_without_private_or_child_text(self):
        request = await self.backend.run(self.key, 'write')
        await self.finish(request)
        events = [r['params'] for r in self.records if r['method'] == 'client/event']
        self.assertEqual(events[0]['type'], 'run.started')
        self.assertEqual(events[-1]['type'], 'run.finished')
        self.assertTrue(any(e['type'] == 'assistant.delta' for e in events))
        serialized = json.dumps(events)
        for forbidden in ('PRIVATE_REASONING', 'CHILD_ONLY', 'runtime-turn'):
            self.assertNotIn(forbidden, serialized)
        self.assertFalse(any(r['method'] == 'engine/event' for r in self.records))
        persisted = self.backend.journal.db.execute('SELECT event FROM events').fetchall()
        for forbidden in ('PRIVATE_REASONING', 'CHILD_ONLY'):
            self.assertNotIn(forbidden, json.dumps(self.records))
            self.assertNotIn(forbidden, json.dumps(persisted))
        self.backend.emit(self.key, 'client/event', {'type': 'item.updated', 'sessionId': self.key,
            'item': {'id': 'tool', 'runId': request['runId'], 'kind': 'tool', 'title': '迟到的运行事件',
                'text': '', 'state': 'running'}})
        snapshot = await self.backend.dispatch('session/snapshot', {'sessionId': self.key})
        self.assertEqual(len(snapshot['items']), 3, 'Author, assistant and tool must not be duplicated')
        self.assertTrue(all(i['runId'] == request['runId'] for i in snapshot['items']))
        tool = next(i for i in snapshot['items'] if i['kind'] == 'tool')
        self.assertEqual(tool['state'], 'completed')
        self.assertFalse(tool['diff']['applied'])
        self.assertEqual(tool['files'], ['ReferenceLibrary/drafts/one.md'])
        await self.backend.close()
        self.backend = Backend(make_config(self.root), self.records.append, agent_factory=StreamingAgent)
        recovered = await self.backend.dispatch('session/snapshot', {'sessionId': self.key})
        self.assertEqual(recovered, snapshot)
        replay = await self.backend.dispatch('events/read', {'sessionId': self.key})
        for forbidden in ('PRIVATE_REASONING', 'CHILD_ONLY'):
            self.assertNotIn(forbidden, json.dumps(replay))

    async def test_old_raw_events_are_omitted_without_stalling_the_cursor(self):
        raw = {'method':'engine/event', 'sessionId':self.key,
               'params':{'method':'item/reasoning/textDelta','params':{'delta':'OLD_PRIVATE'}}}
        with self.backend.journal.db:
            for _ in range(2):
                self.backend.journal.db.execute('INSERT INTO events(session,event) VALUES (?,?)',
                    (self.key, json.dumps(raw)))
        page = await self.backend.dispatch('events/read', {'sessionId':self.key,'limit':1})
        self.assertNotIn('OLD_PRIVATE', json.dumps(page))
        self.assertEqual(page['events'][0]['method'], 'runtime/legacyEventOmitted')
        next_page = await self.backend.dispatch('events/read',
            {'sessionId':self.key,'after':page['events'][-1]['cursor'],'limit':1})
        self.assertGreater(next_page['events'][-1]['cursor'], page['events'][-1]['cursor'])
        self.assertNotIn('OLD_PRIVATE', json.dumps(next_page))
        retained = self.backend.journal.db.execute('SELECT event FROM events').fetchall()
        self.assertIn('OLD_PRIVATE', json.dumps(retained), 'Reading does not rewrite old user history')

    async def test_cancel_then_second_runtime_stream_is_not_lost(self):
        first = await self.backend.run(self.key, 'wait')
        for _ in range(100):
            if self.backend.session(self.key)['agent'].turn_id:
                break
            await asyncio.sleep(.01)
        old_thread = self.backend.session(self.key)['agent'].thread_id
        await self.backend.cancel(self.key)
        await self.finish(first)
        second = await self.backend.run(self.key, 'second')
        await self.finish(second)
        self.assertNotEqual(self.backend.session(self.key)['agent'].thread_id, old_thread)
        snapshot = await self.backend.dispatch('session/snapshot', {'sessionId': self.key})
        answers = [i for i in snapshot['items'] if i['kind'] == 'assistant']
        self.assertEqual([(a['runId'], a['state']) for a in answers],
            [(first['runId'], 'cancelled'), (second['runId'], 'completed')])
        messages = (await self.backend.dispatch('session/read', {'sessionId': self.key}))['messages']
        self.assertEqual([m['role'] for m in messages], ['user', 'assistant', 'user', 'assistant'])

    async def test_question_reply_and_cancel_emit_distinct_input_events(self):
        request = await self.backend.run(self.key, 'question')
        for _ in range(100):
            if self.backend.approvals.pending:
                break
            await asyncio.sleep(.01)
        event = next(r['params'] for r in self.records if r['method'] == 'client/event' and r['params']['type'] == 'userInput.requested')
        key = event['request']['id']
        await self.backend.dispatch('approval/resolve', {'approvalId': key,
            'result': {'answers': {'scope': {'answers': ['第一章']}}}})
        await self.finish(request)
        self.assertTrue(any(r['params'].get('type') == 'userInput.resolved' for r in self.records))
        with self.assertRaisesRegex(ValueError, 'expired'):
            await self.backend.dispatch('approval/resolve', {'approvalId': key, 'result': {'decision': 'accept'}})
        self.records.clear()
        waiting = await self.backend.run(self.key, 'question')
        for _ in range(100):
            if self.backend.approvals.pending:
                break
            await asyncio.sleep(.01)
        await self.backend.cancel(self.key)
        await self.finish(waiting)
        self.assertTrue(any(r['params'].get('type') == 'userInput.resolved' for r in self.records))

    async def test_legacy_cli_messages_are_available_and_unknown_requests_fail_closed(self):
        self.backend.store.append(self.key, 'user', '旧 CLI 会话')
        self.backend.store.append(self.key, 'assistant', '已有答复')
        snapshot = await self.backend.dispatch('session/snapshot', {'sessionId': self.key})
        self.assertEqual([i['text'] for i in snapshot['items']], ['旧 CLI 会话', '已有答复'])
        agent = self.backend.session(self.key)['agent']
        with self.assertRaisesRegex(ValueError, 'Unsupported runtime request'):
            await agent.on_request('unknown/permission', {})
        with self.assertRaisesRegex(ValueError, 'secret'):
            await agent.on_request('item/tool/requestUserInput', {'questions': [{'id': 'key', 'question': 'secret', 'isSecret': True}]})
        self.assertFalse(self.backend.approvals.pending)

    async def test_mixed_cli_and_backend_messages_keep_order(self):
        self.backend.store.append(self.key, 'user', '较早 CLI 消息')
        await self.finish(await self.backend.run(self.key, '中间 Backend 消息'))
        self.backend.store.append(self.key, 'user', '较新 CLI 消息')
        snapshot = await self.backend.dispatch('session/snapshot', {'sessionId': self.key})
        self.assertEqual([i['text'] for i in snapshot['items'] if i['kind'] == 'user'],
            ['较早 CLI 消息', '中间 Backend 消息', '较新 CLI 消息'])

    async def test_real_workflow_process_progress_and_output_survive_snapshot(self):
        control = self.backend.session(self.key)['control']
        async def execute():
            return await control.run([sys.executable, '-c', "print('WORKFLOW_PROGRESS_MARKER')"],
                cwd=self.root, label='测试流程')
        request = self.backend.schedule(self.key, execute, text='执行已指定的测试流程')
        await self.finish(request)
        snapshot = await self.backend.dispatch('session/snapshot', {'sessionId': self.key})
        output = next(i for i in snapshot['items'] if i['title'] == '工作流输出')
        self.assertIn('WORKFLOW_PROGRESS_MARKER', output['text'])
        self.assertEqual(output['state'], 'completed')
        task = next(i for i in snapshot['items'] if i['title'] == '当前命令')
        self.assertIn('已完成', task['text'])
        self.assertEqual(task['state'], 'completed')
