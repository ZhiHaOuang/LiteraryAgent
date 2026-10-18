"""Actual owned workflow processes, shared conversation storage and control concurrency."""
import asyncio
import json
import io
import sys
import threading
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
import jsonschema

from lg_cli.backend import Backend
from lg_cli.conversation_store import ConversationStore
from lg_cli.init_project import init_workspace
from lg_cli.project_store import ProjectStore
from lg_cli.run_store import RunStore
from lg_cli.workflow_io import WorkflowStream,read_story_request
from lg_cli.workflow_control import control_request
from tests.helpers import make_config
from tests.test_backend import FakeAgent
from tests.test_execution_adapter import specimen


class TerminalWorkflowEntryTests(unittest.TestCase):
    def test_slash_command_uses_backend_and_saves_history_once(self):
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from lg_cli.main import interactive_loop
        from lg_cli.terminal_input import LiteraryInput
        from lg_cli.terminal_backend import TerminalBackendClient
        with tempfile.TemporaryDirectory() as raw, create_pipe_input() as pipe:
            root = Path(raw)
            init_workspace(root)
            project=ProjectStore(root)
            project.create_document(kind='chapter',slug='one',title='原稿',content='作者原稿')
            project.create_scene(slug='gate',title='城门',chapter='one')
            config = replace(make_config(root,api_key=None),runtime_engine='native',environment='workflow-test-no-profile')
            clients = []
            class Client(TerminalBackendClient):
                def __init__(self,*args,**kwargs):
                    super().__init__(*args,**kwargs);clients.append(self)
            session = LiteraryInput(root/'history',input=pipe,output=DummyOutput(),usage_path=root/'usage.json')
            def run(handler,*_args,**_kwargs):
                async def exercise():
                    try:
                        await handler('/world --dry-run --no-reference 只规划世界规则')
                        backend = clients[0].backend
                        records = backend.store.read(clients[0].key)
                        self.assertEqual([record['role'] for record in records],['user','assistant'])
                        self.assertEqual(records[0]['text'],'只规划世界规则')
                        self.assertEqual(set(backend.task_kinds.values()),{'workflow'})
                        self.assertEqual(session.transcript.count('LG Workflow Plan: world'),1)
                        for command in ('/scene plan gate --dry-run 只规划城门',
                                '/edit one --mode grammar --dry-run --passage 作者原稿 只修改选中段落',
                                '/bible curate one --dry-run 提取事实提案'):
                            await handler(command)
                        records=backend.store.read(clients[0].key)
                        self.assertEqual([record['role'] for record in records],['user','assistant']*4)
                        self.assertEqual(project.get_document('one').content,'作者原稿')
                        project.set_scene_plan('gate','只盘问身份，不写正文。')
                        with patch.object(session,'ask',return_value='accept') as approval:
                            await handler('/scene approve gate')
                            self.assertIn('只盘问身份',approval.call_args.kwargs['text'])
                        self.assertEqual(project.get_scene('gate').plan_state,'approved')
                        self.assertIn('场景计划已批准',session.transcript)
                        self.assertEqual(set(backend.task_kinds.values()),{'workflow','operation'})
                        runs=RunStore(root)
                        handle=runs.create(workflow='writing',command='write',request='Candidate import',provider='synthetic',model='synthetic')
                        artifact=root/'ReferenceLibrary'/'drafts'/'import.md'
                        artifact.parent.mkdir(parents=True,exist_ok=True)
                        artifact.write_text('Imported candidate for backend.')
                        runs.finalize(handle,status='completed',artifact_path=artifact)
                        with patch.object(session,'ask',return_value='decline') as adoption:
                            await handler('/run adopt '+handle.run_id+' one --kind chapter --title Imported --accept')
                            candidate=project.list_versions('one')[0]
                            await handler('/version accept one '+str(candidate.version_number))
                            await handler('/scene accept gate 1')
                            self.assertEqual(adoption.call_count,3)
                        self.assertEqual(project.get_document('one').content,'作者原稿')
                        self.assertEqual(candidate.state,'candidate')
                        self.assertEqual(len(backend.store.read(clients[0].key)),10)
                        self.assertEqual(session.transcript.count('导入候选已保留，未采用'),1)
                        with patch.object(session,'ask',return_value=None):
                            await handler('/run list')
                        await handler('/run show '+handle.run_id+' --events')
                        self.assertEqual(len(backend.store.read(clients[0].key)),10)
                        await handler('/run resume '+handle.run_id+' --dry-run')
                        self.assertEqual(len(backend.store.read(clients[0].key)),12)
                        self.assertIn('nothing to resume',session.transcript)
                    finally:
                        await session.shutdown_handler()
                    return 0
                return asyncio.run(exercise())
            with patch('lg_cli.main.LiteraryInput',return_value=session),patch.object(session,'run',run),\
                patch('lg_cli.main.load_config',return_value=config),\
                patch('lg_cli.terminal_backend.TerminalBackendClient',Client):
                self.assertEqual(interactive_loop(config),0)


