"""Narrative records share Studio, exact task scopes and independent creative approval."""
import asyncio
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from lg_cli.backend import Backend
from lg_cli.backend_protocol import contract
from lg_cli.codex_policy import WritingLaunchPolicy
from lg_cli.conversation_store import ConversationStore
from lg_cli.main import main
from lg_cli.mcp_approval import READ_TOOLS, WRITE_TOOLS, operation_details, project_fingerprint
from lg_cli.native import writing_settings
from lg_cli.native_tools import NativeTools
from lg_cli.project_store import ProjectStore
from lg_cli.session_runtime import operation_policy
from lg_cli.studio_operations import OPERATIONS, READ_ONLY
from lg_cli.task_scope import bind_task, check_operation
from lg_cli.writing_mcp import create_server
from tests.helpers import make_config
from tests.test_backend import FakeAgent
from tests.test_cli_backend import isolated_cli
from tests.test_native_runtime import answer


def proposal_arguments(holder, *, expected=0, label='first'):
    return {'slug':'gate-belief','title':'门后的认知',
        'topic':{'audience':'character','holder_document_id':holder.id,'key':'gate','from_position':3},
        'statement':{'content':label+'：她相信门后安全。','stance':'believes','until_position':20,'sources':[]},
        'rationale':'作者声明的角色信念，不等同世界事实。','expected_version_id':expected}


class NarrativeKnowledgeToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.registry=patch.dict(os.environ,{'LITERARYGIANT_REGISTRY_HOME':str(self.root/'registry')})
        self.registry.start()
        self.store=ProjectStore(self.root);self.store.initialize(name='Narrative knowledge workbench')
        self.chapter=self.store.create_document(kind='chapter',slug='one',title='门前',content='她尚未开门。',state='accepted')
        self.holder=self.store.create_document(kind='character',slug='ada',title='阿达',content='记录角色身份。',state='accepted')
        self.store.add_fact(category='world',key='gate',value='门后有危险。',state='canonical')
        self.config=replace(make_config(self.root),environment='knowledge-tools-'+self.root.name)
        self.events=[];self.backend=Backend(self.config,self.events.append,agent_factory=FakeAgent)
        self.key=(await self.backend.dispatch('session/create',{}))['sessionId']

    async def asyncTearDown(self):
        await self.backend.close();self.registry.stop();self.tmp.cleanup()

    def seed(self):
        return self.store.propose_knowledge(**proposal_arguments(self.holder))

    def unchanged_story(self):
        return self.store.get_document('one'),self.store.list_versions('one'),self.store.list_facts(limit=None)

    async def pending(self):
        # Native starts a real MCP subprocess; cold startup can exceed one second.
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            if self.backend.approvals.pending:
                return next(event['params'] for event in reversed(self.events) if event['method']=='approval/requested')
            await asyncio.sleep(.01)
        self.fail('No approval request')

    async def finish(self,response):
        await self.backend.tasks[response['runId']][1]
        return next(event['params'] for event in reversed(self.events)
            if event['method']=='run/completed' and event['params']['runId']==response['runId'])

    async def call(self,operation,arguments):
        return await self.backend.dispatch('studio/call',{'sessionId':self.key,'operation':operation,'arguments':arguments})

    async def approved(self,operation,arguments,*,kind='creative',decision='accept'):
        response=await self.call(operation,arguments);shown=await self.pending()
        self.assertEqual(shown['kind'],kind)
        self.backend.approvals.resolve(shown['approvalId'],{'decision':decision})
        return await self.finish(response),shown

    async def test_registry_has_read_write_and_creative_categories_without_expanding_core_review(self):
        tools={tool.name for tool in await create_server(self.config).list_tools()}
        self.assertEqual(tools,READ_TOOLS|WRITE_TOOLS);self.assertEqual(len(tools),34)
        self.assertEqual(len(READ_TOOLS),15)
        for operation,tool in [('knowledge','studio_knowledge'),('knowledge-view','studio_knowledge_view'),
                ('knowledge-propose','studio_knowledge_propose'),('knowledge-confirm','studio_knowledge_confirm'),
                ('knowledge-withdraw','studio_knowledge_withdraw')]:
            self.assertEqual(OPERATIONS[operation],tool)
            self.assertIn(tool,READ_TOOLS if operation in {'knowledge','knowledge-view'} else WRITE_TOOLS)
            self.assertEqual(operation in READ_ONLY,operation in {'knowledge','knowledge-view'})
        settings=writing_settings(self.config)
        self.assertEqual(set(settings['mcp_servers.lg_writing.enabled_tools']),tools)
        self.assertEqual(len(writing_settings(self.config,control_socket='/synthetic/socket')['mcp_servers.lg_writing.enabled_tools']),37)
        review=json.loads(WritingLaunchPolicy().prepare(self.config,review_only=True).settings_json)
        self.assertEqual(set(review['mcp_servers.lg_writing.enabled_tools']),
            {'project_memory','read_document','reference_search','document_history'})
        native=NativeTools(self.config,None,review_only=True)
        try:
            await native.start();self.assertEqual(set(native.tools),READ_TOOLS)
        finally:await native.close()
        self.assertEqual(operation_policy({'mode':'full_access','creative_mode':'ask'},
            {'tool':'studio_knowledge_propose'}),('operation','full_access'))
        for tool in ('studio_knowledge_confirm','studio_knowledge_withdraw'):
            self.assertEqual(operation_policy({'mode':'full_access','creative_mode':'ask'},{'tool':tool}),('creative','ask'))
        schema=Path(__file__).resolve().parents[1]/'docs/backend-request-schema.json'
        self.assertEqual(json.loads(schema.read_text()),contract())

    async def test_reads_need_no_approval_runtime_or_story_change_and_view_is_not_model_isolation(self):
        saved=self.seed();before=(self.unchanged_story(),project_fingerprint(self.root))
        arguments={'audience':'character','position':4,'holder_document':'ada','limit':100}
        with patch('lg_cli.backend.runtime_factory',side_effect=AssertionError('Read started runtime')):
            record=await self.call('knowledge',{'slug':'gate-belief'})
            view=await self.call('knowledge-view',arguments)
        self.assertEqual(record,saved)
        self.assertEqual(view,self.store.knowledge_view(**arguments))
        self.assertFalse(view['view']['model_input_isolation']);self.assertEqual(view['statements'],[])
        self.assertFalse(self.backend.tasks);self.assertFalse(self.backend.approvals.pending)
        self.assertEqual((self.unchanged_story(),project_fingerprint(self.root)),before)
        for invalid in ({**arguments,'position':-1000000001},{**arguments,'audience':'author_global'},
                {**arguments,'holder_document':None},{**arguments,'limit':0},{**arguments,'view':{'strict':True}}):
            with self.subTest(invalid=invalid),self.assertRaises(Exception):await self.call('knowledge-view',invalid)
        self.assertEqual((self.unchanged_story(),project_fingerprint(self.root)),before)

    async def test_proposal_operation_never_confirms_and_creative_decline_preserves_record(self):
        before=self.unchanged_story();args=proposal_arguments(self.holder)
        denied,_=await self.approved('knowledge-propose',args,kind='operation',decision='decline')
        self.assertEqual(denied['status'],'cancelled');self.assertEqual(self.store.list_knowledge_snapshots(),[])
        saved,_=await self.approved('knowledge-propose',args,kind='operation')
        self.assertEqual(saved['status'],'completed',saved.get('error'))
        record=saved['result']['value'];self.assertIsNone(record['confirmed']);self.assertEqual(record['status'],'unconfirmed')
        self.backend.session(self.key)['agent'].runtime.update(mode='full_access')
        confirm={'slug':record['slug'],'proposal_version_number':record['proposal_version_number'],
            'expected_version_id':record['version_id']}
        denied,shown=await self.approved('knowledge-confirm',confirm,decision='decline')
        self.assertEqual(denied['status'],'cancelled');self.assertEqual(self.store.knowledge_snapshot(record['slug']),record)
        self.assertEqual(shown['details']['preview'],self.store.knowledge_confirmation_preview(**confirm))
        with self.assertRaisesRegex(ValueError,'expired'):
            self.backend.approvals.resolve(shown['approvalId'],{'decision':'accept'})
        self.assertEqual(self.unchanged_story(),before)

    async def test_exact_old_proposal_confirm_regenerate_and_withdraw_keep_fact_and_prose(self):
        first=self.seed();before=self.unchanged_story()
        second=self.store.propose_knowledge(**proposal_arguments(self.holder,expected=first['version_id'],label='second'))
        args={'slug':'gate-belief','proposal_version_number':first['proposal_version_number'],'expected_version_id':second['version_id']}
        shown=operation_details(self.root,'studio_knowledge_confirm',args)['preview']
        self.assertEqual(shown['proposal_version_id'],first['proposal_version_id'])
        self.assertEqual(shown['proposed_confirmation']['content'],first['proposal']['content'])
        confirmed,_=await self.approved('knowledge-confirm',args)
        self.assertEqual(confirmed['status'],'completed',confirmed.get('error'))
        record=confirmed['result']['value'];self.assertEqual(record['confirmed']['proposal_version_id'],first['proposal_version_id'])
        self.assertEqual(record['confirmed']['content'],first['proposal']['content'])
        newer=self.store.propose_knowledge(**proposal_arguments(self.holder,expected=record['version_id'],label='third'))
        self.assertEqual(newer['confirmed'],record['confirmed'])
        view=await self.call('knowledge-view',{'audience':'character','position':4,'holder_document':'ada'})
        self.assertEqual(len(view['statements']),1);self.assertEqual(view['statements'][0]['content'],first['proposal']['content'])
        self.assertEqual(view['statements'][0]['stance'],'believes')
        self.assertEqual((await self.call('knowledge-view',{'audience':'character','position':21,'holder_document':'ada'}))['statements'],[])
        withdrawn,_=await self.approved('knowledge-withdraw',{'slug':'gate-belief','expected_version_id':newer['version_id']})
        self.assertEqual(withdrawn['status'],'completed',withdrawn.get('error'));self.assertIsNone(withdrawn['result']['value']['confirmed'])
        self.assertEqual(self.unchanged_story(),before)

    async def test_pending_author_change_and_stale_fingerprint_cannot_confirm_or_replay(self):
        saved=self.seed();args={'slug':'gate-belief','proposal_version_number':saved['proposal_version_number'],
            'expected_version_id':saved['version_id']}
        preview=await self.backend.dispatch('studio/preview',{'operation':'knowledge-confirm','arguments':args})
        response=await self.call('knowledge-confirm',args);shown=await self.pending()
        self.store.create_document(kind='note',slug='author-note',title='补充',content='作者的独立补充。')
        self.backend.approvals.resolve(shown['approvalId'],{'decision':'accept'})
        self.assertEqual((await self.finish(response))['status'],'failed');self.assertIsNone(self.store.knowledge_snapshot('gate-belief')['confirmed'])
        with self.assertRaisesRegex(ValueError,'preview changed'):
            await self.backend.dispatch('studio/call',{'sessionId':self.key,'operation':'knowledge-confirm',
                'arguments':args,'expectedFingerprint':preview['fingerprint']})
        confirmed,_=await self.approved('knowledge-confirm',args);self.assertEqual(confirmed['status'],'completed')
        versions=self.store.list_versions('gate-belief')
        with self.assertRaises(ValueError):await self.call('knowledge-confirm',args)
        self.assertEqual(self.store.list_versions('gate-belief'),versions);self.assertFalse(self.backend.approvals.pending)

    async def test_scopes_bind_exact_proposal_head_action_and_one_transition(self):
        saved=self.seed();definition={'kind':'knowledge','knowledgeAction':'confirm','document':'gate-belief',
            'sourceVersion':saved['version_id'],'versionNumber':saved['proposal_version_number']}
        scope=bind_task(self.root,definition);args={'slug':'gate-belief','expected_version_id':saved['version_id'],
            'proposal_version_number':saved['proposal_version_number']}
        details={'tool':'studio_knowledge_confirm','arguments':args}
        self.assertEqual(check_operation(self.root,scope,details),'grants')
        for change in ({'slug':'one'},{'expected_version_id':0},{'proposal_version_number':2}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                check_operation(self.root,scope,{**details,'arguments':{**args,**change}})
        for tool in ('studio_knowledge_withdraw','studio_knowledge_propose','studio_select_decision','studio_accept','edit_manuscript'):
            with self.subTest(tool=tool),self.assertRaisesRegex(ValueError,'outside the author task'):
                check_operation(self.root,scope,{'tool':tool,'arguments':args})
        for invalid in ({**definition,'maxWrites':2},{**definition,'sourceVersion':0},
                {key:value for key,value in definition.items() if key!='versionNumber'},
                {**definition,'knowledgeAction':'withdraw'},{**definition,'document':'one'}):
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):bind_task(self.root,invalid)
        scope['grants']=1
        with self.assertRaisesRegex(ValueError,'stopping point'):check_operation(self.root,scope,details)
        for task in ({'kind':'planning','stage':'plan','document':'gate-belief','sourceVersion':saved['version_id']},{'kind':'setting'}):
            planning=bind_task(self.root,task)
            proposed=proposal_arguments(self.holder,expected=saved['version_id'])
            self.assertEqual(check_operation(self.root,planning,{'tool':'studio_knowledge_propose','arguments':proposed}),'grants')
            for tool in ('studio_knowledge_confirm','studio_knowledge_withdraw'):
                with self.assertRaisesRegex(ValueError,'outside the author task'):
                    check_operation(self.root,planning,{'tool':tool,'arguments':args,'author_action':True})
        planning=bind_task(self.root,{'kind':'planning','stage':'plan','document':'gate-belief','sourceVersion':saved['version_id'],
            'contextDocument':'one','contextVersion':self.chapter.active_version_id})
        self.store.edit_active_document('one',content='作者改写上下文。',expected_version_id=self.chapter.active_version_id,reason='author')
        with self.assertRaisesRegex(ValueError,'context version changed'):
            check_operation(self.root,planning,{'tool':'studio_knowledge_propose','arguments':proposal_arguments(self.holder,expected=saved['version_id'])})

    async def test_native_exact_task_uses_real_mcp_observed_read_and_creative_approval(self):
        saved=self.seed();before=self.unchanged_story();await self.backend.close()
        config=replace(self.config,runtime_engine='native',protocol='responses',default_model='synthetic-knowledge',
            enable_reference=False,timeout_seconds=15)
        self.backend=Backend(config,self.events.append);await self.backend.open(self.key)
        await self.backend.dispatch('permissions/set',{'sessionId':self.key,'mode':'full_access','creativeMode':'ask'})
        for decision in ('decline','accept'):
            calls=[]
            async def model(_model,request,delta):
                calls.append(request)
                if len(calls)==1:
                    return {'status':'completed','output':[{'type':'function_call','call_id':'read-'+decision,
                        'name':'studio_knowledge','arguments':'{"slug":"gate-belief"}'}]}
                if len(calls)==2:
                    return {'status':'completed','output':[{'type':'function_call','call_id':'confirm-'+decision,
                        'name':'studio_knowledge_confirm','arguments':json.dumps({'slug':'gate-belief',
                            'proposal_version_number':saved['proposal_version_number'],'expected_version_id':saved['version_id']})}]}
                return answer('已处理准确的认知记录任务；未改变正文或世界事实。')
            with patch('lg_cli.native_model.NativeModel.generate',model),\
                    patch('lg_cli.live_agent.LiveAgent',side_effect=AssertionError('No Core for Native')):
                response=await self.backend.dispatch('run/start',{'sessionId':self.key,'text':'处理预览中的准确认知确认',
                    'task':{'kind':'knowledge','knowledgeAction':'confirm','document':'gate-belief',
                        'sourceVersion':saved['version_id'],'versionNumber':saved['proposal_version_number']}})
                shown=await self.pending();self.assertEqual(shown['kind'],'creative')
                self.assertEqual(shown['details']['preview']['action'],'confirm')
                self.backend.approvals.resolve(shown['approvalId'],{'decision':decision})
                final=await self.finish(response)
            self.assertEqual(final['status'],'completed',final.get('error'));self.assertEqual(len(calls),3)
            runtime=self.backend.session(self.key)['agent'].runtime.read()
            self.assertEqual(runtime['task']['grants'],int(decision=='accept'))
            self.assertEqual([item['tool'] for item in runtime['generation_sources']['observations']],['studio_knowledge'])
            self.assertEqual(bool(self.store.knowledge_snapshot('gate-belief')['confirmed']),decision=='accept')
        self.assertEqual(self.unchanged_story(),before)


