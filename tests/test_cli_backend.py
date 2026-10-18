"""Single-invocation writing commands share the same session service as the UI."""
import io
import json
import os
import sqlite3
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager,redirect_stdout,redirect_stderr
from dataclasses import replace
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import patch,AsyncMock

from lg_cli.main import main
from lg_cli.conversation_store import ConversationStore
from lg_cli.project_store import ProjectStore
from lg_cli.init_project import init_workspace
from lg_cli.session_runtime import SessionRuntime
from lg_cli.run_store import RunStore


@contextmanager
def isolated_cli(root,endpoint=''):
    from lg_cli.config import load_config
    from lg_cli.backend import ProcessHost
    config=load_config(root,environment='cli-backend-test-no-profile')
    if endpoint:
        config=replace(config,provider='openai',protocol='responses',api_key='synthetic-key',
            default_model='synthetic-cli',base_url=endpoint,enable_reference=False,max_stage_context_chars=200000)
    original=ProcessHost.run_command
    async def run(host,argv,**options):
        if endpoint and argv[1:4]==['-u','-m','lg_cli']:
            argv=[sys.executable,'-u',str(Path(__file__).resolve()),endpoint,*argv[4:]]
        return await original(host,argv,**options)
    with patch('lg_cli.main.load_config',return_value=config),patch.object(ProcessHost,'run_command',run):
        yield


class CLIBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.env=patch.dict(os.environ,{'LITERARYGIANT_REGISTRY_HOME':str(self.root/'registry')});self.env.start();self.addCleanup(self.env.stop)

    def invoke(self,*args,stdin=''):
        stdout=io.StringIO();stderr=io.StringIO()
        with isolated_cli(self.root,getattr(self,'provider_endpoint','')),\
                patch('sys.stdin',io.StringIO(stdin)),redirect_stdout(stdout),redirect_stderr(stderr):
            code=main(['-C',str(self.root),'--runtime-engine','native',*args])
        return code,stdout.getvalue(),stderr.getvalue()

    def test_dry_run_uses_backend_saves_once_and_retains_jsonl_result(self):
        code,stdout,stderr=self.invoke('outline','--dry-run','--json','只规划第一卷')
        self.assertEqual(code,0,stderr)
        records=[json.loads(line) for line in stdout.splitlines()]
        self.assertEqual(records[0]['type'],'run.started')
        self.assertEqual((records[-1]['type'],records[-1]['status']),('run.result','planned'))
        store=ConversationStore(self.root);sessions=store.list();self.assertEqual(len(sessions),1)
        rows=store.read(sessions[0][0]);self.assertEqual([row['role'] for row in rows],['user','assistant'])
        self.assertEqual(rows[0]['text'],'只规划第一卷')
        self.assertEqual(rows[1]['text'],records[-1]['text'])
        with sqlite3.connect(self.root/'.literarygiant/client-events.sqlite3') as db:
            events=[json.loads(row[0]) for row in db.execute('SELECT event FROM events ORDER BY id')]
        self.assertTrue(any(e['method']=='run/completed' and e['params']['status']=='completed' for e in events))
        self.assertFalse(ProjectStore(self.root).initialized,'A preview must not create story authority')

    def initialize(self):
        init_workspace(self.root);self.store=ProjectStore(self.root)
        self.original=self.store.create_document(kind='note',slug='one',title='原稿',content='作者原稿。',state='accepted')
        self.candidate=self.store.create_version('one',content='待确认候选。',expected_version_id=self.original.active_version_id)

    def fingerprint(self,stdout):
        records=[json.loads(line) for line in stdout.splitlines()]
        return next(record['preview']['fingerprint'] for record in records if record['type']=='approval.required')

    def test_conversation_is_owned_and_saved_once_across_invocations(self):
        key=ConversationStore(self.root).create()
        for text in ('先规划世界','再规划人物'):
            code,stdout,stderr=self.invoke('--conversation',key,'outline','--dry-run','--json',text)
            self.assertEqual(code,0,stderr)
        records=ConversationStore(self.root).read(key)
        self.assertEqual([row['role'] for row in records],['user','assistant']*2)
        self.assertEqual([row['text'] for row in records if row['role']=='user'],['先规划世界','再规划人物'])
        self.assertEqual(len(ConversationStore(self.root).list()),1)
        runtime=SessionRuntime(self.root,key);runtime.acquire()
        try:
            before=list(records)
            code,stdout,_=self.invoke('--conversation',key,'outline','--dry-run','--json','不应写入历史')
            self.assertEqual(code,1);self.assertIn('in use',stdout)
            self.assertEqual(ConversationStore(self.root).read(key),before)
        finally:runtime.release()

    def test_explicit_chat_restores_history_in_real_worker_and_saves_once(self):
        from tests.test_execution_adapter import specimen
        self.initialize()
        store=ConversationStore(self.root);key=store.create()
        store.append(key,'user','Our hero is Lin, never rename her.')
        store.append(key,'assistant','Lin reaches the tower.')
        seen=self.provider(lambda value:json.dumps(specimen(value['text']['format']['schema']),ensure_ascii=False))
        code,stdout,stderr=self.invoke('--conversation',key,'chat','--json','What happens next?')
        self.assertEqual(code,0,stderr+stdout)
        result=json.loads(stdout.splitlines()[-1])
        self.assertEqual((result['status'],result['session_id']),('completed',key))
        self.assertTrue(seen)
        for value in seen:
            request=json.dumps(value['input'],ensure_ascii=False)
            self.assertIn('never rename her',request)
            self.assertIn('Lin reaches the tower.',request)
            self.assertIn('What happens next?',request)
        rows=store.read(key)
        self.assertEqual([row['role'] for row in rows],['user','assistant']*2)
        self.assertEqual(rows[-2]['text'],'What happens next?')
        self.assertTrue(result['text'])
        self.assertEqual(rows[-1]['text'],result['text'])
        self.assertEqual(rows[-1]['workflow_run'],result['run_id'])
        with sqlite3.connect(self.root/'.literarygiant/client-events.sqlite3') as db:
            events=[json.loads(row[0]) for row in db.execute('SELECT event FROM events ORDER BY id')]
        completed=next(e['params'] for e in events if e['method']=='run/completed')
        self.assertEqual(rows[-1]['run_id'],completed['runId'])
        self.assertEqual(completed['result']['workflowRunId'],result['run_id'])
        self.assertEqual(len(store.list()),1)

    def test_standalone_acceptance_requires_exact_preview_then_saves(self):
        self.initialize();version=str(self.candidate.version_number)
        code,stdout,_=self.invoke('--operation-mode','full_access','version','accept','one',version,'--json')
        self.assertEqual(code,2);fingerprint=self.fingerprint(stdout)
        key=json.loads(stdout)['session_id']
        self.assertEqual(SessionRuntime(self.root,key).read()['mode'],'full_access')
        self.assertEqual(SessionRuntime(self.root,key).read()['creative_mode'],'ask')
        self.assertEqual(self.store.get_document('one').active_version_id,self.original.active_version_id)
        code,stdout,stderr=self.invoke('--approve-fingerprint',fingerprint,'version','accept','one',version,'--json')
        self.assertEqual(code,0,stderr+stdout)
        self.assertEqual(json.loads(stdout)['active_version_id'],self.candidate.id)

    def test_old_fingerprint_fails_even_with_full_creative_access(self):
        self.initialize();key=ConversationStore(self.root).create()
        code,stdout,_=self.invoke('--conversation',key,'version','accept','one',str(self.candidate.version_number),'--json')
        self.assertEqual(code,2);fingerprint=self.fingerprint(stdout)
        self.store.edit_active_document('one',content='作者刚修改的原稿。',expected_version_id=self.original.active_version_id,reason='author')
        runtime=SessionRuntime(self.root,key);runtime.acquire()
        try:runtime.update(creative_mode='full_access')
        finally:runtime.release()
        code,stdout,_=self.invoke('--conversation',key,'--approve-fingerprint',fingerprint,'version','accept','one',str(self.candidate.version_number),'--json')
        self.assertEqual(code,1);self.assertIn('fingerprint changed',stdout)
        self.assertEqual(self.store.get_document('one').content,'作者刚修改的原稿。')

    def test_proposal_idea_confirmation_cannot_reuse_approval_for_canonical(self):
        self.initialize();proposal=self.store.propose_fact(category='world',key='设定',value='能力需要代价。')
        arguments=('bible','proposal','accept',str(proposal.id))
        code,stdout,_=self.invoke(*arguments,'--state','idea','--json')
        self.assertEqual(code,2);fingerprint=self.fingerprint(stdout)
        self.assertEqual(json.loads(stdout)['preview']['state'],'idea')
        code,stdout,_=self.invoke('--approve-fingerprint',fingerprint,*arguments,'--state','canonical','--json')
        self.assertEqual(code,1);self.assertEqual(self.store.list_facts(),[])
        code,stdout,stderr=self.invoke('--approve-fingerprint',fingerprint,*arguments,'--state','idea','--json')
        self.assertEqual(code,0,stderr+stdout);self.assertEqual(json.loads(stdout)['state'],'idea')

    def test_worker_marker_never_bypasses_creative_approval_or_session_ownership(self):
        self.initialize();key=ConversationStore(self.root).create()
        for args in (('--backend-worker','version','accept','one',str(self.candidate.version_number),'--json'),
                ('--backend-worker','--conversation',key,'outline','--dry-run','--json','任务')):
            code,stdout,_=self.invoke(*args)
            self.assertEqual(code,1);self.assertIn('owned worker',stdout)
        self.assertEqual(self.store.get_document('one').active_version_id,self.original.active_version_id)

    def test_exact_proposal_id_stays_confirmable_beyond_the_latest_thousand(self):
        self.initialize()
        proposal=self.store.propose_fact(category='world',key='较早提案',value='保留准确来源。')
        with sqlite3.connect(self.root/'.literarygiant/story.sqlite3') as connection:
            connection.executemany('''INSERT INTO fact_proposals(category,fact_key,value,tags_json,status,rationale,created_at)
                SELECT category,fact_key,value,tags_json,status,rationale,created_at FROM fact_proposals WHERE id=?''',
                [(proposal.id,)]*1000)
        self.assertNotIn(proposal.id,[value.id for value in self.store.list_proposals(limit=1000)])
        args=('bible','proposal','accept',str(proposal.id),'--state','idea','--json')
        code,stdout,stderr=self.invoke(*args)
        self.assertEqual(code,2,stderr+stdout);fingerprint=self.fingerprint(stdout)
        code,stdout,stderr=self.invoke('--approve-fingerprint',fingerprint,*args)
        self.assertEqual(code,0,stderr+stdout)
        self.assertEqual(self.store.get_proposal(proposal.id).status,'accepted')

    def test_confirming_candidate_sourced_proposal_cannot_promote_it_to_canonical(self):
        self.initialize()
        proposal=self.store.propose_fact(category='world',key='尚未采纳的设定',value='只存在于候选稿。',
            source_document=self.original.id,source_version=self.candidate.version_number)
        args=('bible','proposal','accept',str(proposal.id),'--json')
        code,stdout,_=self.invoke(*args)
        self.assertEqual(code,2);fingerprint=self.fingerprint(stdout)
        code,stdout,stderr=self.invoke('--approve-fingerprint',fingerprint,*args)
        self.assertEqual(code,1,stdout+stderr)
        self.assertEqual(self.store.list_facts(state='canonical'),[])
        self.assertEqual(self.store.list_proposals()[0].id,proposal.id)

    def test_read_and_resume_use_the_same_run_record_without_reexecuting_completed_work(self):
        code,stdout,stderr=self.invoke('outline','--dry-run','--json','只规划')
        self.assertEqual(code,0,stderr);run=json.loads(stdout.splitlines()[-1])['run_id']
        code,stdout,_=self.invoke('run','show',run,'--events','--json')
        self.assertEqual(code,0);self.assertEqual(json.loads(stdout)['run']['run_id'],run)
        self.assertNotIn('model.event',stdout)
        code,stdout,_=self.invoke('run','list','--json')
        self.assertEqual(code,0);self.assertEqual(json.loads(stdout)['runs'][0]['run_id'],run)
        code,stdout,stderr=self.invoke('run','resume',run,'--dry-run','--json')
        self.assertEqual(code,0,stderr+stdout)
        resumed=json.loads(stdout.splitlines()[-1])['run_id']
        self.assertNotEqual(resumed,run)
        self.assertEqual(RunStore(self.root).load(resumed)['resumed_from'],run)
        handle=RunStore(self.root).create(workflow='outline_workflow',command='outline',request='已完成',provider='synthetic',model='synthetic')
        RunStore(self.root).finalize(handle,status='completed')
        count=len(RunStore(self.root).list_runs())
        code,stdout,stderr=self.invoke('run','resume',handle.run_id,'--json')
        self.assertEqual(code,0,stderr+stdout)
        self.assertEqual(json.loads(stdout.splitlines()[-1])['run_id'],handle.run_id)
        self.assertEqual(len(RunStore(self.root).list_runs()),count)

    def test_import_and_confirmation_reuse_one_candidate(self):
        self.initialize()
        runs=RunStore(self.root)
        handle=runs.create(workflow='writing',command='write',request='Saved draft',provider='synthetic',model='synthetic')
        artifact=self.root/'ReferenceLibrary/drafts/import.md'
        artifact.parent.mkdir(parents=True,exist_ok=True);artifact.write_text('Imported candidate.')
        runs.finalize(handle,status='completed',artifact_path=artifact)
        args=('run','adopt',handle.run_id,'one','--kind','note','--title','原稿','--accept','--json')
        code,stdout,stderr=self.invoke(*args)
        self.assertEqual(code,2,stderr+stdout);fingerprint=self.fingerprint(stdout)
        self.assertEqual(self.store.get_document('one').content,'作者原稿。')
        count=len(self.store.list_versions('one'))
        code,stdout,stderr=self.invoke('--approve-fingerprint',fingerprint,*args)
        self.assertEqual(code,0,stderr+stdout)
        self.assertEqual(len(self.store.list_versions('one')),count)
        self.assertEqual(self.store.get_document('one').content,'Imported candidate.')
        self.assertTrue(json.loads(stdout)['active'])

    def provider(self,reply):
        seen=[]
        class Provider(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_POST(self):
                value=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                seen.append(value)
                text=reply(value)
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                event={'type':'response.completed','response':{'status':'completed','output':[{'type':'message','role':'assistant',
                    'content':[{'type':'output_text','text':text}]}],'usage':{'input_tokens':10,'output_tokens':5}}}
                try:self.wfile.write(('event: response.completed\ndata: '+json.dumps(event,ensure_ascii=False)+'\n\n').encode())
                except (BrokenPipeError,ConnectionResetError):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Provider)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        def close():server.shutdown();server.server_close();worker.join(timeout=5)
        self.addCleanup(close)
        self.provider_endpoint=f'http://127.0.0.1:{server.server_port}'
        return seen

    def test_real_native_cli_workflow_and_revision_preserve_author_text_and_history(self):
        from tests.test_execution_adapter import specimen
        self.initialize()
        def reply(value):
            fields=value['text']['format']['schema']['properties']
            if 'revision_markdown' in fields:
                result={'revision_markdown':'语法已修订。','change_log':['修订所选段落'],'summary':'完成候选',
                    'memory_proposals':[],'risks':[],'book_overview':'作者在故事中寻找真相，与守门者交锋。'}
            else:result=specimen(value['text']['format']['schema'])
            return json.dumps(result,ensure_ascii=False)
        seen=self.provider(reply)
        key=ConversationStore(self.root).create()
        code,stdout,stderr=self.invoke('--conversation',key,'outline','--json','只规划第一卷。')
        self.assertEqual(code,0,stderr+stdout)
        self.assertEqual(json.loads(stdout.splitlines()[-1])['status'],'completed')
        code,stdout,stderr=self.invoke('--conversation',key,'edit','one','--mode','grammar','--request-stdin','--json',
            stdin=json.dumps({'request':'只改语法','passage':'作者原稿。'}))
        self.assertEqual(code,0,stderr+stdout)
        result=json.loads(stdout.splitlines()[-1]);self.assertEqual(result['type'],'story.result')
        self.assertEqual(self.store.get_document('one').content,'作者原稿。')
        self.assertEqual(self.store.list_versions('one')[0].content,'语法已修订。')
        self.assertEqual(self.store.list_versions('one')[0].state,'candidate')
        self.assertEqual([row['role'] for row in ConversationStore(self.root).read(key)],['user','assistant']*2)
        self.assertGreater(len(seen),1)

    def _literary_journey(self,engine):
        from tests.helpers import make_config,reference_card
        from tests.intent_fixture import current_user,routed_reply
        from tests.test_anthropic_bridge import message_events
        from lg_cli.intent_router import MARKER,proposal
        from lg_cli.book_analysis import BookAnalysisStore,CATEGORIES,_prepare_contracts
        from lg_cli.book_analysis import fingerprint as snapshot_fingerprint
        from lg_cli.supervision import configure_supervision
        init_workspace(self.root);self.store=ProjectStore(self.root)
        prefix='前文：雨水顺着门楼流下。\n\n';selection='林砚说：“让我进去。”';suffix='\n\n后文：守卫抬起了灯。'
        original=self.store.create_document(kind='chapter',slug='one',title='第一章',sequence=1,
            content=prefix+selection+suffix,state='accepted')
        unrelated=self.store.create_document(kind='chapter',slug='two',title='第二章',sequence=2,
            content='无关章节：作者要求保持原样。',state='accepted')
        configure_supervision(self.store)
        self.store.add_fact(category='world',key='gate-rule',value='守卫必须核验通行证。',state='canonical')
        canonical=self.store.list_facts(state='canonical')
        replacements={'revision-two':'林砚压低声音，请守卫核验通行证。',
            'revision-three':'林砚停住脚步，请守卫核验旧通行证。'}
        key=ConversationStore(self.root).create()
        phase={'name':'plan','step':0};seen=[];errors=[];review_sources=[];serial=0

        def objects(value):
            if isinstance(value,str) and value.lstrip().startswith(('{','[')):
                try:yield from objects(json.loads(value))
                except ValueError:pass
            elif isinstance(value,dict):
                yield value
                for child in value.values():yield from objects(child)
            elif isinstance(value,list):
                for child in value:yield from objects(child)

        def response(payload):
            nonlocal serial
            text=current_user(payload.get('messages',[]))
            if text.startswith(MARKER):
                data=json.loads(text.split('INPUT_JSON:\n',1)[1])
                target=next(item['target'] for item in data['context']['saved']['documents'] if item['slug']=='one')
                if phase['name']=='plan':routed=proposal(kind='planning',stage='plan',target=target)
                elif phase['name'].startswith('revision'):
                    routed=proposal(kind='revision',target=target,passage=selection)
                elif phase['name']=='review':routed=proposal(kind='review',target=target,versionNumber=2)
                else:routed=routed_reply(text)
                return list(message_events(delta={'type':'text_delta','text':json.dumps(routed,ensure_ascii=False)}))
            step=phase['step'];phase['step']+=1
            name=phase['name']
            if name=='plan' and step==0:
                marker='Save author-requested literary reference material'
                arguments={'kind':'outline','slug':'chapter-01-plan','title':'第一章规划',
                    'content':'JOURNEY_PLAN：先核验通行证，再引出失踪案。','expected_version_id':0,'reason':'作者只请求规划第一章'}
            elif name in replacements and step==0:
                marker='Save an Agent-generated prose revision'
                arguments={'slug':'one','content':prefix+replacements[name]+suffix,
                    'expected_version_id':original.active_version_id,'reason':'仅替换作者明确选区',
                    'book_overview':'林砚追查失踪案，在城门核验线索并逐步揭开秘密。'}
            elif name=='review' and step==0:
                marker='Read the exact source snapshot'
                arguments={'slug':'one','version_number':2}
            elif name=='review' and step==1:
                marker='Save an Agent-generated prose revision'
                arguments={'slug':'one','content':'UNAUTHORIZED_REVIEW_REVISION',
                    'expected_version_id':original.active_version_id,'reason':'Must be refused by read-only task',
                    'book_overview':'林砚追查失踪案，在城门核验线索并逐步揭开秘密。'}
            elif name=='review' and step==2:
                context=next(value for value in objects(payload['messages'])
                    if 'sourceFingerprint' in value and 'snapshot' in value)
                review_sources.append(context)
                marker='Save source-validated reviews produced from studio_review_context'
                arguments={'slug':'one','version_number':2,'source_fingerprint':context['sourceFingerprint'],
                    'reports':{category:{'summary':'已核对准确v2候选，未发现需记录的冲突。',
                        'entries':[{'instance_card':reference_card(category),'key':'selected-source','label':'选定候选',
                            'analysis':'仅依据作者选定v2的核验通行证动作。','evidence':[{'span_id':next(identity
                                for identity,span in context['source_spans'].items() if replacements['revision-two'] in span['quote'])}]}],
                        'findings':[]}
                        for category in context['schemas']}}
            else:
                return list(message_events(delta={'type':'text_delta','text':'已完成本轮指定范围；正文采用仍需作者另行确认。'}))
            tool=next(tool for tool in payload['tools'] if marker in tool.get('description',''))
            serial+=1
            return list(message_events(block={'type':'tool_use','id':'journey-'+str(serial),'name':tool['name'],'input':{}},
                delta={'type':'input_json_delta','partial_json':json.dumps(arguments,ensure_ascii=False)},stop='tool_use'))

        class Provider(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_POST(handler):
                try:
                    self.assertEqual(handler.path,'/v1/messages')
                    payload=json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))
                    seen.append(payload);events=response(payload)
                    for event in events:
                        if event['type']=='message_start':
                            event['message'].update(id='local-message',type='message',role='assistant',content=[],
                                model=payload['model'],stop_reason=None,stop_sequence=None)
                    handler.send_response(200);handler.send_header('Content-Type','text/event-stream');handler.end_headers()
                    for event in events:
                        handler.wfile.write(('event: '+event['type']+'\ndata: '+json.dumps(event,ensure_ascii=False)+'\n\n').encode())
                    handler.wfile.flush()
                except Exception as exc:
                    errors.append(type(exc).__name__+': '+str(exc))
                    try:handler.send_error(500,'Synthetic fixture failed')
                    except (BrokenPipeError,ConnectionResetError):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Provider)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),worker.join(timeout=5)))
        config=replace(make_config(self.root),runtime_engine=engine,provider='stepfun',protocol='anthropic',
            default_model='journey-'+engine,api_key='synthetic-journey-key',enable_reference=False,
            base_url=f'http://127.0.0.1:{server.server_port}',timeout_seconds=60,max_stage_context_chars=200000,
            runtime_manifest=Path(os.environ['LG_TEST_RUNTIME_MANIFEST']) if engine=='codex' else None)
        import socket
        connect=socket.socket.connect
        def local_connect(sock,address):
            if isinstance(address,tuple):
                self.assertIn(address[0],('127.0.0.1','::1','localhost'),'Fixture attempted a non-local connection')
            return connect(sock,address)

        def cli(*arguments):
            stdout=io.StringIO();stderr=io.StringIO()
            with patch('lg_cli.main.load_config',return_value=config),\
                    patch('lg_cli.credentials.read_profiles',return_value={'profiles':{}}),\
                    patch.dict(os.environ,{'HTTP_PROXY':'','HTTPS_PROXY':'','ALL_PROXY':'',
                        'http_proxy':'','https_proxy':'','all_proxy':'',
                        'NO_PROXY':'127.0.0.1,localhost,::1','no_proxy':'127.0.0.1,localhost,::1'}),\
                    patch('socket.socket.connect',new=local_connect),\
                    patch('sys.stdin',io.StringIO('')),redirect_stdout(stdout),redirect_stderr(stderr):
                code=main(['-C',str(self.root),'--runtime-engine',engine,'--conversation',key,
                    '--operation-mode','full_access',*arguments])
            self.assertEqual(errors,[],stderr.getvalue()+stdout.getvalue())
            return code,stdout.getvalue(),stderr.getvalue()

        def agent(name,text):
            phase.update(name=name,step=0)
            code,stdout,stderr=cli('--json',text)
            self.assertEqual(code,0,stderr+stdout)
            self.assertEqual(json.loads(stdout.splitlines()[-1])['status'],'completed')
            self.assertEqual(self.store.get_document('one').active_version_id,original.active_version_id)
            self.assertEqual(self.store.get_document('one').content,original.content)
            self.assertEqual(self.store.list_facts(state='canonical'),canonical)
            self.assertEqual(self.store.get_document('two'),unrelated)

        agent('plan','只规划第一章 one，保存章节大纲，不生成正文。')
        plans=list((self.root/'ReferenceLibrary/plans').rglob('*.md'))
        self.assertTrue(any('JOURNEY_PLAN' in path.read_text() for path in plans))
        self.assertEqual(len(self.store.list_versions('one')),1)
        for phase_name in replacements:
            agent(phase_name,'局部修订第一章 one，只改选区“'+selection+'”，保留前后原文，不采纳。')
        two=self.store.get_version('one',2);three=self.store.get_version('one',3)
        self.assertEqual(two.content,prefix+replacements['revision-two']+suffix)
        self.assertEqual(three.content,prefix+replacements['revision-three']+suffix)
        self.assertEqual((two.state,three.state),('candidate','candidate'))
        self.assertEqual((two.metadata['base_version_id'],three.metadata['base_version_id']),
            (original.active_version_id,original.active_version_id))
        agent('review','只审读第一章 one v2，不修改正文，不采纳任何版本。')
        task=SessionRuntime(self.root,key).read()['task']['definition']
        self.assertEqual((task['kind'],task['versionNumber']),('review',2))
        self.assertFalse(task.get('allowRevision',False))
        self.assertEqual(len(self.store.list_versions('one')),3)
        self.assertEqual(len(review_sources),1)
        reviewed=next(value for value in review_sources[0]['snapshot']['documents'] if value['id']==original.id)
        self.assertEqual((reviewed['version_id'],reviewed['content']),(two.id,two.content))
        self.assertTrue(any('outside the author task scope' in json.dumps(payload,ensure_ascii=False) for payload in seen))
        records=BookAnalysisStore(self.store)
        contracts=_prepare_contracts(CATEGORIES)
        for category in CATEGORIES:
            report=json.loads(records._path('one',category,candidate_id=two.id).read_text())
            self.assertEqual(report['fingerprint'],review_sources[0]['sourceFingerprint'])
            self.assertEqual(report['contract_fingerprint'],contracts[category].fingerprint)
            self.assertEqual(len(report['report']['entries']),1)
            for entry in report['report']['entries']:
                self.assertEqual(len(entry['evidence']),1)
                for evidence in entry['evidence']:
                    self.assertEqual((evidence['document_id'],evidence['version_id']),(original.id,two.id))
                    self.assertIn(evidence['quote'],two.content)
            self.assertFalse(records._path('one',category,candidate_id=three.id).exists())
        runtime=SessionRuntime(self.root,key).read()
        self.assertEqual((runtime['engine'],runtime['mode'],runtime['creative_mode']),(engine,'full_access','ask'))
        self.assertTrue(all(payload['model']=='journey-'+engine for payload in seen))
        if engine=='native':self.assertTrue((self.root/'.literarygiant/runtime/native'/f'{key}.json').exists())
        else:self.assertTrue(list((self.root/'.literarygiant/codex-home/sessions').rglob('*.jsonl')))

        phase.update(name='adoption',step=0)
        arguments=('version','accept','one','2','--json')
        count=len(seen)
        code,stdout,stderr=cli(*arguments)
        self.assertEqual(code,2,stderr+stdout);old_fingerprint=self.fingerprint(stdout)
        self.assertEqual(len(seen),count,'Missing creative approval must not start another model')
        self.assertEqual(self.store.get_document('one').content,original.content)
        self.store.create_document(kind='note',slug='author-note',title='作者独立笔记',content='作者补充了独立笔记。')
        code,stdout,stderr=cli('--approve-fingerprint',old_fingerprint,*arguments)
        self.assertEqual(code,1,stderr+stdout);self.assertIn('fingerprint changed',stdout+stderr)
        self.assertEqual(self.store.get_document('one').active_version_id,original.active_version_id)
        code,stdout,stderr=cli(*arguments)
        self.assertEqual(code,2,stderr+stdout);fingerprint=self.fingerprint(stdout)
        self.assertNotEqual(fingerprint,old_fingerprint)
        code,stdout,stderr=cli('--approve-fingerprint',fingerprint,*arguments)
        self.assertEqual(code,0,stderr+stdout)
        self.assertEqual(self.store.get_document('one').active_version_id,two.id)
        self.assertEqual(self.store.get_document('one').content,two.content)
        self.assertEqual(self.store.get_version('one',3).state,'candidate')
        self.assertEqual(self.store.list_facts(state='canonical'),canonical)
        self.assertEqual(self.store.get_document('two'),unrelated)
        for category in CATEGORIES:
            published=json.loads(records._path('one',category).read_text())
            self.assertEqual(published['fingerprint'],snapshot_fingerprint(records.snapshot('one')))
            self.assertEqual(published['contract_fingerprint'],contracts[category].fingerprint)
            self.assertEqual(len(published['report']['entries']),1)
            for entry in published['report']['entries']:
                self.assertEqual(len(entry['evidence']),1)
                for evidence in entry['evidence']:
                    self.assertEqual((evidence['document_id'],evidence['version_id']),(original.id,two.id))
                    self.assertIn(evidence['quote'],two.content)
        version_count=len(self.store.list_versions('one'))
        code,stdout,stderr=cli('--approve-fingerprint',fingerprint,*arguments)
        self.assertEqual(code,1,stderr+stdout);self.assertIn('fingerprint changed',stdout+stderr)
        self.assertEqual(len(self.store.list_versions('one')),version_count)
        self.assertEqual(self.store.get_document('one').active_version_id,two.id)
        self.assertEqual(len(seen),count,'Current saved reviews suffice; adoption must not generate another candidate')

    def test_native_cli_literary_journey_uses_real_router_tools_and_separate_adoption(self):
        self._literary_journey('native')

    @unittest.skipUnless(os.environ.get('LG_TEST_RUNTIME_MANIFEST'),'set LG_TEST_RUNTIME_MANIFEST for pinned Core')
    def test_codex_cli_literary_journey_uses_real_router_tools_and_separate_adoption(self):
        self._literary_journey('codex')

    def test_failed_real_worker_keeps_exit_code_and_private_diagnostics_out_of_stdout(self):
        self.initialize();seen=self.provider(lambda _:'PRIVATE_SENTINEL invalid structured result')
        code,stdout,stderr=self.invoke('outline','--json','只规划')
        self.assertEqual(code,4,stderr+stdout)
        result=json.loads(stdout.splitlines()[-1])
        self.assertEqual((result['status'],result['exit_code']),('failed',4))
        self.assertEqual(len(seen),1)
        self.assertNotIn('PRIVATE_SENTINEL',stdout+stderr)
        key=ConversationStore(self.root).list()[0][0]
        self.assertEqual([row['role'] for row in ConversationStore(self.root).read(key)],['user'])

    def test_sigint_cancels_owned_worker_and_releases_shared_conversation(self):
        from lg_cli.run_store import process_owner
        self.initialize();entered=threading.Event();release=threading.Event()
        def reply(_):entered.set();release.wait(20);return '{}'
        self.provider(reply)
        key=ConversationStore(self.root).create()
        process=subprocess.Popen([sys.executable,'-u',str(Path(__file__).resolve()),self.provider_endpoint,'-C',str(self.root),'--runtime-engine','native',
            '--conversation',key,'outline','--json','只规划'],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            if not entered.wait(15):
                process.send_signal(signal.SIGINT)
                stdout,stderr=process.communicate(timeout=12)
                self.fail('CLI did not reach the local provider: '+stdout+stderr)
            run=RunStore(self.root).list_runs()[0]
            pid=RunStore(self.root).load(run['run_id'])['owner']['pid']
            process.send_signal(signal.SIGINT)
            stdout,stderr=process.communicate(timeout=12)
            self.assertEqual(process.returncode,130,stderr+stdout)
            self.assertIsNone(process_owner(pid),'Owned workflow still runs after CLI cancellation')
            runtime=SessionRuntime(self.root,key);runtime.acquire();runtime.release()
            self.assertEqual(self.store.get_document('one').content,'作者原稿。')
        finally:
            release.set()
            if process.poll() is None:process.kill();process.communicate(timeout=5)

    def test_story_stdin_stays_private_and_returns_the_same_result_shape(self):
        self.initialize()
        code,stdout,stderr=self.invoke('edit','one','--mode','grammar','--dry-run','--request-stdin','--json',
            stdin=json.dumps({'request':'只改语法','passage':'作者原稿。'}))
        self.assertEqual(code,0,stderr+stdout)
        result=json.loads(stdout.splitlines()[-1]);self.assertEqual(result['type'],'story.result')
        self.assertEqual(result['document']['id'],self.original.id)
        self.assertEqual(self.store.get_document('one').active_version_id,self.original.active_version_id)

    def test_natural_words_do_not_use_the_old_keyword_write_workflow(self):
        from lg_cli.intent_router import proposal
        from lg_cli.terminal_backend import TerminalBackendClient
        from tests.test_backend import FakeAgent
        class Client(TerminalBackendClient):
            def __init__(self,*args,**kwargs):super().__init__(*args,**kwargs,agent_factory=FakeAgent)
        with patch('lg_cli.cli_backend.TerminalBackendClient',Client),\
                patch('lg_cli.intent_router.interpret',AsyncMock(return_value=proposal())),\
                patch('lg_cli.main.WorkflowRunner.run',side_effect=AssertionError('Natural language must use run/start')):
            code,stdout,stderr=self.invoke('--json','只讨论正文，不保存')
        self.assertEqual(code,0,stderr+stdout);result=json.loads(stdout.splitlines()[-1])
        scope=SessionRuntime(self.root,result['session_id']).read()['task']
        self.assertEqual(scope['definition']['kind'],'discussion')

    def test_public_worker_failures_do_not_echo_private_body(self):
        from lg_cli.workflow_io import public_workflow_event,public_workflow_result
        event=public_workflow_event({'type':'stage.failed','message':'PRIVATE_SENTINEL','data':{'private':'PRIVATE_SENTINEL'}})
        result=public_workflow_result({'type':'run.result','text':'PRIVATE_SENTINEL','error':'PRIVATE_SENTINEL'},code=4)
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps([event,result]))


