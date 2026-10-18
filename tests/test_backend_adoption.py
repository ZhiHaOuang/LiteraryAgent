"""Author-bound run import and cancellable candidate supervision via the product service."""
import asyncio
import json
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import patch

import jsonschema
from lg_cli.backend import Backend
from lg_cli.book_analysis import BookAnalysisStore
from lg_cli.project_store import ProjectStore
from lg_cli.run_store import RunStore
from lg_cli.supervision import configure_supervision,DEFAULT_CATEGORIES
from tests.helpers import make_config
from tests.test_backend import FakeAgent


class BackendAdoptionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.root=Path(self.directory.name)
        self.project=ProjectStore(self.root)
        self.project.initialize(name='Author adoption')
        self.document=self.project.create_document(kind='chapter',slug='one',title='One',content='Author original.',state='accepted')
        self.config=replace(make_config(self.root,api_key=None),runtime_engine='native',
            environment='adoption-test-no-profile',enable_reference=False)
        self.events=[]
        self.backend=Backend(self.config,self.events.append,agent_factory=FakeAgent)
        self.key=(await self.backend.dispatch('session/create',{}))['sessionId']

    async def asyncTearDown(self):
        await self.backend.close()
        self.directory.cleanup()

    async def finish(self,response):
        await self.backend.tasks[response['runId']][1]
        return next(event['params'] for event in reversed(self.events)
            if event['method']=='run/completed' and event['params']['runId']==response['runId'])

    async def decide(self,decision):
        for _ in range(300):
            if self.backend.approvals.pending:break
            await asyncio.sleep(.01)
        self.assertTrue(self.backend.approvals.pending)
        key=next(iter(self.backend.approvals.pending))
        shown=next(e['params'] for e in reversed(self.events)
            if e['method']=='approval/requested' and e['params']['approvalId']==key)
        self.backend.approvals.resolve(key,{'decision':decision})
        return shown

    def saved_run(self):
        runs=RunStore(self.root)
        handle=runs.create(workflow='writing',command='write',request='Write one candidate.',provider='synthetic',model='synthetic')
        artifact=self.root/'ReferenceLibrary'/'drafts'/'import.md'
        artifact.parent.mkdir(parents=True,exist_ok=True)
        artifact.write_text('Saved candidate prose.')
        runs.finalize(handle,status='completed',artifact_path=artifact)
        return handle.run_id,artifact

    async def import_run(self,run):
        response=await self.backend.dispatch('run/import',{'sessionId':self.key,'runId':run,
            'slug':'one','kind':'chapter','title':'One'})
        return await self.finish(response)

    def reviews(self,version,*,final=False):
        configure_supervision(self.project)
        records=BookAnalysisStore(self.project)
        base=records.snapshot('one')
        snapshot=records.candidate_snapshot('one',version,final=final)
        for category in DEFAULT_CATEGORIES:
            records.save(snapshot,category,{'summary':'Checked source.','entries':[],'findings':[]},
                'saved-review',candidate_id=version.id,base_snapshot=base)

    async def test_import_keeps_original_reuses_same_version_and_rejects_changed_artifact(self):
        run,artifact=self.saved_run()
        first=await self.import_run(run)
        self.assertEqual(first['status'],'completed')
        self.assertFalse(first['result']['active'])
        again=await self.import_run(run)
        self.assertEqual(first['result'],again['result'])
        version=self.project.get_version('one',first['result']['version'])
        self.assertEqual(version.state,'candidate')
        self.assertEqual(version.metadata['base_version_id'],self.document.active_version_id)
        artifact.write_text('Unreviewed later content.')
        self.assertEqual((await self.import_run(run))['status'],'failed')
        self.assertEqual(self.project.get_document('one').content,'Author original.')
        self.assertEqual(len(self.project.list_versions('one')),2)

    async def test_import_rejects_outside_manifest_and_cannot_include_accept(self):
        run,_=self.saved_run()
        params={'sessionId':self.key,'runId':run,'slug':'one','kind':'chapter','title':'One','accept':True}
        with self.assertRaises(jsonschema.ValidationError):
            await self.backend.dispatch('run/import',params)
        with tempfile.TemporaryDirectory() as raw:
            outside=Path(raw)
            (outside/'run.json').write_text('PRIVATE_SENTINEL')
            (RunStore(self.root).root/'linked').symlink_to(outside,target_is_directory=True)
            final=await self.import_run('linked')
            self.assertEqual(final['status'],'failed')
            self.assertNotIn('PRIVATE_SENTINEL',json.dumps(self.events))
        self.assertEqual(len(self.project.list_versions('one')),1)

    async def test_decline_never_starts_review_and_preserves_imported_candidate(self):
        run,_=self.saved_run()
        imported=(await self.import_run(run))['result']
        with patch.object(self.backend,'review_acceptance',side_effect=AssertionError('Decline started review')):
            response=await self.backend.dispatch('creative/apply',{'sessionId':self.key,'action':'acceptVersion',
                'document':'one','version':imported['version'],'review':True})
            shown=await self.decide('decline')
            self.assertIn('Saved candidate prose.',json.dumps(shown))
            self.assertEqual((await self.finish(response))['status'],'cancelled')
        self.assertEqual(self.project.get_document('one').content,'Author original.')

    async def test_real_review_worker_reuses_current_reviews_and_preserves_final_semantics(self):
        version=self.project.create_version('one',content='Checked final candidate.',expected_version_id=self.document.active_version_id)
        self.reviews(version,final=True)
        response=await self.backend.dispatch('creative/apply',{'sessionId':self.key,'action':'acceptVersion',
            'document':'one','version':version.version_number,'review':True,'final':True})
        shown=await self.decide('accept')
        self.assertTrue(shown['details']['final'])
        final=await self.finish(response)
        self.assertEqual(final['status'],'completed',final.get('error'))
        document=self.project.get_document('one')
        self.assertEqual((document.content,document.state),('Checked final candidate.','final'))
        self.assertEqual(len(BookAnalysisStore(self.project).list()),len(DEFAULT_CATEGORIES))
        self.assertEqual(self.backend.session(self.key)['control'].status()['status'],'completed')

    async def test_book_change_while_waiting_invalidates_author_approval(self):
        version=self.project.create_version('one',content='Candidate prose.')
        response=await self.backend.dispatch('creative/apply',{'sessionId':self.key,'action':'acceptVersion',
            'document':'one','version':version.version_number})
        for _ in range(100):
            if self.backend.approvals.pending:break
            await asyncio.sleep(.01)
        self.project.add_fact(category='world',key='new-rule',value='New Canonical rule.',state='canonical')
        await self.decide('accept')
        self.assertEqual((await self.finish(response))['status'],'failed')
        self.assertEqual(self.project.get_document('one').content,'Author original.')

    async def test_request_mutation_cannot_change_the_approved_version(self):
        first=self.project.create_version('one',content='First candidate.')
        other=self.project.create_version('one',content='Other candidate.')
        params={'sessionId':self.key,'action':'acceptVersion','document':'one','version':first.version_number}
        response=await self.backend.dispatch('creative/apply',params)
        params['version']=other.version_number
        await self.decide('accept')
        self.assertEqual((await self.finish(response))['status'],'completed')
        self.assertEqual(self.project.get_document('one').content,'First candidate.')

    async def test_conflicting_saved_review_blocks_even_full_creative_access(self):
        fact=self.project.add_fact(category='world',key='gate',value='Gate never opens.',state='canonical')
        version=self.project.create_version('one',content='Gate opens.')
        self.reviews(version)
        records=BookAnalysisStore(self.project)
        snapshot=records.candidate_snapshot('one',version)
        report={'summary':'Evidenced conflict.','entries':[],'findings':[{'kind':'canon_conflict',
            'explanation':'The gate opens against the author rule.',
            'evidence':[{'document_id':self.document.id,'version_id':version.id,'quote':'Gate opens.'}],
            'fact_id':fact.id,'fact_quote':fact.value}]}
        records.save(snapshot,'Worldview',report,'conflict-review',candidate_id=version.id,base_snapshot=records.snapshot('one'))
        self.backend.session(self.key)['agent'].runtime.update(creative_mode='full_access')
        response=await self.backend.dispatch('creative/apply',{'sessionId':self.key,'action':'acceptVersion',
            'document':'one','version':version.version_number,'review':True})
        self.assertEqual((await self.finish(response))['status'],'failed')
        self.assertEqual(self.project.get_document('one').content,'Author original.')

    async def test_book_change_during_worker_review_invalidates_approval(self):
        version=self.project.create_version('one',content='Candidate prose.')
        control=self.backend.session(self.key)['control']
        async def changed(argv,**options):
            self.project.add_fact(category='world',key='new-rule',value='Changed during review.',state='canonical')
            options['on_record']({'type':'acceptance.reviewed','documentId':self.document.id,'versionId':version.id,'final':False})
            return 0
        control.session.run_command=changed
        response=await self.backend.dispatch('creative/apply',{'sessionId':self.key,'action':'acceptVersion',
            'document':'one','version':version.version_number,'review':True})
        await self.decide('accept')
        self.assertEqual((await self.finish(response))['status'],'failed')
        self.assertEqual(self.project.get_document('one').content,'Author original.')

    async def test_failed_or_wrong_target_review_cannot_adopt_or_replay_without_fresh_approval(self):
        version=self.project.create_version('one',content='Candidate prose.')
        control=self.backend.session(self.key)['control']
        async def failed(argv,**options):return 1
        control.session.run_command=failed
        response=await self.backend.dispatch('creative/apply',{'sessionId':self.key,'action':'acceptVersion',
            'document':'one','version':version.version_number,'review':True})
        await self.decide('accept')
        self.assertEqual((await self.finish(response))['status'],'failed')
        with self.assertRaisesRegex(ValueError,'fresh creative approval'):
            await control.retry(control.status()['id'])
        self.assertEqual(control.status()['attempt'],1)
        async def wrong(argv,**options):
            options['on_record']({'type':'acceptance.reviewed','documentId':self.document.id,'versionId':9999,'final':False})
            return 0
        control.session.run_command=wrong
        response=await self.backend.dispatch('creative/apply',{'sessionId':self.key,'action':'acceptVersion',
            'document':'one','version':version.version_number,'review':True})
        await self.decide('accept')
        self.assertEqual((await self.finish(response))['status'],'failed')
        self.assertEqual(self.project.get_document('one').content,'Author original.')

    async def test_cancel_owned_review_stops_process_and_retains_finished_report(self):
        version=self.project.create_version('one',content='Candidate prose.')
        control=self.backend.session(self.key)['control']
        original=control.session.run_command
        report=self.root/'completed-review.txt'
        async def held(argv,**options):
            return await original([sys.executable,'-c',
                'import pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text("completed");time.sleep(60)',str(report)])
        control.session.run_command=held
        response=await self.backend.dispatch('creative/apply',{'sessionId':self.key,'action':'acceptVersion',
            'document':'one','version':version.version_number,'review':True})
        await self.decide('accept')
        for _ in range(300):
            if report.exists():break
            await asyncio.sleep(.01)
        self.assertTrue(report.exists())
        process=control.session.process
        await self.backend.cancel(self.key)
        self.assertEqual((await self.finish(response))['status'],'cancelled')
        self.assertIsNotNone(process.returncode)
        self.assertEqual(report.read_text(),'completed')
        self.assertEqual(self.project.get_document('one').content,'Author original.')

    async def test_real_native_missing_reviews_are_generated_before_activation(self):
        seen=[]
        class Provider(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_POST(self):
                value=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                seen.append(value)
                payload={'summary':'Checked candidate source.','entries':[],'findings':[]}
                event={'type':'response.completed','response':{'status':'completed',
                    'output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':json.dumps(payload)}]}],
                    'usage':{'input_tokens':10,'output_tokens':5}}}
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                self.wfile.write(('event: response.completed\ndata: '+json.dumps(event)+'\n\n').encode())
        server=ThreadingHTTPServer(('127.0.0.1',0),Provider)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        configure_supervision(self.project)
        version=self.project.create_version('one',content='Fresh checked candidate.',expected_version_id=self.document.active_version_id)
        control=self.backend.session(self.key)['control']
        original=control.session.run_command
        source=f'''
import sys
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
import lg_cli.acceptance_review as review
from lg_cli.config import load_config
root=Path(sys.argv[sys.argv.index("-C")+1])
cfg=replace(load_config(root,environment="adoption-test-no-profile"),runtime_engine="native",provider="openai",
    protocol="responses",api_key="test-placeholder",default_model="synthetic-review",
    base_url="http://127.0.0.1:{server.server_port}",enable_reference=False)
with patch("lg_cli.acceptance_review.load_config",return_value=cfg),patch("lg_cli.execution_adapter.CodexExecAdapter",side_effect=AssertionError("Native invoked Codex")):
    raise SystemExit(review.main(sys.argv[1:]))
'''
        async def local(argv,**options):
            self.assertEqual(self.project.get_document('one').content,'Author original.')
            return await original([sys.executable,'-c',source,*argv[4:]],**options)
        control.session.run_command=local
        try:
            response=await self.backend.dispatch('creative/apply',{'sessionId':self.key,'action':'acceptVersion',
                'document':'one','version':version.version_number,'review':True})
            await self.decide('accept')
            final=await self.finish(response)
            self.assertEqual(final['status'],'completed',final.get('error'))
            self.assertEqual(len(seen),len(DEFAULT_CATEGORIES))
            self.assertTrue(all(not value.get('tools') for value in seen))
            self.assertEqual(self.project.get_document('one').content,'Fresh checked candidate.')
            reports=BookAnalysisStore(self.project).list()
            self.assertEqual(len(reports),len(DEFAULT_CATEGORIES))
            self.assertTrue(all(report['status']=='current' for report in reports))
        finally:
            server.shutdown();server.server_close();worker.join(timeout=5)
