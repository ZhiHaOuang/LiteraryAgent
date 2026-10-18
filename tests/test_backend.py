import asyncio
import json
import tempfile
import unittest
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from lg_cli.backend import Backend, ProcessHost
from lg_cli.conversation_store import ConversationStore
from lg_cli.project_store import ProjectStore
from lg_cli.session_runtime import SessionRuntime
from tests.helpers import make_config


class FakeAgent:
    def __init__(self, config, event, *, on_request=None, conversation=None, **kwargs):
        self.runtime = SessionRuntime(config.workspace, conversation)
        self.event, self.on_request = event, on_request
        self.thread_id = self.turn_id = None
        self.closed = False

    async def start(self, **kwargs):
        self.runtime.acquire()
        self.thread_id = self.runtime.read()['thread_id'] or 'persisted-thread'
        self.runtime.update(thread_id=self.thread_id)

    async def run(self, text, output_schema=None):
        self.turn_id = 'turn-1'
        if text == 'wait' or text.endswith('\n\nAuthor message:\nwait'):
            await asyncio.Future()
        if text == 'approve':
            reply = await self.on_request('item/commandExecution/requestApproval', {'command':'test'})
            text = json.dumps(reply)
        self.event('item/completed', {'item': {'type':'agentMessage','id':'msg','text':text}})
        self.turn_id = None
        return {'id':'turn-1','status':'completed'}

    async def steer(self, text):
        if not self.turn_id:
            raise ValueError('No active turn')

    async def interrupt(self):
        pass

    async def stop_execution(self):
        self.closed = True

    async def close(self):
        await self.stop_execution()
        self.runtime.release()