class WorkflowStreamTests(unittest.TestCase):
    def test_story_request_rejects_invalid_or_oversized_envelopes_without_echoing_content(self):
        self.assertEqual(read_story_request(io.StringIO('{"request":"中文🖋","passage":"SELECTED"}')),
            {'request':'中文🖋','passage':'SELECTED'})
        for value in ('PRIVATE_SENTINEL','[]','{"request":"x","argv":[]}',
                json.dumps({'request':'x','passage':''}),json.dumps({'request':'x'*(4*1024*1024+1)})):
            with self.subTest(size=len(value)),self.assertRaises(ValueError):
                read_story_request(io.StringIO(value))

    def test_utf8_and_multiple_records_preserve_exact_text(self):
        records = []
        parser = WorkflowStream(records.append)
        data = (json.dumps({'text':'中文🖋正文'},ensure_ascii=False)+'\n'+json.dumps({'type':'run.result'})+'\n').encode()
        for value in data:
            parser.feed(bytes([value]))
        parser.end()
        self.assertEqual(records,[{'text':'中文🖋正文'},{'type':'run.result'}])

    def test_malformed_output_does_not_publish_private_body(self):
        for data in (b'PRIVATE_SENTINEL\n',b'[]\n',b'\xff\n'):
            with self.subTest(data=data):
                records = []
                with self.assertRaises(ValueError) as error:
                    WorkflowStream(records.append).feed(data)
                self.assertNotIn('PRIVATE_SENTINEL',str(error.exception))
                self.assertEqual(records,[])


class BackendWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        init_workspace(self.root)
        self.project = ProjectStore(self.root)
        self.original = self.project.create_document(kind='chapter',slug='one',title='原稿',
            content='作者的原稿。',state='accepted')
        self.events = []
        self.config = replace(make_config(self.root,api_key=None),runtime_engine='native',
            environment='workflow-test-no-profile')
        self.backend = Backend(self.config,self.events.append,agent_factory=FakeAgent)
        self.key = (await self.backend.dispatch('session/create',{}))['sessionId']

    async def asyncTearDown(self):
        await self.backend.close()
        self.directory.cleanup()

    async def finished(self,response):
        await self.backend.tasks[response['runId']][1]
        return next(event['params'] for event in reversed(self.events)
            if event['method']=='run/completed' and event['params']['runId']==response['runId'])

    async def test_real_dry_run_history_is_saved_once_and_resumes_without_codex(self):
        self.backend.store.append(self.key,'user','历史约束：只规划闸门事件。')
        params = {'sessionId':self.key,'command':'outline','request':'只规划第一卷。中文🖋',
            'dryRun':True,'noReference':True}
        response = await self.backend.dispatch('workflow/run',params)
        final = await self.finished(response)
        self.assertEqual(final['status'],'completed',final.get('error'))
        worker = RunStore(self.root).load(final['result']['workflowRunId'])
        self.assertEqual(worker['status'],'planned')
        self.assertIn('历史约束',worker['request'])
        records = ConversationStore(self.root).read(self.key)
        self.assertEqual([record['role'] for record in records],['user','user','assistant'])
        self.assertEqual(records[1]['text'],params['request'])
        snapshot = await self.backend.dispatch('session/snapshot',{'sessionId':self.key})
        self.assertEqual(len([item for item in snapshot['items'] if item['kind']=='user']),2)
        self.assertEqual(len([item for item in snapshot['items'] if item['kind']=='assistant']),1)
        await self.backend.close()
        self.backend = Backend(self.config,self.events.append,agent_factory=FakeAgent)
        await self.backend.dispatch('session/open',{'sessionId':self.key})
        again = await self.backend.dispatch('session/snapshot',{'sessionId':self.key})
        self.assertEqual(snapshot['items'],again['items'])
        self.assertEqual(self.project.get_document('one').active_version_id,self.original.active_version_id)

    async def test_unknown_command_or_arbitrary_argv_cannot_start_a_worker(self):
        for params in ({'command':'shell','request':'touch anything'},
            {'command':'outline','request':'任务','argv':['sh']},
            {'command':'outline','request':'任务','runtimeManifest':'relative.json'}):
            with self.subTest(params=params):
                with self.assertRaises(Exception):
                    await self.backend.dispatch('workflow/run',{'sessionId':self.key,**params})
        self.assertEqual(self.backend.tasks,{})
        self.assertEqual(self.backend.store.read(self.key),[])

    async def test_async_retry_has_new_run_and_saves_only_verified_reply(self):
        control = self.backend.session(self.key)['control']
        original = control.session.run_command
        async def fail(argv,**options):
            return 1
        control.session.run_command = fail
        first = await self.backend.dispatch('workflow/run',{'sessionId':self.key,'command':'world',
            'request':'只规划世界','dryRun':True,'noReference':True})
        self.assertEqual((await self.finished(first))['status'],'failed')
        control.session.run_command = original
        endpoint = await control.endpoint()
        retry = await asyncio.to_thread(control_request,endpoint,'retry',job_id=control.status()['id'],guidance='只规划第二段')
        self.assertNotEqual(retry['runId'],first['runId'])
        self.assertEqual(retry['status'],'starting')
        self.assertTrue(self.backend._active(self.key))
        with self.assertRaises(ValueError):
            await self.backend.dispatch('permissions/set',{'sessionId':self.key,'mode':'full_access'})
        final = await self.finished(retry)
        self.assertEqual(final['status'],'completed',final.get('error'))
        rows = self.backend.store.read(self.key)
        self.assertEqual([row['role'] for row in rows],['user','assistant'])
        self.assertEqual(rows[-1]['run_id'],retry['runId'])
        snapshot = await self.backend.dispatch('session/snapshot',{'sessionId':self.key})
        self.assertEqual(len([item for item in snapshot['items'] if item['kind']=='user']),1)
        self.assertEqual(len([item for item in snapshot['items'] if item['kind']=='notice' and item['title']=='重试']),1)
        self.assertEqual(snapshot['activeRuns'],[])
        with self.assertRaises(ValueError):
            await control.retry(control.status()['id'])

    async def test_retry_without_valid_result_finishes_failed_and_can_retry_again(self):
        control = self.backend.session(self.key)['control']
        async def missing_result(argv,**options):
            return 0
        control.session.run_command = missing_result
        first = await self.backend.dispatch('workflow/run',{'sessionId':self.key,'command':'world','request':'规划'})
        self.assertEqual((await self.finished(first))['status'],'failed')
        self.assertEqual(control.status()['status'],'failed')
        retry = await control.retry(control.status()['id'])
        self.assertEqual((await self.finished(retry))['status'],'failed')
        self.assertEqual(control.status()['status'],'failed')
        self.assertEqual([row['role'] for row in self.backend.store.read(self.key)],['user'])

    async def test_cancelled_retry_before_start_never_spawns_a_process(self):
        control = self.backend.session(self.key)['control']
        calls = []
        async def fail(argv,**options):
            calls.append(argv)
            return 1
        control.session.run_command = fail
        first = await self.backend.dispatch('workflow/run',{'sessionId':self.key,'command':'world','request':'规划'})
        await self.finished(first)
        retry = await control.retry(control.status()['id'])
        await self.backend.cancel(self.key)
        self.assertEqual((await self.finished(retry))['status'],'cancelled')
        self.assertEqual(len(calls),1)
        self.assertEqual(control.status()['status'],'stopped')

    async def test_analysis_retry_requires_fresh_approval_before_worker(self):
        control=self.backend.session(self.key)['control']
        calls=[]
        async def fail(argv,**kwargs):
            calls.append(argv)
            return 1
        control.session.run_command=fail
        async def decide(decision):
            for _ in range(100):
                if self.backend.approvals.pending:break
                await asyncio.sleep(.01)
            self.backend.approvals.resolve(next(iter(self.backend.approvals.pending)),{'decision':decision})
        first=await self.backend.dispatch('workflow/start',{'sessionId':self.key,'chapter':'one','category':'Worldview'})
        await decide('accept');await self.finished(first)
        retry=await control.retry(control.status()['id'])
        await decide('decline')
        self.assertEqual((await self.finished(retry))['status'],'cancelled')
        self.assertEqual(len(calls),1)
        retry=await control.retry(control.status()['id'])
        await decide('accept')
        self.assertEqual((await self.finished(retry))['status'],'failed')
        self.assertEqual(len(calls),2)

    async def test_stop_analysis_retry_while_approval_pending_never_starts_worker(self):
        control=self.backend.session(self.key)['control']
        calls=[]
        async def fail(argv,**options):
            calls.append(argv)
            return 1
        control.session.run_command=fail
        async def pending():
            for _ in range(100):
                if self.backend.approvals.pending:
                    return next(iter(self.backend.approvals.pending))
                await asyncio.sleep(.01)
            self.fail('Expected analysis approval')
        first=await self.backend.dispatch('workflow/start',{'sessionId':self.key,'chapter':'one','category':'Worldview'})
        self.backend.approvals.resolve(await pending(),{'decision':'accept'})
        await self.finished(first)
        retry=await control.retry(control.status()['id'])
        approval=await pending()
        status=await asyncio.wait_for(control.stop(control.status()['id']),2)
        self.assertEqual(status['status'],'stopped')
        self.assertEqual((await self.finished(retry))['status'],'cancelled')
        self.assertEqual(len(calls),1)
        self.assertNotIn(approval,self.backend.approvals.pending)
        with self.assertRaises(ValueError):
            self.backend.approvals.resolve(approval,{'decision':'accept'})

    async def test_concurrent_analysis_approval_keeps_workflow_owner_after_main_finishes(self):
        control=self.backend.session(self.key)['control']
        calls=[]
        async def fail(argv,**options):
            calls.append(argv)
            return 1
        control.session.run_command=fail
        async def pending():
            for _ in range(100):
                if self.backend.approvals.pending:
                    return next(iter(self.backend.approvals.pending))
                await asyncio.sleep(.01)
            self.fail('Expected analysis approval')
        first=await self.backend.dispatch('workflow/start',{'sessionId':self.key,'chapter':'one','category':'Worldview'})
        self.backend.approvals.resolve(await pending(),{'decision':'accept'})
        await self.finished(first)
        async def hold(agent,text,output_schema=None):
            agent.turn_id='held'
            await agent.on_request('item/commandExecution/requestApproval',{'command':'test'})
            agent.turn_id=None
            return {'id':'held','status':'completed'}
        with patch.object(FakeAgent,'run',hold):
            main=await self.backend.run(self.key,'保持控制对话')
            main_approval=await pending()
            retry=await control.retry(control.status()['id'])
            for _ in range(100):
                if len(self.backend.approvals.pending)==2:break
                await asyncio.sleep(.01)
            approval=next(key for key in self.backend.approvals.pending if key!=main_approval)
            shown={event['params']['approval']['id']:event['params']['approval']['runId'] for event in self.events
                if event['method']=='client/event' and event['params']['type']=='approval.requested'}
            self.assertEqual(shown[approval],retry['runId'])
            self.assertEqual(shown[main_approval],main['runId'])
            self.backend.approvals.resolve(main_approval,{'decision':'accept'})
            self.assertEqual((await self.finished(main))['status'],'completed')
            self.assertIn(approval,self.backend.approvals.pending)
            snapshot=await self.backend.dispatch('session/snapshot',{'sessionId':self.key})
            self.assertEqual(snapshot['activeRuns'],[{'id':retry['runId'],'state':'running'}])
            self.backend.approvals.resolve(approval,{'decision':'decline'})
            self.assertEqual((await self.finished(retry))['status'],'cancelled')
            self.assertEqual(len(calls),1)

    async def test_retry_can_run_while_main_control_turn_is_active(self):
        control = self.backend.session(self.key)['control']
        original = control.session.run_command
        async def fail(argv,**options):
            return 1
        control.session.run_command=fail
        first=await self.backend.dispatch('workflow/run',{'sessionId':self.key,'command':'world',
            'request':'规划','dryRun':True,'noReference':True})
        await self.finished(first)
        control.session.run_command=original
        async def hold(agent,text,output_schema=None):
            agent.turn_id='held-control-turn'
            await asyncio.Future()
        with patch.object(FakeAgent,'run',hold):
            main=await self.backend.run(self.key,'wait')
            await asyncio.sleep(.01)
            retry=await control.retry(control.status()['id'],'继续原任务')
            self.assertEqual((await self.finished(retry))['status'],'completed')
            snapshot=await self.backend.dispatch('session/snapshot',{'sessionId':self.key})
            self.assertEqual(snapshot['activeRuns'],[{'id':main['runId'],'state':'running'}])
            await self.backend.cancel(self.key)
            self.assertEqual((await self.finished(main))['status'],'cancelled')

    async def test_cancel_stops_running_retry_and_keeps_completed_file(self):
        control = self.backend.session(self.key)['control']
        original = control.session.run_command
        async def fail(argv,**options):
            return 1
        control.session.run_command=fail
        first=await self.backend.dispatch('workflow/run',{'sessionId':self.key,'command':'world','request':'规划'})
        await self.finished(first)
        completed=self.root/'completed-before-stop.txt'
        async def held(argv,**options):
            return await original([sys.executable,'-c',
                'import pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text("completed");time.sleep(60)',str(completed)])
        control.session.run_command=held
        retry=await control.retry(control.status()['id'])
        for _ in range(300):
            if completed.exists():break
            await asyncio.sleep(.01)
        self.assertTrue(completed.exists())
        process=control.session.process
        await self.backend.cancel(self.key)
        self.assertEqual((await self.finished(retry))['status'],'cancelled')
        self.assertIsNotNone(process.returncode)
        self.assertEqual(completed.read_text(),'completed')

    async def test_workflow_and_main_agent_keep_separate_events_and_cancel_together(self):
        control = self.backend.session(self.key)['control']
        original = control.session.run_command
        ready = asyncio.Event()
        async def held(argv,**options):
            ready.set()
            return await original([sys.executable,'-c','import time; time.sleep(60)'])
        control.session.run_command = held
        workflow = await self.backend.dispatch('workflow/run',{'sessionId':self.key,'command':'world','request':'只规划'})
        await ready.wait()
        agent = await self.backend.run(self.key,'hello')
        self.assertEqual((await self.finished(agent))['status'],'completed')
        self.assertTrue(self.backend._active(self.key))
        with self.assertRaises(ValueError):
            await self.backend.dispatch('permissions/set',{'sessionId':self.key,'mode':'full_access'})
        with self.assertRaises(ValueError):
            await self.backend.dispatch('workflow/run',{'sessionId':self.key,'command':'plot','request':'另一个任务'})
        await self.backend.dispatch('run/cancel',{'sessionId':self.key})
        self.assertEqual((await self.finished(workflow))['status'],'cancelled')
        self.assertEqual(self.project.get_document('one').content,'作者的原稿。')
        agent_items = [event['params']['item'] for event in self.events if event['method']=='client/event'
            and event['params'].get('type')=='item.updated' and event['params']['item']['kind']=='assistant']
        self.assertEqual(agent_items[0]['runId'],agent['runId'])

    async def test_story_dry_runs_keep_sources_and_validate_saved_targets(self):
        scene=self.project.create_scene(slug='gate',title='城门',chapter='one')
        self.project.set_scene_plan(scene.document.id,'先盘问，再拒绝。')
        self.project.approve_scene(scene.document.id)
        self.backend.store.append(self.key,'user','历史约束：只处理城门。')
        for command in ('scene-plan','scene-draft','revise','bible-curate'):
            with self.subTest(command=command):
                params={'sessionId':self.key,'command':command,'request':'中文任务🖋','dryRun':True,
                    'document':'gate' if command.startswith('scene-') else 'one'}
                if command=='revise':params.update(revisionMode='grammar',passage='作者的原稿。')
                final=await self.finished(await self.backend.dispatch('workflow/run',params))
                self.assertEqual(final['status'],'completed',final.get('error'))
                run=RunStore(self.root).load(final['result']['workflowRunId'])
                self.assertEqual((run['command'],run['status']),(command,'planned'))
                self.assertIn('中文任务',run['request'])
                self.assertIn('历史约束',run['request'])
        self.assertEqual(self.project.get_document('one').content,'作者的原稿。')
        self.assertEqual(len(self.project.list_versions('one')),1)
        self.assertEqual(self.project.get_scene('gate').plan_state,'approved')
        self.assertEqual(self.project.list_proposals(),[])

    async def test_scene_draft_requires_approved_plan_and_exact_creative_approval(self):
        scene=self.project.create_scene(slug='gate',title='城门',chapter='one')
        self.project.set_scene_plan(scene.document.id,'盘问后拒绝。')
        draft=await self.backend.dispatch('workflow/run',{'sessionId':self.key,'command':'scene-draft',
            'document':'gate','request':'写城门场景','dryRun':True})
        self.assertEqual((await self.finished(draft))['status'],'failed')
        async def approve(decision,change=False):
            run=await self.backend.dispatch('creative/apply',{'sessionId':self.key,'action':'approveScene','document':'gate'})
            for _ in range(100):
                if self.backend.approvals.pending:break
                await asyncio.sleep(.01)
            if change:self.project.update_scene_card('gate',goal='作者修改了目标')
            self.backend.approvals.resolve(next(iter(self.backend.approvals.pending)),{'decision':decision})
            return await self.finished(run)
        self.assertEqual((await approve('decline'))['status'],'cancelled')
        self.assertEqual(self.project.get_scene('gate').plan_state,'candidate')
        self.assertEqual((await approve('accept',change=True))['status'],'failed')
        self.assertEqual(self.project.get_scene('gate').plan_state,'candidate')
        self.assertEqual((await approve('accept'))['status'],'completed')
        self.assertEqual(self.project.get_scene('gate').plan_state,'approved')
        self.assertEqual(self.project.get_document('one').content,'作者的原稿。')

    async def test_scene_card_change_inside_approval_transaction_is_rejected(self):
        scene=self.project.create_scene(slug='gate',title='城门',chapter='one')
        self.project.set_scene_plan(scene.document.id,'盘问后拒绝。')
        preview=self.project.get_scene('gate')
        self.project.update_scene_card('gate',pov='作者指定视角')
        with self.assertRaises(ValueError):
            self.project.approve_scene('gate',expected_scene=preview)
        self.assertEqual(self.project.get_scene('gate').plan_state,'candidate')

    async def test_story_contract_rejects_wrong_options_and_missing_target_before_execution(self):
        base={'sessionId':self.key,'command':'revise','request':'修改','document':'one','revisionMode':'light'}
        for params in ({k:v for k,v in base.items() if k!='document'},
                {k:v for k,v in base.items() if k!='revisionMode'},
                {**base,'allowRaw':True},{**base,'noReference':True},{**base,'strategy':'single-pass'},
                {**base,'command':'scene-plan'},{**base,'command':'world'},
                {**base,'document':'../../another-book'}):
            with self.subTest(params=params),self.assertRaises((ValueError,jsonschema.ValidationError)):
                await self.backend.dispatch('workflow/run',params)
        self.assertEqual(self.backend.tasks,{})

    async def test_model_events_are_not_forwarded_into_public_workflow_history(self):
        control = self.backend.session(self.key)['control']
        async def fake(argv,**options):
            options['on_record']({'type':'model.event','data':{'engine_event':{'type':'reasoning','text':'PRIVATE_SENTINEL'}}})
            return 1
        control.session.run_command = fake
        response = await self.backend.dispatch('workflow/run',{'sessionId':self.key,'command':'world','request':'规划'})
        self.assertEqual((await self.finished(response))['status'],'failed')
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(self.events))
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(self.backend.journal.read(self.key)))

    async def test_real_native_story_workers_create_only_candidates_and_keep_selected_passage_scope(self):
        seen,arguments=[],[]
        class Provider(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_POST(self):
                value=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                seen.append(value)
                fields=value['text']['format']['schema']['properties']
                payload={'summary':'生成暂定结果','memory_proposals':[],'risks':[]}
                if 'plan_markdown' in fields:
                    payload.update(plan_markdown='先盘问身份，再拒绝进入。',card_updates={})
                elif 'draft_markdown' in fields:
                    payload.update(draft_markdown='林砚走到城门，守卫拦住了他。',continuity_notes=[],book_overview='林砚追寻星港真相，在城门与守卫交锋。')
                elif 'revision_markdown' in fields:
                    payload.update(revision_markdown='选中部分已修订。',change_log=['只修订指定段落'],book_overview='林砚追寻星港真相，在城门与守卫交锋。')
                else:
                    payload['memory_proposals']=[{'category':'world','key':'gate-rule','value':'守卫在城门核验身份。',
                        'tags':['gate'],'rationale':'暂定测试来源'}]
                event={'type':'response.completed','response':{'status':'completed',
                    'output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':json.dumps(payload,ensure_ascii=False)}]}],
                    'usage':{'input_tokens':10,'output_tokens':5}}}
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                self.wfile.write(('event: response.completed\ndata: '+json.dumps(event,ensure_ascii=False)+'\n\n').encode())
        server=ThreadingHTTPServer(('127.0.0.1',0),Provider)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        control=self.backend.session(self.key)['control']
        original=control.session.run_command
        source=f'''
import sys
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
import lg_cli.main as cli
from lg_cli.config import load_config
root=Path(sys.argv[sys.argv.index("-C")+1])
cfg=replace(load_config(root,environment="workflow-test-no-profile"),provider="openai",protocol="responses",
    api_key="test-key",default_model="synthetic-story",base_url="http://127.0.0.1:{server.server_port}")
cli._load_runtime()
with patch("lg_cli.main.load_config",return_value=cfg),patch("lg_cli.execution_adapter.CodexExecAdapter",side_effect=AssertionError("Native invoked Codex")):
    raise SystemExit(cli.main(sys.argv[1:]))
'''
        async def local(argv,**options):
            arguments.append(argv)
            return await original([sys.executable,'-c',source,*argv[4:]],**options)
        control.session.run_command=local
        scene=self.project.create_scene(slug='gate',title='城门',chapter='one')
        selected='SECRET_SELECTED_PASSAGE'
        self.project.create_version('one',content='未选中前文\n'+selected+'\n未选中后文')
        try:
            plan=await self.backend.dispatch('workflow/run',{'sessionId':self.key,'command':'scene-plan',
                'document':'gate','request':'只设计计划 AUTHOR_REQUEST_SENTINEL'})
            self.assertEqual((await self.finished(plan))['status'],'completed')
            self.assertEqual(self.project.get_scene('gate').plan_state,'candidate')
            approval=await self.backend.dispatch('creative/apply',{'sessionId':self.key,'action':'approveScene','document':'gate'})
            for _ in range(100):
                if self.backend.approvals.pending:break
                await asyncio.sleep(.01)
            self.backend.approvals.resolve(next(iter(self.backend.approvals.pending)),{'decision':'accept'})
            self.assertEqual((await self.finished(approval))['status'],'completed')
            for params in ({'command':'scene-draft','document':'gate','request':'只写城门正文'},
                    {'command':'revise','document':'one','request':'局部改写','revisionMode':'grammar','sourceVersion':2,'passage':selected},
                    {'command':'bible-curate','document':'one','request':'只提议事实'}):
                final=await self.finished(await self.backend.dispatch('workflow/run',{'sessionId':self.key,**params}))
                self.assertEqual(final['status'],'completed',final.get('error'))
            self.assertEqual(self.project.get_document('one').content,'作者的原稿。')
            self.assertEqual(self.project.get_document('gate').content,'')
            self.assertEqual(self.project.list_versions('one')[0].content,'未选中前文\n选中部分已修订。\n未选中后文')
            self.assertEqual(self.project.list_versions('gate')[0].state,'candidate')
            self.assertEqual(self.project.list_facts(state='canonical'),[])
            self.assertTrue(self.project.list_proposals())
            self.assertEqual(len(seen),4)
            self.assertIn(selected,json.dumps(seen,ensure_ascii=False))
            self.assertNotIn(selected,json.dumps(arguments))
            self.assertNotIn('AUTHOR_REQUEST_SENTINEL',json.dumps(arguments))
        finally:
            server.shutdown();server.server_close();worker.join(timeout=5)

    async def test_real_native_model_worker_receives_request_through_stdin(self):
        seen = []
        class Provider(BaseHTTPRequestHandler):
            def log_message(self,*_):
                pass
            def do_POST(self):
                value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                seen.append(value)
                text = json.dumps(specimen(value['text']['format']['schema']),ensure_ascii=False)
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                event = {'type':'response.completed','response':{'status':'completed',
                    'output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':text}]}],
                    'usage':{'input_tokens':10,'output_tokens':5}}}
                self.wfile.write(('event: response.completed\ndata: '+json.dumps(event,ensure_ascii=False)+'\n\n').encode())
                self.wfile.flush()
        server = ThreadingHTTPServer(('127.0.0.1',0),Provider)
        worker = threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        control = self.backend.session(self.key)['control']
        original = control.session.run_command
        arguments = []
        source = f'''
import sys
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
import lg_cli.main as cli
from lg_cli.config import load_config
root=Path(sys.argv[sys.argv.index("-C")+1])
cfg=replace(load_config(root,environment="workflow-test-no-profile"),provider="openai",protocol="responses",
    api_key="test-key",default_model="synthetic-workflow",base_url="http://127.0.0.1:{server.server_port}")
cli._load_runtime()
with patch("lg_cli.main.load_config",return_value=cfg),patch("lg_cli.execution_adapter.CodexExecAdapter",side_effect=AssertionError("Native invoked Codex")):
    raise SystemExit(cli.main(sys.argv[1:]))
'''
        async def local(argv,**options):
            arguments.append(argv)
            return await original([sys.executable,'-c',source,*argv[4:]],**options)
        control.session.run_command = local
        try:
            response = await self.backend.dispatch('workflow/run',{'sessionId':self.key,'command':'world',
                'request':'只设计世界：维修师为何留下？','noReference':True})
            final = await self.finished(response)
            self.assertEqual(final['status'],'completed',final.get('error'))
            self.assertEqual(len(seen),4)
            self.assertIn('维修师为何留下',seen[0]['input'][0]['content'])
            self.assertNotIn('维修师为何留下',' '.join(arguments[0]))
            self.assertNotIn('--conversation',arguments[0])
            self.assertEqual([record['role'] for record in self.backend.store.read(self.key)],['user','assistant'])
            self.assertEqual(self.project.get_document('one').content,'作者的原稿。')
            self.assertTrue(Path(final['result']['artifactPath']).is_relative_to(self.root/'ReferenceLibrary/bible'))
        finally:
            await asyncio.to_thread(server.shutdown);server.server_close();worker.join(timeout=2)
