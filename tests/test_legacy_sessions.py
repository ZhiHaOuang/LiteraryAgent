"""Legacy history import preserves dialogue without tools, approvals or old runtime grants."""
import asyncio
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock,patch

from lg_cli.backend import Backend
from lg_cli.conversation_store import ConversationStore
from lg_cli.codex_history import CodexHistoryReader
from lg_cli.legacy_sessions import identifier,imported_id
from lg_cli.project_store import ProjectStore
from tests.helpers import make_config
from tests.test_backend import FakeAgent


class FakeReader:
    threads={}
    requests=[]
    def __init__(self,config,home):self.config,self.home=config,home
    async def __aenter__(self):return self
    async def __aexit__(self,*_):pass
    async def list(self):
        self.requests.append('list')
        return [meta for meta,_ in self.threads.values() if Path(meta['cwd'])==self.config.workspace]
    async def read(self,thread):
        self.requests.append('read')
        return self.threads[thread]


class LegacySessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.root=Path(self.directory.name)
        self.project=ProjectStore(self.root);self.project.initialize(name='Legacy import')
        self.project.create_document(kind='chapter',slug='one',title='Original',content='Original prose.')
        self.home=self.root/'.literarygiant'/'codex-home'
        self.home.mkdir();(self.home/'state_5.sqlite').touch()
        self.config=replace(make_config(self.root,api_key=None),runtime_engine='native',environment='legacy-test-no-profile')
        self.thread='01900000-1111-7777-aaaa-bbbbbbbbbbbb'
        self.meta={'id':self.thread,'cwd':str(self.root),'preview':'旧会话','name':'Old named conversation','updatedAt':123}
        self.turns=[{'id':'old-turn','status':'inProgress','items':[
            {'id':'user','type':'userMessage','content':[{'type':'text','text':'只规划，不写正文。'}]},
            {'id':'private','type':'reasoning','text':'PRIVATE_REASONING'},
            {'id':'tool','type':'mcpToolCall','result':{'private':'PRIVATE_TOOL_RESULT'}},
            {'id':'assistant','type':'agentMessage','text':'旧历史中的规划结果。'}]}]
        FakeReader.threads={self.thread:(self.meta,self.turns)};FakeReader.requests=[]
        self.events=[]
        self.backend=Backend(self.config,self.events.append,agent_factory=FakeAgent,legacy_reader_factory=FakeReader)
        self.key=identifier(self.home,self.thread)

    async def asyncTearDown(self):
        await self.backend.close();self.directory.cleanup()

    async def test_list_read_import_and_resume_keep_dialogue_and_reset_permissions(self):
        before=self.project.get_document('one')
        listing=await self.backend.dispatch('session/list',{})
        self.assertEqual(listing['sessions'][0]['sessionId'],self.key)
        snapshot=await self.backend.dispatch('session/snapshot',{'sessionId':self.key})
        self.assertEqual([item['kind'] for item in snapshot['items']],['user','notice','assistant'])
        self.assertEqual(snapshot['activeRuns'],[])
        self.assertNotIn('PRIVATE_',json.dumps(snapshot))
        opened=await self.backend.dispatch('session/open',{'sessionId':self.key})
        target=imported_id(self.key)
        self.assertEqual(opened['sessionId'],target)
        self.assertEqual((opened['mode'],opened['creative_mode'],opened['thread_id']),('ask','ask',None))
        saved=await self.backend.dispatch('session/snapshot',{'sessionId':target})
        self.assertEqual(saved['items'],snapshot['items'])
        self.assertEqual([row['role'] for row in self.backend.store.read(target)],['user','assistant'])
        self.assertEqual(await self.backend.dispatch('session/open',{'sessionId':self.key}),opened)
        self.assertEqual(len(self.backend.store.read(target)),2)
        self.assertEqual(self.project.get_document('one'),before)
        self.assertEqual(set(FakeReader.requests),{'list','read'})
        self.assertEqual(len((await self.backend.dispatch('session/list',{}))['sessions']),1)

    async def test_imported_session_can_continue_without_re_reading_legacy_source(self):
        target=(await self.backend.dispatch('session/open',{'sessionId':self.key}))['sessionId']
        with patch.object(FakeReader,'read',side_effect=AssertionError('Re-read legacy runtime')):
            response=await self.backend.dispatch('run/start',{'sessionId':target,'text':'继续整理规划。'})
            await self.backend.tasks[response['runId']][1]
            messages=self.backend.store.read(target)
            self.assertEqual([row['role'] for row in messages],['user','assistant','user','assistant'])
            snapshot=await self.backend.dispatch('session/snapshot',{'sessionId':target})
            self.assertEqual(snapshot['items'][0]['text'],'只规划，不写正文。')
            self.assertEqual(snapshot['items'][-1]['kind'],'assistant')

    async def test_real_native_http_continuation_imports_context_without_codex_or_old_grants(self):
        await self.backend.close()
        requests=[]
        class Model(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                response={'status':'completed','output':[{'type':'message','role':'assistant',
                    'content':[{'type':'output_text','text':'Native 接续的回复。'}]}]}
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                self.wfile.write(('data: '+json.dumps({'type':'response.completed','response':response})+'\n\n').encode())
        server=ThreadingHTTPServer(('127.0.0.1',0),Model)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        config=replace(make_config(self.root),runtime_engine='native',protocol='responses',default_model='synthetic-native',
            base_url=f'http://127.0.0.1:{server.server_port}',runtime_manifest=None,environment='legacy-test-no-profile')
        self.backend=Backend(config,self.events.append,legacy_reader_factory=FakeReader)
        try:
            with patch('lg_cli.live_agent.LiveAgent',side_effect=AssertionError('Must not start Codex')):
                target=(await self.backend.dispatch('session/open',{'sessionId':self.key}))['sessionId']
                self.assertEqual(type(self.backend.session(target)['agent']).__name__,'NativeRuntime')
                with patch.object(FakeReader,'read',side_effect=AssertionError('Must not re-read old history')):
                    run=await self.backend.run(target,'继续讨论规划。')
                    await self.backend.tasks[run['runId']][1]
                    await self.backend.close()
                    self.backend=Backend(config,self.events.append,legacy_reader_factory=FakeReader)
                    await self.backend.open(target)
                    run=await self.backend.run(target,'继续当前讨论。')
                    await self.backend.tasks[run['runId']][1]
            self.assertEqual(len(requests),2)
            payload=json.dumps(requests[0],ensure_ascii=False)
            self.assertIn('只规划，不写正文。',payload)
            self.assertIn('旧历史中的规划结果。',payload)
            self.assertNotIn('PRIVATE_',payload)
            self.assertEqual(len(self.backend.store.read(target)),6)
            self.assertEqual(self.backend.session(target)['agent'].runtime.read()['creative_mode'],'ask')
            self.assertEqual(self.project.get_document('one').content,'Original prose.')
        finally:
            await asyncio.to_thread(server.shutdown);server.server_close();worker.join(timeout=2)

    async def test_projection_tamper_and_runtime_permission_collision_are_rejected(self):
        snapshot=await self.backend.legacy.snapshot(self.key,self.backend.store)
        target=imported_id(self.key)
        path=self.backend.store.path(target)
        path.parent.mkdir(parents=True,exist_ok=True)
        path.with_suffix('.runtime.json').write_text('{"mode":"full_access"}')
        with self.assertRaisesRegex(ValueError,'pre-existing runtime'):
            self.backend.store.import_history(target,snapshot)
        self.assertFalse(path.exists())
        other='imported-safe-target'
        self.backend.store.import_history(other,snapshot)
        sidecar=self.backend.store.path(other).with_suffix('.legacy.json')
        value=json.loads(sidecar.read_text());value['items'][0]['text']='changed'
        sidecar.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'fingerprint'):
            self.backend.store.legacy_items(other)

    async def test_interrupted_import_remains_unpublished_and_can_retry(self):
        snapshot=await self.backend.legacy.snapshot(self.key,self.backend.store)
        target=imported_id(self.key)
        with patch('lg_cli.conversation_store.os.replace',side_effect=OSError('Simulated interruption')):
            with self.assertRaises(OSError):self.backend.store.import_history(target,snapshot)
        self.assertEqual(self.backend.store.list(),[])
        self.backend.store.import_history(target,snapshot)
        self.assertEqual(len(self.backend.store.read(target)),2)

    async def test_redirected_lg_history_home_is_not_scanned(self):
        from lg_cli.legacy_sessions import LegacySessions
        with tempfile.TemporaryDirectory() as raw:
            outside=Path(raw)
            (outside/'codex-home').mkdir();(outside/'codex-home'/'state_5.sqlite').touch()
            book=self.root/'redirected-book';book.mkdir()
            (book/'.literarygiant').symlink_to(outside,target_is_directory=True)
            config=replace(self.config,workspace=book)
            with patch('lg_cli.legacy_sessions.environment_dir',return_value=self.root/'no-env'):
                self.assertEqual(LegacySessions(config).homes(),[])

    async def test_missing_codec_is_visible_without_hiding_current_lg_sessions(self):
        current=await self.backend.dispatch('session/create',{})
        with patch.object(FakeReader,'list',side_effect=ValueError('PRIVATE_ERROR')):
            result=await self.backend.dispatch('session/list',{})
            self.assertEqual(result['sessions'][0]['sessionId'],current['sessionId'])
            self.assertIn('legacyWarning',result)
            self.assertNotIn('PRIVATE_ERROR',json.dumps(result))

    async def test_foreign_cwd_and_child_history_cannot_import(self):
        await self.backend.dispatch('session/list',{})
        for field,value in (('cwd',str(self.root.parent)),('parentThreadId','parent')):
            old=self.meta.get(field)
            self.meta[field]=value
            with self.assertRaises(ValueError):
                await self.backend.dispatch('session/open',{'sessionId':self.key})
            if old is None:self.meta.pop(field)
            else:self.meta[field]=old
        self.assertEqual(self.backend.store.list(),[])

    async def test_large_import_uses_frozen_pages_and_is_idempotent_under_concurrent_open(self):
        self.turns[0]['items'][-1]['text']='中文旧历史🖋'*120000
        opened=await asyncio.gather(self.backend.dispatch('session/open',{'sessionId':self.key}),
            self.backend.dispatch('session/open',{'sessionId':self.key}))
        self.assertEqual(opened[0]['sessionId'],opened[1]['sessionId'])
        self.assertEqual(len(self.backend.store.list()),1)
        result=await self.backend.dispatch('session/snapshotPage',{'sessionId':opened[0]['sessionId']})
        self.assertGreater(result['pages'],1)
        self.assertEqual(len(self.backend.store.read(opened[0]['sessionId'])),2)


class CodecContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_paged_codec_reads_every_full_turn_and_rejects_incomplete_or_repeated_pages(self):
        root=Path('/synthetic-book')
        config=make_config(root,api_key=None)
        meta={'id':'thread','cwd':str(root),'historyMode':'paginated','updatedAt':123}
        first={'id':'one','itemsView':'full','items':[]}
        second={'id':'two','itemsView':'full','items':[]}
        reader=CodexHistoryReader(config,root/'.literarygiant/codex-home')
        reader.request=AsyncMock(side_effect=[{'thread':meta},{'data':[first],'nextCursor':'next'},
            {'data':[second],'nextCursor':None},{'thread':meta}])
        self.assertEqual((await reader.read('thread'))[1],[first,second])
        for bad in ({**first,'itemsView':'summary'},first):
            reader.request=AsyncMock(side_effect=[{'thread':meta},{'data':[first],'nextCursor':'next'},
                {'data':[bad],'nextCursor':None}])
            with self.assertRaisesRegex(ValueError,'incomplete|repeats'):await reader.read('thread')
        reader.request=AsyncMock(side_effect=[{'thread':meta},{'data':[],'nextCursor':None},
            {'thread':{**meta,'updatedAt':124}}])
        with self.assertRaisesRegex(ValueError,'changed while reading'):await reader.read('thread')

    async def test_list_disables_provider_filter_and_only_reads_owned_parent_conversations(self):
        root=Path('/synthetic-book')
        reader=CodexHistoryReader(make_config(root,api_key=None),root/'.literarygiant/codex-home')
        meta={'id':'root','cwd':str(root)}
        reader.request=AsyncMock(return_value={'data':[meta,{**meta,'id':'child','parentThreadId':'root'},
            {**meta,'id':'other','cwd':'/another-book'}],'nextCursor':None})
        self.assertEqual(await reader.list(),[meta])
        self.assertEqual(reader.request.call_args.args[1]['modelProviders'],[])
        with self.assertRaisesRegex(ValueError,'cannot execute'):await CodexHistoryReader.request(reader,'turn/start',{})

    async def test_unpinned_reader_never_discovers_or_launches_a_runtime(self):
        config=replace(make_config(Path('/synthetic-book'),api_key=None),runtime_manifest=None)
        with patch.dict(os.environ,{'LG_CODEX_RUNTIME_MANIFEST':''}),\
             patch('lg_cli.codex_history.discover_core_commands',side_effect=AssertionError('Must not discover arbitrary runtimes')):
            with self.assertRaisesRegex(ValueError,'pinned runtime manifest'):
                await CodexHistoryReader(config,Path('/synthetic-book/.literarygiant/codex-home')).__aenter__()