class NarrativeKnowledgeCLITests(unittest.TestCase):
    def test_cli_workbench_proposal_preview_confirmation_withdrawal_and_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=ProjectStore(root);store.initialize(name='CLI narrative knowledge')
            holder=store.create_document(kind='character',slug='ada',title='阿达',content='角色身份。',state='accepted')
            key=ConversationStore(root).create();input_path=root/'input.json'
            def call(operation,arguments,*extra):
                input_path.write_text(json.dumps(arguments,ensure_ascii=False));stdout=io.StringIO();stderr=io.StringIO()
                with patch('lg_cli.config.read_profiles',return_value={'profiles':{},'active':None}),isolated_cli(root),\
                        patch.dict(os.environ,{'LITERARYGIANT_REGISTRY_HOME':str(root/'registry')}),\
                        patch('sys.stdin',io.StringIO()),redirect_stdout(stdout),redirect_stderr(stderr):
                    code=main(['-C',str(root),'--conversation',key,'--operation-mode','full_access',*extra,
                        'studio',operation,'--input',str(input_path),'--json'])
                return code,stdout.getvalue(),stderr.getvalue()
            args=proposal_arguments(holder)
            code,out,err=call('knowledge-propose',args);self.assertEqual(code,2,err+out)
            self.assertEqual(store.list_knowledge_snapshots(),[])
            code,out,err=call('knowledge-propose',args,'--approve-fingerprint',json.loads(out)['fingerprint'])
            self.assertEqual(code,0,err+out);saved=store.knowledge_snapshot('gate-belief');self.assertIsNone(saved['confirmed'])
            confirm={'slug':'gate-belief','proposal_version_number':saved['proposal_version_number'],'expected_version_id':saved['version_id']}
            code,out,err=call('knowledge-confirm',confirm);self.assertEqual(code,2,err+out)
            preview=json.loads(out);self.assertEqual(preview['preview']['preview']['proposed_confirmation']['content'],args['statement']['content'])
            code,out,err=call('knowledge-confirm',confirm,'--approve-fingerprint',preview['fingerprint']);self.assertEqual(code,0,err+out)
            confirmed=store.knowledge_snapshot('gate-belief');self.assertIsNotNone(confirmed['confirmed'])
            count=len(store.list_versions('gate-belief'))
            code,out,err=call('knowledge-confirm',confirm,'--approve-fingerprint',preview['fingerprint'])
            self.assertEqual(code,1,err+out);self.assertEqual(len(store.list_versions('gate-belief')),count)
            code,out,err=call('knowledge-view',{'audience':'character','position':4,'holder_document':'ada'})
            self.assertEqual(code,0,err+out);self.assertIn(args['statement']['content'],out)
            withdraw={'slug':'gate-belief','expected_version_id':confirmed['version_id']}
            code,out,err=call('knowledge-withdraw',withdraw);self.assertEqual(code,2,err+out)
            code,out,err=call('knowledge-withdraw',withdraw,'--approve-fingerprint',json.loads(out)['fingerprint'])
            self.assertEqual(code,0,err+out);self.assertIsNone(store.knowledge_snapshot('gate-belief')['confirmed'])
            self.assertEqual(store.list_facts(),[])
