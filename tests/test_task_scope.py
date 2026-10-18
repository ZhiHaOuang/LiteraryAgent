import asyncio
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from lg_cli.backend import Backend
from lg_cli.backend_protocol import validate
from lg_cli.project_store import ProjectStore
from lg_cli.task_scope import bind_task,check_operation,stage_task
from tests.helpers import make_config
from tests.test_backend import FakeAgent, start_waiting_run
from tests.test_native_runtime import answer


class TaskScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=ProjectStore(self.root);self.store.initialize(name='Task scope')
        self.one=self.store.create_document(kind='chapter',slug='one',title='First',content='前文。选中的对白。后文。',state='accepted',sequence=1)
        self.two=self.store.create_document(kind='chapter',slug='two',title='Second',content='第二章正文。',state='accepted',sequence=2)

    def test_planning_rejects_all_prose_adoption_and_wrong_stage_profile_writes(self):
        scope=bind_task(self.root,{'kind':'planning','stage':'world'})
        for tool in ('create_chapter','edit_manuscript','studio_draft','restore_manuscript','studio_accept','studio_accept_proposal'):
            with self.subTest(tool=tool),self.assertRaisesRegex(ValueError,'outside'):
                check_operation(self.root,scope,{'tool':tool,'arguments':{}})
        for arguments in ({'stage':'plan','updates':{}},{'stage':'world','updates':{'volumes':['not authorized']}}):
            with self.assertRaisesRegex(ValueError,'outside'):
                check_operation(self.root,scope,{'tool':'studio_save','arguments':arguments})
        self.assertEqual(check_operation(self.root,scope,{'tool':'studio_save','arguments':{'stage':'world','updates':{'characters':['提案人物']}}}),'grants')

    def test_revision_preserves_prefix_suffix_and_exact_document_and_version(self):
        scope=bind_task(self.root,{'kind':'revision','document':'one','sourceVersion':self.one.active_version_id,'passage':'选中的对白。'})
        arguments={'slug':'one','expected_version_id':self.one.active_version_id,'content':'前文。新的对白。后文。'}
        self.assertEqual(check_operation(self.root,scope,{'tool':'edit_manuscript','arguments':arguments}),'grants')
        for change in ({'slug':'two'},{'expected_version_id':999},{'content':'前文也改了。新的对白。后文。'},{'content':'前文。新的对白。结尾也改了。'}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                check_operation(self.root,scope,{'tool':'edit_manuscript','arguments':{**arguments,**change}})
        self.store.edit_active_document('one',content='作者保存了新正文。',expected_version_id=self.one.active_version_id,reason='Author edit')
        with self.assertRaisesRegex(ValueError,'source version changed'):
            check_operation(self.root,scope,{'tool':'edit_manuscript','arguments':arguments})

    def test_passage_ambiguity_and_wrong_task_fields_never_bind(self):
        self.store.edit_active_document('one',content='重复。重复。',expected_version_id=self.one.active_version_id,reason='setup')
        version=self.store.get_document('one').active_version_id
        for passage in ('重复。','not in source'):
            with self.assertRaisesRegex(ValueError,'exactly once'):bind_task(self.root,{'kind':'revision','document':'one','sourceVersion':version,'passage':passage})
        for value in ({'kind':'revision'},{'kind':'planning','passage':'not allowed'},{'kind':'adopt','proposalId':1,'document':'one'}):
            with self.assertRaises(ValueError):bind_task(self.root,value)

    def test_new_chapter_has_exact_slug_sequence_and_stopping_budget(self):
        scope=bind_task(self.root,{'kind':'draft','document':'three','sourceVersion':0,'sequence':3})
        details={'tool':'create_chapter','arguments':{'slug':'three','sequence':3}}
        self.assertEqual(check_operation(self.root,scope,details),'grants')
        for change in ({'slug':'four'},{'sequence':4}):
            with self.assertRaisesRegex(ValueError,'outside'):check_operation(self.root,scope,{**details,'arguments':{**details['arguments'],**change}})
        scope['grants']=1
        with self.assertRaisesRegex(ValueError,'stopping point'):check_operation(self.root,scope,details)
        self.assertEqual(check_operation(self.root,scope,{'tool':'update_book_overview','arguments':{}}),'overviewGrants')

    def test_ui_first_chapter_scope_and_exact_review_adoption_version(self):
        candidate=self.store.create_version('one',content='候选。')
        for stage,kind in (('draft','draft'),('review','review'),('accept','adopt')):
            definition=stage_task(self.root,stage)
            self.assertEqual((definition['kind'],definition['document']),(kind,'one'))
        scope=bind_task(self.root,stage_task(self.root,'review'))
        for wrong in (candidate.version_number-1,candidate.version_number+1):
            with self.assertRaisesRegex(ValueError,'Candidate version'):check_operation(self.root,scope,{'tool':'studio_review','arguments':{'slug':'one','version_number':wrong}})
        self.assertEqual(scope['definition']['maxWrites'],5)
        self.store.create_version('one',content='另一个候选。')
        with self.assertRaisesRegex(ValueError,'exact saved candidate'):stage_task(self.root,'accept')

    def test_wire_contract_retains_old_calls_and_rejects_model_style_extra_authority(self):
        validate('run/start',{'sessionId':'test','text':'讨论'})
        validate('run/start',{'sessionId':'test','text':'只规划','stage':'plan'})
        for value in ({'task':{'kind':'planning','allowedTools':['studio_accept']}},
            {'stage':'plan','task':{'kind':'planning'}}):
            with self.assertRaises(Exception):validate('run/start',{'sessionId':'test','text':'任务',**value})

    def test_review_only_and_explicit_review_with_revision_keep_distinct_boundaries(self):
        candidate=self.store.create_version('one',content='审阅候选。')
        definition={'kind':'review','document':'one','sourceVersion':self.one.active_version_id,'versionNumber':candidate.version_number}
        details={'tool':'edit_manuscript','arguments':{'slug':'one','expected_version_id':self.one.active_version_id,'content':'修订候选。'}}
        with self.assertRaisesRegex(ValueError,'outside'):check_operation(self.root,bind_task(self.root,definition),details)
        scope=bind_task(self.root,{**definition,'allowRevision':True})
        self.assertEqual(check_operation(self.root,scope,details),'revisionGrants')
        scope['revisionGrants']=1
        with self.assertRaisesRegex(ValueError,'stopping point'):check_operation(self.root,scope,details)

    def test_existing_unnumbered_chapter_does_not_authorize_another_first_chapter(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);store=ProjectStore(root);store.initialize(name='Legacy numbering')
            store.create_document(kind='chapter',slug='legacy',title='Legacy first',content='原正文。')
            with self.assertRaisesRegex(ValueError,'not identified'):stage_task(root,'draft')
            self.assertEqual(len(store.list_documents(kind='chapter')),1)


class BackendTaskScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=ProjectStore(self.root);self.store.initialize(name='Host task scope')
        self.one=self.store.create_document(kind='chapter',slug='one',title='First',content='前文。原对白。后文。',state='accepted',sequence=1)
        self.config=make_config(self.root);self.events=[]
        self.backend=Backend(self.config,self.events.append,agent_factory=FakeAgent)
        self.key=(await self.backend.dispatch('session/create',{}))['sessionId']

    async def asyncTearDown(self):
        await self.backend.close();self.tmp.cleanup()

    async def run_task(self,**params):
        result=await self.backend.dispatch('run/start',{'sessionId':self.key,'text':'作者任务',**params})
        await self.backend.tasks[result['runId']][1]

    async def test_planning_scope_survives_continue_permissions_and_reconnect(self):
        await self.run_task(stage='plan')
        first=self.backend.session(self.key)['agent'].runtime.read()['task']
        await self.run_task()
        await self.backend.dispatch('permissions/set',{'sessionId':self.key,'mode':'full_access','creativeMode':'full_access'})
        await start_waiting_run(self.backend,self.key)
        with self.assertRaisesRegex(ValueError,'outside'):
            await self.backend.session(self.key)['control'].approval_handler({'tool':'studio_draft','arguments':{'slug':'one'}})
        await self.backend.close();self.backend=Backend(self.config,self.events.append,agent_factory=FakeAgent)
        await self.backend.open(self.key)
        self.assertEqual(self.backend.session(self.key)['agent'].runtime.read()['task'],first)
        await start_waiting_run(self.backend,self.key)
        with self.assertRaisesRegex(ValueError,'outside'):
            await self.backend.session(self.key)['control'].approval_handler({'tool':'studio_accept','arguments':{}})
        self.assertFalse(self.backend.approvals.pending)

    async def test_concurrent_approvals_cannot_exceed_task_budget(self):
        await self.run_task(task={'kind':'revision','document':'one','sourceVersion':self.one.active_version_id,'passage':'原对白。'})
        details={'tool':'edit_manuscript','arguments':{'slug':'one','expected_version_id':self.one.active_version_id,'content':'前文。新对白。后文。'}}
        await start_waiting_run(self.backend,self.key)
        approve=self.backend.session(self.key)['control'].approval_handler
        first=asyncio.create_task(approve(copy.deepcopy(details)));second=asyncio.create_task(approve(copy.deepcopy(details)))
        for _ in range(100):
            if len(self.backend.approvals.pending)==2:break
            await asyncio.sleep(.01)
        self.assertEqual(len(self.backend.approvals.pending),2)
        for key in list(self.backend.approvals.pending):self.backend.approvals.resolve(key,{'decision':'accept'})
        outcomes=await asyncio.gather(first,second,return_exceptions=True)
        self.assertEqual(sum(isinstance(value,dict) for value in outcomes),1)
        self.assertEqual(sum(isinstance(value,ValueError) for value in outcomes),1)
        self.assertEqual(self.backend.session(self.key)['agent'].runtime.read()['task']['grants'],1)

    async def test_real_native_mcp_rejects_planning_to_prose_under_full_access(self):
        await self.backend.close()
        config=replace(self.config,runtime_engine='native',protocol='responses',default_model='synthetic-native')
        self.backend=Backend(config,self.events.append)
        await self.backend.open(self.key)
        await self.backend.dispatch('permissions/set',{'sessionId':self.key,'mode':'full_access','creativeMode':'full_access'})
        calls=[]
        async def model(_model,request,delta):
            calls.append(request)
            if len(calls)==1:
                return {'status':'completed','output':[{'type':'function_call','call_id':'bad-prose','name':'edit_manuscript',
                    'arguments':json.dumps({'slug':'one','expected_version_id':self.one.active_version_id,'content':'不应保存的正文。',
                        'reason':'malicious scope expansion','book_overview':'维修师在星港调查失灵的闸门。'})}]}
            return answer('越界正文未保存。')
        with patch('lg_cli.native_model.NativeModel.generate',model),patch('lg_cli.live_agent.LiveAgent',side_effect=AssertionError('No Codex')):
            await self.run_task(stage='plan')
        self.assertEqual(len(calls),2)
        self.assertIn('outside the author task scope',json.dumps(calls[-1]))
        self.assertEqual(len(self.store.list_versions('one')),1)
        self.assertEqual(self.store.get_document('one'),self.one)
        self.assertFalse(self.backend.approvals.pending)

    async def test_scoped_model_control_socket_cannot_stop_or_retry_another_workflow(self):
        from lg_cli.workflow_control import control_request
        await self.run_task(stage='plan')
        endpoint=await self.backend.session(self.key)['control'].endpoint()
        status=await asyncio.to_thread(control_request,endpoint,'status')
        self.assertEqual(status['status'],'idle')
        for action in ('stop','retry'):
            with self.assertRaisesRegex(ValueError,'outside this literary task'):
                await asyncio.to_thread(control_request,endpoint,action,job_id='unrelated-workflow')
        with self.assertRaisesRegex(ValueError,'not authorized'):
            await self.backend.session(self.key)['request']('command',{'command':'must not execute'})

    async def test_empty_corrupted_scope_is_not_treated_as_an_unscoped_legacy_session(self):
        runtime=self.backend.session(self.key)['agent'].runtime
        before=runtime.read()
        try:
            runtime.update(task={})
            with self.assertRaisesRegex(ValueError,'Invalid literary task metadata'):runtime.read()
        finally:
            runtime.path.write_text(json.dumps(before))