async def start_waiting_run(backend, key):
    response = await backend.run(key, 'wait')
    for _ in range(100):
        if backend.session(key).get('agent_run_id') == response['runId']:
            return response
        await asyncio.sleep(.01)
    raise AssertionError('Agent approval fixture did not start')


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ProjectStore(self.root).initialize(name='Backend test')
        self.events = []
        self.service = Backend(make_config(self.root), self.events.append, agent_factory=FakeAgent)
        self.key = (await self.service.dispatch('session/create', {}))['sessionId']

    async def asyncTearDown(self):
        await self.service.close()
        self.tmp.cleanup()

    async def finish(self, result):
        await self.service.tasks[result['runId']][1]
        return [e for e in self.events if e['method']=='run/completed'][-1]['params']

    async def test_persistent_identity_exclusive_ownership_and_events(self):
        result = await self.service.run(self.key, 'hello')
        await self.finish(result)
        foreign = SessionRuntime(self.root, self.key)
        with self.assertRaisesRegex(ValueError, 'in use'):
            foreign.acquire()
        await self.service.dispatch('session/close', {'sessionId':self.key})
        data = await self.service.open(self.key)
        self.assertEqual(data['thread_id'], 'persisted-thread')
        records = await self.service.dispatch('session/read', {'sessionId':self.key})
        self.assertEqual([m['role'] for m in records['messages']], ['user','assistant'])
        events = await self.service.dispatch('events/read', {'sessionId':self.key,'after':0})
        self.assertTrue(events['events'])
        cursor = events['events'][-1]['cursor']
        self.assertEqual(self.service.journal.read(self.key,cursor), [])

    async def test_analysis_worker_preserves_profile_runtime_and_refusal(self):
        ProjectStore(self.root).create_document(kind='chapter',slug='one',title='One',content='原稿')
        self.service.config = replace(self.service.config,environment='isolated-book',runtime_engine='native',
            runtime_manifest=self.root/'runtime.json')
        control = self.service.session(self.key)['control']
        commands = []
        async def run(argv,**kwargs):
            from lg_cli.book_analysis import BookAnalysisStore
            from lg_cli.run_store import RunStore
            commands.append(argv)
            project=ProjectStore(self.root)
            records=BookAnalysisStore(project);snapshot=records.snapshot('one')
            runs=RunStore(self.root)
            handle=runs.create(workflow='chapter-analysis',command='analysis',request='one',provider='synthetic',model='synthetic')
            report={'summary':'Checked source','entries':[],'findings':[]}
            records.save(snapshot,'Worldview',report,handle.run_id)
            runs.write_stage(handle,stage_id='Worldview',content=json.dumps(report),metadata={})
            runs.finalize(handle,status='completed')
            kwargs['on_record']({'type':'analysis.result','run_id':handle.run_id,'status':'completed','categories':['Worldview'],'blockers':[]})
            return 0
        control.run = run
        for decision in ('decline','accept'):
            result = await self.service.dispatch('workflow/start',{'sessionId':self.key,'chapter':'one','category':'Worldview'})
            for _ in range(100):
                if self.service.approvals.pending:
                    break
                await asyncio.sleep(.01)
            self.service.approvals.resolve(next(iter(self.service.approvals.pending)),{'decision':decision})
            finished = await self.finish(result)
            self.assertEqual(finished['status'],'cancelled' if decision=='decline' else 'completed')
            self.assertEqual(len(commands),0 if decision=='decline' else 1)
        argv = commands[0]
        self.assertEqual(argv[argv.index('--environment')+1],'isolated-book')
        self.assertEqual(argv[argv.index('--runtime-engine')+1],'native')
        self.assertEqual(argv[argv.index('--runtime-manifest')+1],str(self.root/'runtime.json'))

    async def test_structured_output_validated_and_external_refs_rejected(self):
        schema={'type':'object','properties':{'answer':{'type':'integer'}},'required':['answer'],'additionalProperties':False}
        result = await self.service.run(self.key, '{"answer": 3}', schema)
        self.assertEqual((await self.finish(result))['result']['output'], {'answer':3})
        result = await self.service.run(self.key, '{"answer": "wrong"}', schema)
        self.assertEqual((await self.finish(result))['status'], 'failed')
        with self.assertRaises(ValueError):
            await self.service.run(self.key, 'x', {'$ref':'https://example.com/schema'})

    async def test_approval_reply_and_cancel_while_waiting(self):
        result = await self.service.run(self.key, 'approve')
        await asyncio.sleep(.01)
        approval = next(iter(self.service.approvals.pending))
        self.service.approvals.resolve(approval, {'decision':'decline'})
        await self.finish(result)
        with self.assertRaises(ValueError):
            self.service.approvals.resolve(approval, {'decision':'accept'})
        result = await self.service.run(self.key, 'approve')
        await asyncio.sleep(.01)
        agent = self.service.sessions[self.key]['agent']
        await self.service.cancel(self.key)
        self.assertTrue(agent.closed)
        self.assertFalse(self.service.approvals.pending)
        self.assertEqual((await self.finish(result))['status'],'cancelled')

    async def test_creative_decline_accept_and_stale_preview(self):
        store=ProjectStore(self.root)
        fact=store.add_fact(category='world',key='gate',value='closed',state='idea')
        for decision in ('decline','accept'):
            result=await self.service.dispatch('creative/apply', {'sessionId':self.key,'action':'promoteFact','factId':fact.id})
            await asyncio.sleep(.01)
            self.service.approvals.resolve(next(iter(self.service.approvals.pending)), {'decision':decision})
            await self.finish(result)
            self.assertEqual(store.get_fact(fact.id).state, 'idea' if decision=='decline' else 'canonical')
        result=await self.service.dispatch('creative/apply', {'sessionId':self.key,'action':'promoteFact','factId':fact.id})
        await asyncio.sleep(.01)
        store.archive_fact(fact.id)
        self.service.approvals.resolve(next(iter(self.service.approvals.pending)), {'decision':'accept'})
        self.assertEqual((await self.finish(result))['status'],'failed')
        self.assertEqual(store.get_fact(fact.id).state,'archived')

    async def test_busy_permissions_and_cancel_preserves_history(self):
        await self.finish(await self.service.run(self.key,'completed'))
        result=await self.service.run(self.key,'wait')
        await asyncio.sleep(.01)
        with self.assertRaises(ValueError):
            await self.service.dispatch('permissions/set',{'sessionId':self.key,'mode':'full_access'})
        await self.service.cancel(self.key)
        self.assertIn('completed', [r['text'] for r in ConversationStore(self.root).read(self.key)])
        await self.service.dispatch('permissions/set',{'sessionId':self.key,'mode':'auto_review'})
        self.assertEqual(self.service.session(self.key)['agent'].runtime.read()['mode'],'auto_review')

    async def test_real_process_group_cancel_keeps_completed_file(self):
        output = self.root/'completed.txt'
        child = self.root/'child.pid'
        code = ("import subprocess,sys,time,pathlib; "
                "p=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)']); "
                "pathlib.Path(sys.argv[1]).write_text('finished'); "
                "pathlib.Path(sys.argv[2]).write_text(str(p.pid)); time.sleep(60)")
        host=ProcessHost(lambda *_:None)
        task=asyncio.create_task(host.run_command([sys.executable,'-c',code,str(output),str(child)]))
        for _ in range(200):
            if child.exists():
                break
            await asyncio.sleep(.01)
        self.assertTrue(child.exists())
        pid=int(child.read_text())
        await host.stop_workflow_process()
        await asyncio.wait_for(task,3)
        self.assertEqual(output.read_text(),'finished')
        # A reaped child disappears; an orphan can briefly remain as a zombie.
        for _ in range(100):
            stat=Path(f'/proc/{pid}/stat')
            if not stat.exists() or stat.read_text().split()[2]=='Z':
                break
            await asyncio.sleep(.01)
        else:
            self.fail('Child process survived cancellation')

    async def test_transport_runs_as_real_process_without_model_calls(self):
        messages=[{'jsonrpc':'2.0','id':1,'method':'initialize'},
                  {'jsonrpc':'2.0','id':2,'method':'session/list'},
                  {'jsonrpc':'2.0','id':3,'method':'reference/contracts'},
                  {'jsonrpc':'2.0','id':4,'method':'protocol/schema'}]
        env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]/'lg-cli'))
        result=await asyncio.to_thread(subprocess.run,
            [sys.executable,'-m','lg_cli.backend_stdio','--workspace',str(self.root)],
            input=''.join(json.dumps(m)+'\n' for m in messages), text=True, capture_output=True, env=env, timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        records=[json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([r['id'] for r in records], [1,2,3,4])
        self.assertEqual(records[0]['result']['protocolVersion'],'lg.backend.v1')
        self.assertEqual(len(records[2]['result']['categories']),5)

    async def test_transport_runtime_override_wins_over_project_config(self):
        config=self.root/'.literarygiant/config.toml'
        config.write_text('[runtime]\nengine = "native"\n')
        env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]/'lg-cli'))
        for engine in (None,'codex','native'):
            args=[sys.executable,'-m','lg_cli.backend_stdio','--workspace',str(self.root)]
            if engine:args+=['--runtime-engine',engine]
            result=await asyncio.to_thread(subprocess.run,args,
                input=json.dumps({'jsonrpc':'2.0','id':1,'method':'initialize'})+'\n',
                text=True,capture_output=True,env=env,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['result']['runtime'],engine or 'native')

    async def test_auto_creative_review_decline_and_failure_never_write(self):
        from unittest.mock import AsyncMock, patch
        store=ProjectStore(self.root)
        fact=store.add_fact(category='world',key='sun',value='blue',state='idea')
        await self.service.dispatch('permissions/set',{'sessionId':self.key,'mode':'ask','creativeMode':'auto_review'})
        for reply in ({'decision':'decline','reason':'Not supported'}, RuntimeError('review unavailable')):
            fake=AsyncMock(side_effect=reply) if isinstance(reply,Exception) else AsyncMock(return_value=reply)
            with patch('lg_cli.creative_review.review_creative',fake):
                result=await self.service.dispatch('creative/apply',{'sessionId':self.key,'action':'promoteFact','factId':fact.id})
                await self.finish(result)
            self.assertEqual(store.get_fact(fact.id).state,'idea')

    async def test_cancelling_session_stops_pending_operation_auto_review(self):
        from unittest.mock import patch
        from lg_cli.workflow_control import control_request
        started, stopped = asyncio.Event(), asyncio.Event()
        async def review(*args, **kwargs):
            started.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()
        await self.service.dispatch('permissions/set', {'sessionId':self.key,'mode':'auto_review'})
        await start_waiting_run(self.service,self.key)
        endpoint=await self.service.session(self.key)['control'].endpoint()
        with patch('lg_cli.creative_review.review_creative', review):
            request=asyncio.create_task(asyncio.to_thread(control_request,endpoint,'approval',details={'tool':'edit_manuscript'}))
            await asyncio.wait_for(started.wait(),2)
            await self.service.cancel(self.key)
            self.assertTrue(stopped.is_set())
            with self.assertRaises(ValueError):
                await request

    async def test_mcp_adoption_does_not_inherit_full_operation_permission(self):
        from lg_cli.workflow_control import control_request
        await self.service.dispatch('permissions/set', {
            'sessionId': self.key, 'mode': 'full_access', 'creativeMode': 'ask'})
        await start_waiting_run(self.service,self.key)
        endpoint = await self.service.session(self.key)['control'].endpoint()
        pending = asyncio.create_task(asyncio.to_thread(control_request, endpoint, 'approval',
            details={'tool': 'studio_accept', 'arguments': {'slug': 'one', 'version_number': 2}}))
        try:
            for _ in range(200):
                if self.service.approvals.pending:
                    break
                await asyncio.sleep(.01)
            self.assertTrue(self.service.approvals.pending)
            event = [e for e in self.events if e['method'] == 'approval/requested'][-1]
            self.assertEqual(event['params']['kind'], 'creative')
            self.service.approvals.resolve(event['params']['approvalId'], {'decision': 'decline'})
            self.assertEqual(await pending, {'decision': 'decline'})
            result = await asyncio.to_thread(control_request, endpoint, 'approval',
                details={'tool': 'studio_draft'})
            self.assertEqual(result['decision'], 'accept')
            self.assertEqual(result['generation_sources']['run_id'], self.service.session(self.key)['run_id'])
            self.assertEqual(result['generation_sources']['coverage'], 'unknown')
            self.assertEqual(result['generation_sources']['observations'], [])
        finally:
            await self.service.session(self.key)['control'].cancel_requests()
            await asyncio.gather(pending, return_exceptions=True)

    async def test_mcp_creative_auto_review_failure_never_grants_approval(self):
        from lg_cli.workflow_control import control_request
        from unittest.mock import AsyncMock, patch
        await self.service.dispatch('permissions/set', {
            'sessionId': self.key, 'mode': 'full_access', 'creativeMode': 'auto_review'})
        await start_waiting_run(self.service,self.key)
        endpoint = await self.service.session(self.key)['control'].endpoint()
        with patch('lg_cli.creative_review.review_creative', AsyncMock(return_value={
            'decision': 'decline', 'reason': '未授权设定变更'})):
            result = await asyncio.to_thread(control_request, endpoint, 'approval',
                details={'tool': 'studio_accept_proposal'})
            self.assertEqual(result['decision'], 'decline')
        with patch('lg_cli.creative_review.review_creative', AsyncMock(side_effect=ValueError('审查不可用'))):
            with self.assertRaisesRegex(ValueError, '审查不可用'):
                await asyncio.to_thread(control_request, endpoint, 'approval',
                    details={'tool': 'studio_adopt_reference'})