@unittest.skipUnless(os.environ.get('LG_TEST_RUNTIME_MANIFEST'),'set pinned runtime for compatibility integration')
class LegacyCoreIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_real_extension_format_is_read_without_model_then_imported(self):
        from lg_cli.live_agent import LiveAgent
        from lg_cli.anthropic_bridge import AnthropicBridge
        from tests.test_anthropic_bridge import message_events
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)
            project=ProjectStore(root);project.initialize(name='Actual legacy codec')
            config=replace(make_config(root),provider='stepfun',protocol='anthropic',default_model='step-3.5-flash',
                runtime_manifest=Path(os.environ['LG_TEST_RUNTIME_MANIFEST']))
            requests=[]
            def response(payload):
                requests.append(payload)
                yield from message_events(delta={'type':'text_delta','text':'旧 Extension 的保存回复。'})
            agent=LiveAgent(config,lambda *_:None)
            with patch('lg_cli.live_agent.provider_bridge',side_effect=lambda _:AnthropicBridge(config,stream_factory=response)):
                try:
                    await agent.start()
                    result=await agent.request('thread/start',{'cwd':str(root),'ephemeral':False,'model':config.default_model,
                        'sandbox':'read-only','approvalPolicy':'never','baseInstructions':'Reply briefly without tools.'})
                    thread=result['thread']['id'];agent.thread_id=thread
                    await agent.run('LEGACY_AUTHOR_MARKER；只简短回复，不使用工具。')
                finally:await agent.close()
            count=len(requests)
            reading=replace(config,runtime_engine='native',api_key=None)
            async with CodexHistoryReader(reading,root/'.literarygiant'/'codex-home') as reader:
                self.assertTrue(await reader.list())
            backend=Backend(reading,agent_factory=FakeAgent)
            try:
                listed=await backend.dispatch('session/list',{})
                self.assertNotIn('legacyWarning',listed)
                key=next(item['sessionId'] for item in listed['sessions'] if item['sessionId']==identifier(root/'.literarygiant'/'codex-home',thread))
                opened=await backend.dispatch('session/open',{'sessionId':key})
                snapshot=await backend.dispatch('session/snapshot',{'sessionId':opened['sessionId']})
                self.assertIn('LEGACY_AUTHOR_MARKER',json.dumps(snapshot,ensure_ascii=False))
                self.assertIn('旧 Extension 的保存回复',json.dumps(snapshot,ensure_ascii=False))
                self.assertEqual(len(requests),count)
                self.assertEqual(opened['thread_id'],None)
            finally:await backend.close()
