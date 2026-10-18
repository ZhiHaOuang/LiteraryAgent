"""Actual run-record pages, live-process guards and controlled CLI recovery."""
import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

from lg_cli.backend import Backend
from lg_cli.project_store import ProjectStore
from lg_cli.run_store import RunStore
from lg_cli.terminal_backend import TerminalBackendClient
from tests.helpers import make_config
from tests.helpers import FakeAdapter
from lg_cli.workflow_runner import WorkflowRunner
from tests.test_execution_adapter import specimen
from tests.test_backend import FakeAgent


class BackendRunTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.root=Path(self.directory.name)
        self.project=ProjectStore(self.root)
        self.project.initialize(name='Run recovery')
        self.project.create_document(kind='chapter',slug='one',title='Original',content='Original prose.')
        self.store=RunStore(self.root)
        config=replace(make_config(self.root,api_key=None),runtime_engine='native',environment='run-test-no-profile')
        self.events=[]
        self.backend=Backend(config,self.events.append,agent_factory=FakeAgent)
        self.key=(await self.backend.dispatch('session/create',{}))['sessionId']

    async def asyncTearDown(self):
        await self.backend.close()
        self.directory.cleanup()

    def run_record(self,*,request='Original request',status='failed',command='outline'):
        handle=self.store.create(workflow='outline_workflow',command=command,request=request,provider='synthetic',model='synthetic')
        if status!='running':self.store.finalize(handle,status=status,error='PRIVATE_ERROR_CANARY' if status=='failed' else None)
        return handle

    async def finish(self,response):
        await self.backend.tasks[response['runId']][1]
        return next(e['params'] for e in reversed(self.events) if e['method']=='run/completed' and e['params']['runId']==response['runId'])

    def reader(self):
        client=TerminalBackendClient.__new__(TerminalBackendClient)
        client.backend,client.key=self.backend,self.key
        return client

    async def test_public_pages_keep_large_request_exact_without_private_model_events(self):
        request='中文🖋'*180000
        handle=self.run_record(request=request)
        raw=[{'sequence':1,'type':'model.event','message':'PRIVATE_REASONING_CANARY','data':{'private':'PRIVATE_TOOL_CANARY'}},
            {'sequence':2,'type':'stage.started','stage_id':'outline','message':'Public stage','data':{'private':'PRIVATE_NESTED_CANARY'}}]
        handle.events_path.write_text('\n'.join(json.dumps(e) for e in raw)+'\n')
        first=await self.backend.dispatch('run/readPage',{'sessionId':self.key,'runId':handle.run_id,'events':True})
        self.assertGreater(first['pages'],1)
        value=await self.reader().read_run(handle.run_id,events=True)
        self.assertEqual(value['run']['request'],request)
        self.assertEqual(len(value['events']),1)
        self.assertNotIn('PRIVATE_',json.dumps(value))
        self.assertNotIn('owner',value['run'])
        listing=await self.backend.dispatch('run/list',{})
        self.assertNotIn('request',listing['runs'][0])
        self.assertIsNone(self.backend.session(self.key)['agent'].thread_id)
        self.assertEqual(self.backend.tasks,{})

    async def test_frozen_run_pages_bind_both_session_and_run(self):
        handle=self.run_record(request='long request'*40000)
        other=self.run_record()
        first=await self.backend.dispatch('run/readPage',{'sessionId':self.key,'runId':handle.run_id})
        self.store.finalize(handle,status='completed')
        later=await self.backend.dispatch('run/readPage',{'sessionId':self.key,'runId':handle.run_id,
            'snapshotId':first['snapshotId'],'page':1})
        self.assertEqual(later['sha256'],first['sha256'])
        with self.assertRaises(ValueError):
            await self.backend.dispatch('run/readPage',{'sessionId':self.key,'runId':other.run_id,
                'snapshotId':first['snapshotId'],'page':1})

    async def test_live_owner_cannot_resume_even_with_legacy_confirmation(self):
        handle=self.run_record(status='running')
        for confirmed in (False,True):
            with self.assertRaisesRegex(ValueError,'still executing'):
                await self.backend.dispatch('workflow/resume',{'sessionId':self.key,'runId':handle.run_id,
                    'dryRun':True,'confirmStopped':confirmed})
        self.assertEqual(self.backend.tasks,{})

    async def test_two_processes_cannot_resume_same_failed_run(self):
        handle=self.run_record()
        source='from pathlib import Path\nimport sys,time\nfrom lg_cli.run_store import RunStore\ns=RunStore(Path(sys.argv[1]))\nwith s.resume_lock(sys.argv[2]):\n print("locked",flush=True)\n time.sleep(60)\n'
        process=await asyncio.create_subprocess_exec(sys.executable,'-c',source,str(self.root),handle.run_id,stdout=asyncio.subprocess.PIPE)
        try:
            self.assertEqual((await process.stdout.readline()).decode().strip(),'locked')
            response=await self.backend.dispatch('workflow/resume',{'sessionId':self.key,'runId':handle.run_id,'dryRun':True})
            final=await self.finish(response)
            self.assertEqual(final['status'],'failed')
            self.assertEqual(len(self.store.list_runs()),1)
        finally:
            process.terminate();await process.wait()
        response=await self.backend.dispatch('workflow/resume',{'sessionId':self.key,'runId':handle.run_id,'dryRun':True})
        self.assertEqual((await self.finish(response))['status'],'completed')

    async def test_legacy_owner_needs_explicit_stopped_confirmation(self):
        handle=self.run_record(status='running')
        value=self.store.load(handle.run_id);value.pop('owner')
        handle.manifest_path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'confirm-stopped'):
            await self.backend.dispatch('workflow/resume',{'sessionId':self.key,'runId':handle.run_id,'dryRun':True})
        response=await self.backend.dispatch('workflow/resume',{'sessionId':self.key,'runId':handle.run_id,
            'dryRun':True,'confirmStopped':True})
        self.assertEqual((await self.finish(response))['status'],'completed')

    async def test_completed_run_does_not_create_another_model_run(self):
        handle=self.run_record(status='completed')
        response=await self.backend.dispatch('workflow/resume',{'sessionId':self.key,'runId':handle.run_id})
        final=await self.finish(response)
        self.assertEqual(final['status'],'completed',final.get('error'))
        self.assertEqual(final['result']['workflowRunId'],handle.run_id)
        self.assertEqual(len(self.store.list_runs()),1)
        self.assertIn('nothing to resume',self.backend.store.read(self.key)[-1]['text'])

    async def test_real_interrupted_process_resumes_original_request_without_codex(self):
        source='from pathlib import Path\nimport sys,time\nfrom lg_cli.run_store import RunStore\ns=RunStore(Path(sys.argv[1]))\nh=s.create(workflow="outline_workflow",command="outline",request="Saved original request",provider="synthetic",model="synthetic")\nprint(h.run_id,flush=True)\ntime.sleep(60)\n'
        process=await asyncio.create_subprocess_exec(sys.executable,'-c',source,str(self.root),stdout=asyncio.subprocess.PIPE)
        try:
            run=(await process.stdout.readline()).decode().strip()
            with self.assertRaisesRegex(ValueError,'still executing'):
                await self.backend.dispatch('workflow/resume',{'sessionId':self.key,'runId':run,'dryRun':True})
        finally:
            process.terminate();await process.wait()
        self.backend.store.append(self.key,'user','Later conversation must not replace original request.')
        response=await self.backend.dispatch('workflow/resume',{'sessionId':self.key,'runId':run,'dryRun':True})
        final=await self.finish(response)
        self.assertEqual(final['status'],'completed',final.get('error'))
        restored=self.store.load(final['result']['workflowRunId'])
        self.assertEqual((restored['request'],restored['resumed_from']),('Saved original request',run))
        self.assertEqual(self.project.get_document('one').content,'Original prose.')

    async def test_manifest_change_after_selection_cannot_switch_to_code(self):
        handle=self.run_record()
        control=self.backend.session(self.key)['control']
        original=control.session.run_command
        async def changed(argv,**options):
            value=self.store.load(handle.run_id);value['command']='code'
            handle.manifest_path.write_text(json.dumps(value))
            return await original(argv,**options)
        control.session.run_command=changed
        response=await self.backend.dispatch('workflow/resume',{'sessionId':self.key,'runId':handle.run_id,'dryRun':True})
        self.assertEqual((await self.finish(response))['status'],'failed')
        self.assertEqual(len(self.store.list_runs()),1)

    async def test_unsupported_workflow_and_external_record_are_rejected(self):
        for command in ('code','scene-draft','revise','analysis'):
            handle=self.run_record(command=command)
            with self.assertRaises(ValueError):
                await self.backend.dispatch('workflow/resume',{'sessionId':self.key,'runId':handle.run_id,'dryRun':True})
        with tempfile.TemporaryDirectory() as raw:
            outside=Path(raw);(outside/'run.json').write_text('PRIVATE_CANARY')
            (self.store.root/'linked').symlink_to(outside,target_is_directory=True)
            with self.assertRaises(ValueError):
                await self.backend.dispatch('run/readPage',{'sessionId':self.key,'runId':'linked'})

    async def test_native_resume_preserves_verified_prefix_and_generates_only_missing_stages(self):
        seen=[]
        class Provider(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_POST(self):
                value=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                seen.append(value)
                payload=specimen(value['text']['format']['schema'])
                event={'type':'response.completed','response':{'status':'completed',
                    'output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':json.dumps(payload)}]}],
                    'usage':{'input_tokens':10,'output_tokens':5}}}
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                self.wfile.write(('event: response.completed\ndata: '+json.dumps(event)+'\n\n').encode())
        server=ThreadingHTTPServer(('127.0.0.1',0),Provider)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        config=replace(self.backend.config,provider='openai',protocol='responses',api_key='test-placeholder',
            default_model='synthetic-resume',base_url=f'http://127.0.0.1:{server.server_port}')
        failed=WorkflowRunner(config,adapter=FakeAdapter(fail_at=3)).run('outline','Original novel request')
        control=self.backend.session(self.key)['control']
        original=control.session.run_command
        project_root=Path(__file__).resolve().parents[1]
        source=f'''
import sys
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
sys.path.insert(0,{str(project_root)!r})
from tests.helpers import make_config
import lg_cli.main as cli
root=Path(sys.argv[sys.argv.index("-C")+1])
cfg=replace(make_config(root),runtime_engine="native",environment="run-test-no-profile",provider="openai",protocol="responses",
    api_key="test-placeholder",default_model="synthetic-resume",base_url="http://127.0.0.1:{server.server_port}")
cli._load_runtime()
with patch("lg_cli.main.load_config",return_value=cfg),patch("lg_cli.execution_adapter.CodexExecAdapter",side_effect=AssertionError("Native invoked Codex")):
    raise SystemExit(cli.main(sys.argv[1:]))
'''
        async def local(argv,**options):
            return await original([sys.executable,'-c',source,*argv[4:]],**options)
        control.session.run_command=local
        try:
            response=await self.backend.dispatch('workflow/resume',{'sessionId':self.key,'runId':failed.run_id})
            final=await self.finish(response)
            self.assertEqual(final['status'],'completed',final.get('error'))
            self.assertEqual(len(seen),5)
            restored=self.store.load(final['result']['workflowRunId'])
            self.assertEqual(restored['resumed_from'],failed.run_id)
            self.assertEqual(len(restored['completed_stages']),7)
            self.assertEqual(self.project.get_document('one').content,'Original prose.')
        finally:
            server.shutdown();server.server_close();worker.join(timeout=5)
