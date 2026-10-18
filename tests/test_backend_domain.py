import asyncio
import json
import tempfile
import unittest
from dataclasses import replace,asdict
from pathlib import Path

from lg_cli.backend import Backend
from lg_cli.book_analysis import ChapterAnalysisService,BookAnalysisStore
from lg_cli.core_adapter import CoreExecutionResult
from lg_cli.project_store import ProjectStore
from lg_cli.task_scope import bind_task
from tests.helpers import make_config
from tests.test_backend import FakeAgent, start_waiting_run


class DomainBackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.project=ProjectStore(self.root);self.project.initialize(name='Domain service')
        self.original=self.project.create_document(kind='chapter',slug='one',title='One',content='作者原稿。',state='accepted')
        self.config=replace(make_config(self.root),runtime_engine='native',enable_reference=False,max_stage_context_chars=200000)
        self.events=[];self.backend=Backend(self.config,self.events.append,agent_factory=FakeAgent)
        self.key=(await self.backend.dispatch('session/create',{}))['sessionId']

    async def asyncTearDown(self):
        await self.backend.close();self.tmp.cleanup()

    async def pending(self):
        for _ in range(100):
            if self.backend.approvals.pending:
                return next(event['params'] for event in reversed(self.events) if event['method']=='approval/requested')
            await asyncio.sleep(.01)
        self.fail('No approval request')

    async def finish(self,response):
        await self.backend.tasks[response['runId']][1]
        return next(e['params'] for e in reversed(self.events) if e['method']=='run/completed' and e['params']['runId']==response['runId'])

    def draft(self):
        return {'operation':'draft','arguments':{'slug':'one','title':'One','content':'待确认的修订。','sequence':1,
            'expected_version_id':self.original.active_version_id,'reason':'作者指定输入'}}

    async def test_studio_read_does_not_start_runtime_and_invalid_calls_never_write(self):
        await self.backend.dispatch('studio/call',{'operation':'status','arguments':{}})
        self.assertEqual(self.backend.tasks,{})
        for params in ({'operation':'shell','arguments':{}}, {'operation':'draft','arguments':{}},
                {'operation':'status','arguments':{'author_action':True}}):
            with self.subTest(params=params),self.assertRaises(Exception):
                await self.backend.dispatch('studio/call',params)
        self.assertEqual(len(self.project.list_versions('one')),1)

    async def test_studio_candidate_uses_operation_approval_and_keeps_active_text(self):
        params=self.draft();preview=await self.backend.dispatch('studio/preview',params)
        response=await self.backend.dispatch('studio/call',{'sessionId':self.key,**params,'expectedFingerprint':preview['fingerprint']})
        shown=await self.pending();self.assertEqual(shown['kind'],'operation')
        self.assertEqual(shown['details']['arguments']['content'],'待确认的修订。')
        self.backend.approvals.resolve(shown['approvalId'],{'decision':'accept'})
        final=await self.finish(response)
        self.assertEqual(final['status'],'completed',final.get('error'))
        self.assertFalse(final['result']['value']['diff']['applied'])
        self.assertEqual(self.project.get_document('one').content,'作者原稿。')
        self.assertEqual([row['role'] for row in self.backend.store.read(self.key)],['user','assistant'])

    async def test_studio_decline_cancel_and_stale_preview_cannot_write(self):
        for cancel in (False,True):
            response=await self.backend.dispatch('studio/call',{'sessionId':self.key,**self.draft()})
            shown=await self.pending()
            if cancel:await self.backend.cancel(self.key)
            else:self.backend.approvals.resolve(shown['approvalId'],{'decision':'decline'})
            self.assertEqual((await self.finish(response))['status'],'cancelled')
        self.assertEqual(len(self.project.list_versions('one')),1)
        params=self.draft();preview=await self.backend.dispatch('studio/preview',params)
        self.project.edit_active_document('one',content='作者刚更新。',expected_version_id=self.original.active_version_id,reason='author')
        with self.assertRaisesRegex(ValueError,'preview changed'):
            await self.backend.dispatch('studio/call',{'sessionId':self.key,**params,'expectedFingerprint':preview['fingerprint']})

    async def test_studio_creative_approval_is_separate_and_changes_during_approval_fail(self):
        document=self.project.create_document(kind='character',slug='person',title='Person',content='候选人物',state='candidate')
        params={'sessionId':self.key,'operation':'adopt-reference','arguments':{'slug':'person','version_number':1,
            'expected_version_id':document.active_version_id}}
        self.backend.session(self.key)['agent'].runtime.update(mode='full_access')
        response=await self.backend.dispatch('studio/call',params)
        shown=await self.pending();self.assertEqual(shown['kind'],'creative')
        self.project.add_fact(category='world',key='new',value='作者新增约束',state='idea')
        self.backend.approvals.resolve(shown['approvalId'],{'decision':'accept'})
        self.assertEqual((await self.finish(response))['status'],'failed')
        self.assertEqual(self.project.get_document('person').state,'candidate')

    async def test_model_details_cannot_claim_an_explicit_author_action(self):
        state=self.backend.session(self.key)
        state['agent'].runtime.update(task=bind_task(self.root,{'kind':'discussion'}),mode='full_access')
        await start_waiting_run(self.backend,self.key)
        with self.assertRaisesRegex(ValueError,'outside the author task'):
            await state['control'].approval_handler({'tool':'studio_draft','arguments':self.draft()['arguments'],'author_action':True})

    def fake_worker(self,*,missing=False):
        class Adapter:
            def run(self,**_):
                return CoreExecutionResult(True,False,json.dumps({'summary':'已核对来源','entries':[],'findings':[]}),None,'test',[],0,'','',.01)
        async def run(argv,**options):
            if missing:return 0
            from lg_cli.main import build_parser
            args=build_parser().parse_args(argv[4:])
            value=ChapterAnalysisService(self.config,adapter=Adapter()).analyze(args.reference,categories=args.category,
                volume=args.analysis_command=='volume',expected_fingerprint=args.expected_source_fingerprint,
                on_event=lambda event:options['on_record'](json.loads(json.dumps(asdict(event)))))
            options['on_record']({'type':'analysis.result',**value})
            return 0
        self.backend.session(self.key)['control'].session.run_command=run

    async def test_analysis_saved_reports_are_verified_and_zero_exit_is_insufficient(self):
        self.fake_worker(missing=True)
        params={'sessionId':self.key,'chapter':'one','categories':['Worldview','EventsLibrary']}
        first=await self.backend.dispatch('workflow/start',params)
        shown=await self.pending();self.backend.approvals.resolve(shown['approvalId'],{'decision':'accept'})
        self.assertEqual((await self.finish(first))['status'],'failed')
        self.fake_worker()
        response=await self.backend.dispatch('workflow/start',params)
        shown=await self.pending();self.backend.approvals.resolve(shown['approvalId'],{'decision':'accept'})
        final=await self.finish(response)
        self.assertEqual(final['status'],'completed',final.get('error'))
        self.assertEqual(final['result']['analysisResult']['categories'],params['categories'])
        self.assertEqual(len(BookAnalysisStore(self.project).list('one')),2)
        self.assertEqual([row['role'] for row in self.backend.store.read(self.key)],['user','user','assistant'])

    async def test_analysis_source_change_while_waiting_never_starts_worker(self):
        async def forbidden(*_,**__):self.fail('Stale source started a worker')
        self.backend.session(self.key)['control'].session.run_command=forbidden
        response=await self.backend.dispatch('workflow/start',{'sessionId':self.key,'chapter':'one','category':'Worldview'})
        shown=await self.pending()
        self.project.edit_active_document('one',content='作者改稿。',expected_version_id=self.original.active_version_id,reason='author')
        self.backend.approvals.resolve(shown['approvalId'],{'decision':'accept'})
        self.assertEqual((await self.finish(response))['status'],'failed')

    async def test_volume_analysis_and_preview_selection_are_bound(self):
        self.fake_worker()
        params={'chapter':'__book__','volume':True,'categories':['Worldview']}
        preview=await self.backend.dispatch('analysis/preview',params)
        response=await self.backend.dispatch('workflow/start',{'sessionId':self.key,**params,'expectedFingerprint':preview['fingerprint']})
        shown=await self.pending();self.backend.approvals.resolve(shown['approvalId'],{'decision':'accept'})
        final=await self.finish(response)
        self.assertEqual(final['status'],'completed',final.get('error'))
        self.assertEqual(BookAnalysisStore(self.project).list('__book__',volume=True)[0]['scope'],'volume')