class InteractiveFactTests(unittest.TestCase):
    def test_promote_and_proposal_accept_share_backend_creative_approval(self):
        import asyncio
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from lg_cli.main import interactive_loop
        from lg_cli.terminal_input import LiteraryInput
        from tests.helpers import make_config
        with tempfile.TemporaryDirectory() as raw,create_pipe_input() as pipe:
            root=Path(raw);store=ProjectStore(root);store.initialize(name='Author facts')
            fact=store.add_fact(category='world',key='代价',value='力量需要代价。',state='idea')
            proposal=store.propose_fact(category='character',key='动机',value='寻找亲人。')
            config=replace(make_config(root,api_key=None),runtime_engine='native',environment='cli-fact-test-no-profile')
            session=LiteraryInput(root/'history',input=pipe,output=DummyOutput(),usage_path=root/'usage.json')
            def run(handler,*_args,**_kwargs):
                async def exercise():
                    try:
                        with patch.object(session,'ask',return_value='decline'):
                            await handler('/bible promote '+str(fact.id))
                        self.assertEqual(store.get_fact(fact.id).state,'idea')
                        with patch.object(session,'ask',return_value='accept') as approval:
                            await handler('/bible promote '+str(fact.id)+' --reason 作者确认')
                            self.assertIn('作者确认',approval.call_args.kwargs['text'])
                        self.assertEqual(store.get_fact(fact.id).state,'canonical')
                        with patch.object(session,'ask',return_value='accept') as approval:
                            await handler('/bible proposal accept '+str(proposal.id)+' --state idea')
                            self.assertIn('idea',approval.call_args.kwargs['text'])
                        self.assertEqual(store.list_facts(category='character')[0].state,'idea')
                    finally:await session.shutdown_handler()
                    return 0
                return asyncio.run(exercise())
            with patch('lg_cli.main.LiteraryInput',return_value=session),patch.object(session,'run',run),\
                    patch('lg_cli.main.load_config',return_value=config):
                self.assertEqual(interactive_loop(config),0)


if __name__=='__main__':
    root=Path(sys.argv[sys.argv.index('-C')+1])
    with isolated_cli(root,sys.argv[1]):
        raise SystemExit(main(sys.argv[2:]))
