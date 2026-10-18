"""Real Runtime reads share source-impact evidence with CLI and Backend views."""
import asyncio
import io
import ipaddress
import json
import os
import socket
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from lg_cli.backend import Backend
from lg_cli.book_analysis import BookAnalysisStore, fingerprint
from lg_cli.init_project import init_workspace
from lg_cli.main import main
from lg_cli.project_store import ProjectStore
from lg_cli.supervision import configure_supervision
from tests.helpers import make_config
from tests.test_anthropic_bridge import message_events
from tests.test_decision_review_runtime import objects
from tests.test_live_generation_sources import canonical_sha


class SourceImpactRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def journey(self, engine):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.enterContext(patch.dict(os.environ, {'LITERARYGIANT_REGISTRY_HOME':str(root/'registry')}))
        self.enterContext(patch('lg_cli.credentials.read_profiles', return_value={'active':None, 'profiles':{}}))
        init_workspace(root)
        saved = ProjectStore(root)
        original = saved.create_document(kind='chapter', slug='one', title='原稿', sequence=1,
            content='前文保持。\n\n作者已采纳的原稿。\n\n后文保持。', state='accepted')
        other = saved.create_document(kind='chapter', slug='two', title='无关章节', sequence=2,
            content='无关章节保持原样。', state='accepted')
        saved.set_fact(category='world', key='gate', value='守卫必须核验通行证。')
        configure_supervision(saved)
        decision = saved.save_decision_options(slug='gate-choice', title='城门方向',
            topic={'scope':'work', 'document_id':None, 'key':'gate'}, sources=[], request='比较三种城门方向。',
            options=[{'id':key, 'title':key, 'content':content} for key, content in (
                ('A','CHOICE_A：先核验通行证。'), ('B','CHOICE_B：等待领队。'), ('C','CHOICE_C：询问登记官。'))],
            expected_version_id=0)
        selected_a = saved.select_decision_option(decision['slug'], options_version_number=1,
            option_id='A', expected_version_id=decision['version_id'])
        phase = {'name':'', 'step':0}
        observed, results, errors = {}, {}, []
        serial = 0
        candidate_text = '前文保持。\n\n来源影响测试候选：林砚先核验通行证。\n\n后文保持。'

        def result(payload, predicate):
            return next(value for value in reversed(list(objects(payload['messages']))) if predicate(value))

        def last_tool_result(payload):
            parts=[part for message in payload['messages'] if isinstance(message.get('content'),list)
                for part in message['content'] if isinstance(part,dict) and part.get('type')=='tool_result']
            self.assertTrue(parts)
            return parts[-1]

        def edit_arguments():
            return {'slug':'one', 'content':candidate_text, 'expected_version_id':original.active_version_id,
                'reason':'作者仅请求候选', 'book_overview':'林砚在城门核验线索，追查失踪案并逐步发现阴谋。'}

        def response(payload):
            nonlocal serial
            name, step = phase['name'], phase['step']
            phase['step'] += 1
            results.setdefault(name, []).extend(objects(payload.get('messages', [])))
            impact = False
            if name=='generate' and step==0:
                marker, arguments = 'Read shared project progress for the author', {}
            elif name=='generate' and step==1:
                observed['memory'] = result(payload, lambda value: {'creative_decisions','facts','documents','view'} <= value.keys())
                marker, arguments = 'Save an Agent-generated prose revision', edit_arguments()
            elif name=='query' and step in {0,1,2}:
                if step==1:
                    observed['candidate'] = result(payload, lambda value: value.get('schema')=='lg.source-impact.v1'
                        and value['subject']['document_id']==original.id)
                if step==2:
                    observed['direction'] = result(payload, lambda value: value.get('schema')=='lg.source-impact.v1'
                        and value['subject']['document_id']==decision['document_id'])
                impact = True
                arguments = {'slug':('one','gate-choice','two')[step], 'limit':100,
                    'version_number':candidate.version_number if step==0 else None}
            elif name=='query' and step==3:
                observed['index-only'] = result(payload, lambda value: value.get('schema')=='lg.source-impact.v1'
                    and value['subject']['document_id']==other.id)
                marker, arguments = 'Read the exact source snapshot', {'slug':'one','version_number':candidate.version_number}
            elif name=='query' and step==4:
                observed['review'] = result(payload, lambda value: {'snapshot','sourceFingerprint','generationSourceReview'} <= value.keys())
                marker, arguments = 'Save an Agent-generated prose revision', edit_arguments()
            elif (name=='query' and step==5) or (name=='adopt' and step==0):
                if name=='query':
                    observed['edit-denied']=last_tool_result(payload)
                marker = 'Explicitly adopt a reviewed candidate once'
                arguments = {'slug':'one','version_number':candidate.version_number,
                    'expected_version_id':original.active_version_id}
            else:
                if name=='query':
                    observed['accept-denied']=last_tool_result(payload)
                return list(message_events(delta={'type':'text_delta','text':'仅报告已知直接输入变化；未知覆盖不等于没有影响。采用仍需作者另行决定。'}))
            if impact:
                tools = [tool for tool in payload['tools']
                    if set(tool.get('input_schema',{}).get('properties',{}))=={'slug','version_number','limit'}]
                self.assertEqual(len(tools), 1, 'Resolve the real source-impact MCP schema, including encoded Core aliases')
                tool = tools[0]
            else:
                tool = next(tool for tool in payload['tools'] if marker in tool.get('description',''))
            serial += 1
            return list(message_events(block={'type':'tool_use','id':'impact-'+str(serial),'name':tool['name'],'input':{}},
                delta={'type':'input_json_delta','partial_json':json.dumps(arguments,ensure_ascii=False)},stop='tool_use'))

        class Provider(BaseHTTPRequestHandler):
            def log_message(self,*_):
                pass

            def do_POST(handler):
                try:
                    self.assertEqual(handler.path,'/v1/messages')
                    payload=json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))
                    events=response(payload)
                    handler.send_response(200)
                    handler.send_header('Content-Type','text/event-stream')
                    handler.end_headers()
                    for event in events:
                        if event['type']=='message_start':
                            event['message'].update(id='local-message',type='message',role='assistant',content=[],
                                model=payload['model'],stop_reason=None,stop_sequence=None)
                        handler.wfile.write(('event: '+event['type']+'\ndata: '+json.dumps(event,ensure_ascii=False)+'\n\n').encode())
                    handler.wfile.flush()
                except Exception as exc:
                    errors.append(type(exc).__name__+': '+str(exc))
                    try:
                        handler.send_error(500,'Synthetic response contract failed')
                    except (BrokenPipeError,ConnectionResetError):
                        pass

        original_connect=socket.socket.connect
        def local_connect(connection,address):
            if connection.family in {socket.AF_INET,socket.AF_INET6} and not ipaddress.ip_address(address[0]).is_loopback:
                raise AssertionError('Source impact tests only permit loopback model sockets')
            return original_connect(connection,address)

        self.enterContext(patch.object(socket.socket,'connect',local_connect))
        server=ThreadingHTTPServer(('127.0.0.1',0),Provider)
        worker=threading.Thread(target=server.serve_forever,daemon=True)
        worker.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),worker.join(timeout=5)))
        config=replace(make_config(root),runtime_engine=engine,provider='stepfun',protocol='anthropic',
            api_key='synthetic-local-only',default_model='source-impact-'+engine,
            environment='source-impact-fixture-no-profile',timeout_seconds=60,enable_reference=False,
            base_url=f'http://127.0.0.1:{server.server_port}',
            runtime_manifest=Path(os.environ['LG_TEST_RUNTIME_MANIFEST']) if engine=='codex' else None)
        events=[]
        backend=Backend(config,events.append)
        self.addAsyncCleanup(backend.close)
        key=(await backend.dispatch('session/create',{}))['sessionId']
        backend.session(key)['agent'].runtime.update(mode='full_access',creative_mode='ask')

        def story_state():
            documents=saved.list_documents(limit=None)
            return {'documents':[asdict(document) for document in documents],
                'versions':{document.id:[asdict(version) for version in saved.list_versions(document.id)] for document in documents},
                'facts':[asdict(fact) for fact in saved.list_facts(limit=None)],
                'choices':saved.list_decision_snapshots(limit=None)}

        async def run(name):
            phase.update(name=name,step=0)
            definition=({'kind':'draft','document':'one','sourceVersion':original.active_version_id} if name=='generate'
                else {'kind':'discussion'} if name=='query' else {'kind':'adopt','document':'one',
                    'sourceVersion':original.active_version_id,'versionNumber':candidate.version_number})
            return await backend.run(key,'只执行本轮作者指定操作；读取不授予写入或采用权限。',task=definition)

        async def finish(started):
            await asyncio.wait_for(backend.tasks[started['runId']][1],60)
            complete=next(event['params'] for event in reversed(events)
                if event['method']=='run/completed' and event['params']['runId']==started['runId'])
            self.assertEqual(complete['status'],'completed',complete.get('error'))
            self.assertEqual(errors,[])

        await finish(await run('generate'))
        candidates=[version for version in saved.list_versions('one') if version.content==candidate_text]
        self.assertEqual(len(candidates),1)
        candidate=candidates[0]
        self.assertEqual(candidate.state,'candidate')
        memory=observed['memory']
        self.assertEqual(memory['creative_decisions'][0]['selection']['option_id'],'A')
        observation=next(item for item in candidate.metadata['generation_sources']['observations'] if item['tool']=='project_memory')
        self.assertEqual(observation['payload_sha256'],canonical_sha(memory))
        saved.select_decision_option(decision['slug'],options_version_number=1,option_id='B',expected_version_id=selected_a['version_id'])
        before=story_state()
        review=saved.generation_source_review('one',candidate.version_number)
        self.assertEqual(review['schema'],'lg.source-review.v1')
        self.assertEqual(review['receipt_status'],'recorded')
        self.assertEqual(review['status'],'needs_review')
        change=next(item for item in review['items'] if item['ref']['kind']=='creative_decision')
        self.assertEqual(change['status'],'changed')
        self.assertEqual(change['ref']['option_id'],'A')
        self.assertGreaterEqual(review['summary']['index_only'],2)
        expected_candidate=saved.source_impact('one',version_number=candidate.version_number)
        expected_direction=saved.source_impact('gate-choice')
        expected_index=saved.source_impact('two')
        dependent=next(item for item in expected_direction['direct_dependents']['items'] if item['version_id']==candidate.id)
        self.assertEqual((dependent['document_id'],dependent['version_number'],dependent['state']),
            (original.id,candidate.version_number,'candidate'))
        self.assertEqual(dependent['relation'],'captured_input')
        self.assertFalse(dependent['is_active'])
        self.assertFalse(any(item['version_id']==candidate.id for item in expected_index['direct_dependents']['items']))
        self.assertGreater(expected_index['direct_dependents']['scan']['index_only_refs'],0)
        self.assertGreater(expected_direction['direct_dependents']['scan']['unknown_versions'],0)
        self.assertFalse(expected_direction['semantic_impact_proven'])
        self.assertEqual(expected_direction['coverage'],'partial')
        self.assertEqual(expected_direction['purpose'],'author_global')
        self.assertTrue(expected_direction['unknown_reasons'])

        queried=await run('query')
        await finish(queried)
        self.assertEqual(observed['candidate'],expected_candidate)
        self.assertEqual(observed['direction'],expected_direction)
        self.assertEqual(observed['index-only'],expected_index)
        context=observed['review']
        self.assertEqual(context['generationSourceReview'],review)
        self.assertEqual(context['sourceFingerprint'],fingerprint(context['snapshot']))
        self.assertEqual(context['snapshot'],json.loads(json.dumps(BookAnalysisStore(saved).candidate_snapshot('one',candidate))))
        self.assertFalse(any(event['method']=='approval/requested' and event['params'].get('runId')==queried['runId'] for event in events))
        self.assertIn('outside the author task scope',json.dumps(results['query']))
        for name in ('edit-denied','accept-denied'):
            self.assertIn('outside the author task scope',json.dumps(observed[name]))
        self.assertEqual(story_state(),before)

        backend_value=await backend.dispatch('studio/call',{'operation':'source-impact',
            'arguments':{'slug':'gate-choice','version_number':None,'limit':100}})
        self.assertEqual(backend_value,expected_direction)
        detail=await backend.dispatch('library/read',{'topic':'pending','key':f'candidate:one:{candidate.version_number}'})
        self.assertIn('## 生成时捕获的输入',detail['body'])
        self.assertIn('## 已捕获来源的当前复核',detail['body'])
        self.assertIn('存在已捕获来源变化或不可用，待作者复核；不自动改写、撤销或否定候选。',detail['body'])
        self.assertIn('已变化 '+str(review['summary']['changed']),detail['body'])
        self.assertIn('仅目录索引 '+str(review['summary']['index_only']),detail['body'])
        self.assertEqual(detail['actions'],['review','adopt'])

        source=root/'source-impact-cli-input.json'
        source.write_text(json.dumps({'slug':'gate-choice','version_number':None,'limit':100}))
        def cli():
            stdout,stderr=io.StringIO(),io.StringIO()
            with patch('lg_cli.main.load_config',return_value=config),redirect_stdout(stdout),redirect_stderr(stderr):
                code=main(['-C',str(root),'studio','source-impact','--input',str(source),'--json'])
            self.assertEqual(code,0,stderr.getvalue()+stdout.getvalue())
            return json.loads(stdout.getvalue())
        self.assertEqual(await asyncio.to_thread(cli),expected_direction)
        self.assertEqual(story_state(),before)

        adoption=await run('adopt')
        for _ in range(600):
            if backend.approvals.pending or backend.tasks[adoption['runId']][1].done():
                break
            await asyncio.sleep(.02)
        self.assertTrue(backend.approvals.pending,'Source-impact reads never grant creative adoption')
        identity=next(iter(backend.approvals.pending))
        shown=next(event['params'] for event in reversed(events)
            if event['method']=='approval/requested' and event['params']['approvalId']==identity)
        self.assertEqual(shown['kind'],'creative')
        self.assertEqual(shown['details']['preview']['version_id'],candidate.id)
        await backend.dispatch('approval/resolve',{'approvalId':identity,'result':{'decision':'decline'}})
        await finish(adoption)
        self.assertEqual(story_state(),before)
        self.assertEqual(saved.get_document('one'),original)
        self.assertEqual(saved.get_document('two'),other)
        current=backend.session(key)['agent'].runtime.read()
        self.assertEqual(current['engine'],engine)
        self.assertEqual((current['mode'],current['creative_mode']),('full_access','ask'))
        checkpoint=root/'.literarygiant'/'runtime'/'native'/(key+'.json')
        core_history=any((root/'.literarygiant'/'codex-home'/'sessions').rglob('*.jsonl'))
        if engine=='native':
            native=json.loads(checkpoint.read_text())
            self.assertEqual(native['schema'],'lg.native-runtime.v1')
            self.assertEqual(native['thread'],current['thread_id'])
            self.assertFalse(core_history)
        else:
            self.assertTrue(core_history)
            self.assertFalse(checkpoint.exists())

    async def test_native_source_impact_tool_cli_and_backend_share_readonly_kernel(self):
        await self.journey('native')

    @unittest.skipUnless(os.environ.get('LG_TEST_RUNTIME_MANIFEST'),'set LG_TEST_RUNTIME_MANIFEST for pinned Core')
    async def test_codex_source_impact_tool_cli_and_backend_share_readonly_kernel(self):
        await self.journey('codex')
