"""Native execution uses real LG tools and approvals without any Codex process."""
import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from lg_cli.backend import Backend
from lg_cli.config import load_config, ConfigError
from lg_cli.init_project import init_workspace
from lg_cli.project_store import ProjectStore
from lg_cli.session_runtime import SessionRuntime
from tests.helpers import make_config


def answer(text='Native 回复。'):
    return {'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant',
        'content': [{'type': 'output_text', 'text': text}]}]}


class NativeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        init_workspace(self.root)
        self.project = ProjectStore(self.root)
        self.original = self.project.create_document(kind='chapter', slug='one', title='原稿',
            content='作者已采纳的原稿。', state='accepted')
        self.config = replace(make_config(self.root), runtime_engine='native', protocol='responses',
            default_model='synthetic-model', timeout_seconds=15)
        self.records = []
        self.backend = Backend(self.config, self.records.append)
        self.key = (await self.backend.dispatch('session/create', {}))['sessionId']
        self.no_codex = patch('lg_cli.live_agent.LiveAgent', side_effect=AssertionError('Native must not invoke Codex'))
        self.no_codex.start()

    async def asyncTearDown(self):
        await self.backend.close()
        self.no_codex.stop()
        self.tmp.cleanup()

    async def finish(self, request):
        await self.backend.tasks[request['runId']][1]
        return [r['params'] for r in self.records if r['method'] == 'run/completed'][-1]

    async def test_real_mcp_candidate_approval_preserves_accepted_text_and_diff(self):
        for decision in ('decline', 'accept'):
            calls = []
            async def model(_model, request, delta):
                calls.append(request)
                if len(calls) == 1:
                    return {'status': 'completed', 'output': [{'type': 'function_call', 'call_id': 'edit-' + decision,
                        'name': 'edit_manuscript', 'arguments': json.dumps({'slug': 'one', 'content': 'Native 候选正文。',
                            'expected_version_id': self.original.active_version_id, 'reason': '作者请求局部修改',
                            'book_overview': '维修师在星港查找闸门失灵的原因。'})}]}
                delta('Native 回复。')
                return answer()
            with patch('lg_cli.native_model.NativeModel.generate', model):
                request = await self.backend.dispatch('run/start', {'sessionId': self.key, 'text': '提供改稿候选'})
                for _ in range(500):
                    if self.backend.approvals.pending:
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(self.backend.approvals.pending)
                self.backend.approvals.resolve(next(iter(self.backend.approvals.pending)), {'decision': decision})
                self.assertEqual((await self.finish(request))['status'], 'completed')
            self.assertEqual(self.project.get_document('one').content, '作者已采纳的原稿。')
            self.assertEqual(len(self.project.list_versions('one')), 1 if decision == 'decline' else 2)
        items = (await self.backend.dispatch('session/snapshot', {'sessionId': self.key}))['items']
        self.assertTrue(any(i.get('diff', {}).get('after') == 'Native 候选正文。' for i in items))
        self.assertFalse(self.project.list_facts(state='canonical'))
        self.assertFalse(any(r['method'] == 'engine/event' for r in self.records))

    async def test_session_resume_keeps_native_checkpoint_and_prior_author_request(self):
        seen = []
        async def model(_model, request, delta):
            seen.append(request)
            delta('Native 回复。')
            return answer()
        with patch('lg_cli.native_model.NativeModel.generate', model):
            await self.finish(await self.backend.run(self.key, 'NATIVE_SESSION_MARKER'))
            identity = self.backend.session(self.key)['agent'].thread_id
            await self.backend.close()
            self.backend = Backend(self.config, self.records.append)
            await self.backend.open(self.key)
            await self.finish(await self.backend.run(self.key, '继续当前讨论'))
        self.assertEqual(self.backend.session(self.key)['agent'].thread_id, identity)
        self.assertIn('NATIVE_SESSION_MARKER', json.dumps(seen[-1], ensure_ascii=False))
        self.assertEqual([m['role'] for m in self.backend.store.read(self.key)], ['user', 'assistant', 'user', 'assistant'])

    async def test_invalid_tool_is_rejected_before_any_operation(self):
        bad = {'status': 'completed', 'output': [{'type': 'function_call', 'call_id': 'bad',
            'name': 'shell_command', 'arguments': '{"command":"must not run"}'}]}
        with patch('lg_cli.native_model.NativeModel.generate', AsyncMock(return_value=bad)):
            result = await self.finish(await self.backend.run(self.key, '讨论方案'))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(self.project.list_versions('one')), 1)
        self.assertFalse(self.backend.approvals.pending)

    async def test_cancelled_tool_is_not_replayed_after_restart(self):
        call = {'status': 'completed', 'output': [{'type': 'function_call', 'call_id': 'pending',
            'name': 'update_book_overview', 'arguments': json.dumps({'text': '不得写入的作品概况。'})}]}
        with patch('lg_cli.native_model.NativeModel.generate', AsyncMock(return_value=call)):
            request = await self.backend.run(self.key, '提供概况候选')
            for _ in range(500):
                if self.backend.approvals.pending:
                    break
                await asyncio.sleep(.01)
            self.assertTrue(self.backend.approvals.pending)
            await self.backend.cancel(self.key)
            self.assertEqual((await self.finish(request))['status'], 'cancelled')
        self.assertFalse((self.root / 'ReferenceLibrary/bible/book-overview.json').exists())
        with patch('lg_cli.native_model.NativeModel.generate', side_effect=AssertionError('No replay')):
            result = await self.finish(await self.backend.run(self.key, '继续'))
        self.assertEqual(result['status'], 'failed')
        self.assertIn('unresolved', result['error'])

    async def test_subscription_and_unsupported_protocol_never_fall_back(self):
        for config in (replace(self.config, auth_mode='chatgpt'), replace(self.config, protocol='chat_completions')):
            with self.subTest(protocol=config.protocol, auth=config.auth_mode):
                service = Backend(config)
                try:
                    key = (await service.dispatch('session/create', {}))['sessionId']
                    request = await service.run(key, '只讨论')
                    await service.tasks[request['runId']][1]
                    status = [r for r in service.journal.read(key) if r['method'] == 'run/completed'][-1]['params']['status']
                    self.assertEqual(status, 'failed')
                finally:
                    await service.close()

    async def test_corrupt_checkpoint_is_rejected_before_model_or_tools(self):
        with patch('lg_cli.native_model.NativeModel.generate',AsyncMock(return_value=answer())):
            await self.finish(await self.backend.run(self.key,'保存对话'))
        checkpoint=self.backend.session(self.key)['agent'].checkpoint
        saved=json.loads(checkpoint.read_text())
        call={'type':'function_call','call_id':'uncertain-write','name':'update_book_overview','arguments':'{"text":"pending"}'}
        output={'type':'function_call_output','call_id':'uncertain-write','output':'{}'}
        cases=[([call],[]),([output],[]),([call,call],['uncertain-write']),
            ([call,output,output],[]),([call],['uncertain-write','uncertain-write'])]
        for extra,pending in cases:
            with self.subTest(extra=extra,pending=pending):
                await self.backend.close()
                checkpoint.write_text(json.dumps({**saved,'messages':saved['messages']+extra,'pending':pending}))
                self.backend=Backend(self.config,self.records.append);await self.backend.open(self.key)
                with patch('lg_cli.native_model.NativeModel.generate',side_effect=AssertionError('No replay')), \
                    patch('lg_cli.native_tools.NativeTools.start',side_effect=AssertionError('No tools')):
                    result=await self.finish(await self.backend.run(self.key,'继续'))
                self.assertEqual(result['status'],'failed')
                self.assertIn('checkpoint',result['error'])

    async def test_historical_call_id_cannot_dispatch_a_second_tool(self):
        from mcp.types import CallToolResult
        calls=0
        async def model(_model,request,delta):
            nonlocal calls
            calls+=1
            if calls==2:return answer()
            return {'status':'completed','output':[{'type':'function_call','call_id':'once',
                'name':'update_book_overview','arguments':'{"text":"synthetic"}'}]}
        # Count dispatcher invocations; this fixture does not execute or approve a write.
        with patch('lg_cli.native_model.NativeModel.generate',model), \
            patch('lg_cli.native_tools.NativeTools.call',AsyncMock(return_value=CallToolResult(content=[]))) as tool:
            self.assertEqual((await self.finish(await self.backend.run(self.key,'第一次')))['status'],'completed')
            result=await self.finish(await self.backend.run(self.key,'第二次'))
        self.assertEqual(result['status'],'failed');self.assertIn('identity',result['error'])
        self.assertEqual(tool.await_count,1)

    async def test_engine_bindings_preserve_codex_identity_when_switching_back(self):
        runtime = self.backend.session(self.key)['agent'].runtime
        runtime.update(thread_id='old-codex-thread')
        runtime.select_engine('native')
        self.assertIsNone(runtime.read()['thread_id'])
        runtime.update(thread_id='native-thread')
        runtime.select_engine('codex')
        self.assertEqual(runtime.read()['thread_id'], 'old-codex-thread')
        runtime.select_engine('native')
        self.assertEqual(runtime.read()['thread_id'], 'native-thread')

    async def test_native_auto_review_has_read_tools_only_and_returns_validated_decision(self):
        from lg_cli.creative_review import review_creative
        names = []
        async def model(_model, request, delta):
            names.extend(tool['name'] for tool in request['tools'])
            return answer('{"decision":"decline","reason":"来源不足。"}')
        with patch('lg_cli.native_model.NativeModel.generate', model):
            result = await review_creative(self.config, {'operation': 'confirm unsupported fact'})
        self.assertEqual(result['decision'], 'decline')
        self.assertIn('read_document', names)
        self.assertNotIn('edit_manuscript', names)
        self.assertNotIn('studio_accept', names)


class RuntimeConfigTests(unittest.TestCase):
    def test_engine_is_explicit_and_unknown_engine_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / '.literarygiant').mkdir()
            path = root / '.literarygiant/config.toml'
            path.write_text('[runtime]\nengine="native"\n')
            self.assertEqual(load_config(root, environment='runtime-test').runtime_engine, 'native')
            path.write_text('[runtime]\nengine="unknown"\n')
            with self.assertRaises(ConfigError):
                load_config(root, environment='runtime-test')
