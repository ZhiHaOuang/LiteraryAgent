"""Decision options and official choices share the real Studio approval boundary."""
import asyncio
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from lg_cli.backend import Backend
from lg_cli.conversation_store import ConversationStore
from lg_cli.main import main
from lg_cli.mcp_approval import READ_TOOLS, WRITE_TOOLS, operation_details
from lg_cli.project_store import ProjectStore
from lg_cli.session_runtime import operation_policy
from lg_cli.studio_operations import OPERATIONS, READ_ONLY
from lg_cli.task_scope import bind_task, check_operation
from lg_cli.writing_mcp import create_server
from tests.helpers import make_config
from tests.test_backend import FakeAgent, start_waiting_run
from tests.test_cli_backend import isolated_cli
from tests.test_native_runtime import answer


def options_arguments(source, *, expected=0, label='first'):
    return {'slug':'crossroads','title':'岔路方向','topic':{'scope':'chapter','document_id':source.id,'key':'next-turn'},
        'options':[{'id':key,'title':label+' '+key,'content':label+'：'+text}
            for key,text in [('rescue','先救被困者，再追踪停电者。'),('pursue','先追踪停电者，留下求援信号。'),
                ('bargain','与守门人交换线索，承担公开身份的代价。')]],
        'sources':[{'document_id':source.id,'version_id':source.active_version_id}],
        'request':'比较救人、追踪与交换线索三个方向，等待作者选择。','expected_version_id':expected}


class DecisionToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=ProjectStore(self.root);self.store.initialize(name='Decision workbench')
        self.source=self.store.create_document(kind='chapter',slug='one',title='停电',content='钟塔停了，门后有人求救。',state='accepted')
        self.store.add_fact(category='world',key='救援规则',value='打开闸门需要切断备用电源。',state='canonical')
        self.config=make_config(self.root);self.events=[]
        self.backend=Backend(self.config,self.events.append,agent_factory=FakeAgent)
        self.key=(await self.backend.dispatch('session/create',{}))['sessionId']

    async def asyncTearDown(self):
        await self.backend.close();self.tmp.cleanup()

    async def pending(self):
        for _ in range(100):
            if self.backend.approvals.pending:
                return next(e['params'] for e in reversed(self.events) if e['method']=='approval/requested')
            await asyncio.sleep(.01)
        self.fail('No approval request')

    async def finish(self,response):
        await self.backend.tasks[response['runId']][1]
        return next(e['params'] for e in reversed(self.events)
            if e['method']=='run/completed' and e['params']['runId']==response['runId'])

    async def call(self,operation,arguments):
        return await self.backend.dispatch('studio/call',{'sessionId':self.key,'operation':operation,'arguments':arguments})

    async def approved(self,operation,arguments,*,kind='creative',decision='accept'):
        response=await self.call(operation,arguments);shown=await self.pending()
        self.assertEqual(shown['kind'],kind)
        self.backend.approvals.resolve(shown['approvalId'],{'decision':decision})
        return await self.finish(response),shown

    def seed(self):
        return self.store.save_decision_options(**options_arguments(self.source))

    async def test_registry_read_and_host_permission_classification(self):
        from lg_cli.native import writing_settings
        enabled=writing_settings(self.config)['mcp_servers.lg_writing.enabled_tools']
        tools={tool.name:tool for tool in await create_server(self.config).list_tools()}
        for operation,tool in [('decision-options','studio_decision_options'),('select-decision','studio_select_decision'),
                ('withdraw-decision','studio_withdraw_decision'),('decision','studio_decision')]:
            self.assertEqual(OPERATIONS[operation],tool);self.assertIn(tool,tools)
            self.assertIn(tool,enabled)
            self.assertIn(tool,READ_TOOLS if operation=='decision' else WRITE_TOOLS)
        self.assertIn('decision',READ_ONLY)
        self.assertEqual(operation_policy({'mode':'full_access','creative_mode':'ask'},
            {'tool':'studio_decision_options'}),('operation','full_access'))
        for tool in ('studio_select_decision','studio_withdraw_decision'):
            self.assertEqual(operation_policy({'mode':'full_access','creative_mode':'ask'},{'tool':tool}),('creative','ask'))
        expected=self.seed()
        actual=await self.backend.dispatch('studio/call',{'operation':'decision','arguments':{'slug':'crossroads'}})
        self.assertEqual(actual,expected);self.assertFalse(self.backend.tasks)
        self.assertFalse(self.backend.approvals.pending)

    async def test_options_approval_does_not_select_or_change_manuscript_or_facts(self):
        args=options_arguments(self.source)
        before=self.store.get_document('one');facts=self.store.list_facts()
        denied,_=await self.approved('decision-options',args,kind='operation',decision='decline')
        self.assertEqual(denied['status'],'cancelled')
        self.assertEqual(self.store.list_decision_snapshots(),[])
        saved,_=await self.approved('decision-options',args,kind='operation')
        self.assertEqual(saved['status'],'completed',saved.get('error'))
        value=saved['result']['value'];self.assertEqual(len(value['options']),3)
        self.assertIsNone(value['selection']);self.assertEqual(value['status'],'unselected')
        self.assertTrue((self.root/value['artifact']).is_file())
        self.assertTrue(value['artifact'].startswith('ReferenceLibrary/plans/'))
        self.assertEqual(self.store.get_document('one'),before);self.assertEqual(self.store.list_facts(),facts)

    async def test_exact_old_option_selection_regeneration_and_withdrawal(self):
        initial=self.seed()
        latest=self.store.save_decision_options(**options_arguments(self.source,expected=initial['version_id'],label='second'))
        self.backend.session(self.key)['agent'].runtime.update(mode='full_access')
        args={'slug':'crossroads','options_version_number':initial['version_number'],
            'option_id':'rescue','expected_version_id':latest['version_id']}
        preview=operation_details(self.root,'studio_select_decision',args)['preview']
        self.assertEqual(preview['proposed_selection']['content'],initial['options'][0]['content'])
        self.assertEqual(preview['options_version_id'],initial['version_id'])
        denied,shown=await self.approved('select-decision',args,decision='decline')
        self.assertEqual(denied['status'],'cancelled');self.assertIsNone(self.store.decision_snapshot('crossroads')['selection'])
        with self.assertRaisesRegex(ValueError,'expired'):
            self.backend.approvals.resolve(shown['approvalId'],{'decision':'accept'})
        selected,_=await self.approved('select-decision',args)
        self.assertEqual(selected['status'],'completed',selected.get('error'))
        selected=selected['result']['value'];choice=selected['selection']
        self.assertEqual(choice['options_version_id'],initial['version_id']);self.assertEqual(choice['option_id'],'rescue')
        self.assertEqual(choice['content'],initial['options'][0]['content'])
        regenerated=await self.call('decision-options',options_arguments(self.source,expected=selected['version_id'],label='third'))
        regenerated=(await self.finish(regenerated))['result']['value']
        self.assertEqual(regenerated['selection'],choice)
        self.store.edit_active_document('one',content='作者明确更改了救援现场。',expected_version_id=self.source.active_version_id,reason='author')
        self.assertEqual(self.store.decision_snapshot('crossroads')['source_status'],'stale')
        withdrawn,_=await self.approved('withdraw-decision',{'slug':'crossroads','expected_version_id':regenerated['version_id']})
        self.assertEqual(withdrawn['status'],'completed',withdrawn.get('error'))
        self.assertIsNone(withdrawn['result']['value']['selection'])
        self.assertEqual(withdrawn['result']['value']['status'],'withdrawn')
        self.assertEqual(self.store.get_document('one').content,'作者明确更改了救援现场。')

    async def test_pending_change_and_old_fingerprint_cannot_authorize_selection(self):
        saved=self.seed();args={'slug':'crossroads','options_version_number':saved['version_number'],
            'option_id':'pursue','expected_version_id':saved['version_id']}
        params={'operation':'select-decision','arguments':args}
        preview=await self.backend.dispatch('studio/preview',params)
        response=await self.call('select-decision',args);shown=await self.pending()
        self.store.create_document(kind='note',slug='author-note',title='作者补充',content='先保留犹豫。')
        self.backend.approvals.resolve(shown['approvalId'],{'decision':'accept'})
        self.assertEqual((await self.finish(response))['status'],'failed')
        self.assertIsNone(self.store.decision_snapshot('crossroads')['selection'])
        with self.assertRaisesRegex(ValueError,'preview changed'):
            await self.backend.dispatch('studio/call',{'sessionId':self.key,**params,'expectedFingerprint':preview['fingerprint']})
        selected,_=await self.approved('select-decision',args)
        self.assertEqual(selected['status'],'completed')
        with self.assertRaises(ValueError):
            await self.call('select-decision',args)
        self.assertFalse(self.backend.approvals.pending)

    async def test_planning_can_save_options_but_never_select_or_withdraw(self):
        args=options_arguments(self.source)
        scope=bind_task(self.root,{'kind':'planning','stage':'plan','document':'crossroads','sourceVersion':0})
        self.assertEqual(check_operation(self.root,scope,{'tool':'studio_decision_options','arguments':args}),'grants')
        wrong={**args,'expected_version_id':123}
        with self.assertRaisesRegex(ValueError,'source version'):
            check_operation(self.root,scope,{'tool':'studio_decision_options','arguments':wrong})
        control=self.backend.session(self.key)['control']
        self.backend.session(self.key)['agent'].runtime.update(task=scope,mode='full_access',creative_mode='full_access')
        await start_waiting_run(self.backend,self.key)
        for tool in ('studio_select_decision','studio_withdraw_decision'):
            with self.subTest(tool=tool),self.assertRaisesRegex(ValueError,'outside the author task'):
                await control.approval_handler({'tool':tool,'arguments':args,'author_action':True})
        self.assertFalse(self.backend.approvals.pending);self.assertEqual(self.store.list_decision_snapshots(),[])

    async def test_decision_scope_binds_exact_action_option_head_and_single_transition(self):
        saved=self.seed()
        definition={'kind':'decision','decisionAction':'select','document':'crossroads','sourceVersion':saved['version_id'],
            'versionNumber':saved['version_number'],'optionId':'rescue'}
        scope=bind_task(self.root,definition)
        args={'slug':'crossroads','expected_version_id':saved['version_id'],
            'options_version_number':saved['version_number'],'option_id':'rescue'}
        details={'tool':'studio_select_decision','arguments':args}
        self.assertEqual(check_operation(self.root,scope,details),'grants')
        for change in ({'slug':'one'},{'expected_version_id':0},{'options_version_number':2},{'option_id':'pursue'}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                check_operation(self.root,scope,{**details,'arguments':{**args,**change}})
        for tool in ('studio_withdraw_decision','studio_decision_options','studio_accept','edit_manuscript'):
            with self.subTest(tool=tool),self.assertRaisesRegex(ValueError,'outside the author task'):
                check_operation(self.root,scope,{'tool':tool,'arguments':args})
        for invalid in ({**definition,'maxWrites':2},{**definition,'sourceVersion':0},
                {key:value for key,value in definition.items() if key!='optionId'},
                {**definition,'decisionAction':'withdraw'},{**definition,'document':'one'}):
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):bind_task(self.root,invalid)
        scope['grants']=1
        with self.assertRaisesRegex(ValueError,'stopping point'):check_operation(self.root,scope,details)
        self.store.edit_active_document('one',content='来源已变。',expected_version_id=self.source.active_version_id,reason='author')
        with self.assertRaisesRegex(ValueError,'source changed'):bind_task(self.root,definition)

    async def test_native_scoped_run_uses_real_mcp_and_separate_creative_approval(self):
        saved=self.seed();before=self.store.get_document('one');facts=self.store.list_facts()
        await self.backend.close()
        config=replace(self.config,runtime_engine='native',protocol='responses',default_model='synthetic-decision',
            enable_reference=False,timeout_seconds=15)
        self.backend=Backend(config,self.events.append)
        await self.backend.open(self.key)
        await self.backend.dispatch('permissions/set',{'sessionId':self.key,'mode':'full_access','creativeMode':'ask'})

        async def run(action,arguments,definition,decision):
            calls=[];tool='studio_select_decision' if action=='select' else 'studio_withdraw_decision'
            async def model(_model,request,delta):
                calls.append(request)
                if len(calls)==1:
                    self.assertIn(tool,{entry['name'] for entry in request['tools']})
                    return {'status':'completed','output':[{'type':'function_call','call_id':action+'-'+decision,
                        'name':tool,'arguments':json.dumps(arguments)}]}
                return answer('已按作者的准确决定范围完成；未写入正文或事实。')
            with patch('lg_cli.native_model.NativeModel.generate',model),\
                    patch('lg_cli.live_agent.LiveAgent',side_effect=AssertionError('No Core for Native')):
                response=await self.backend.dispatch('run/start',{'sessionId':self.key,'text':'处理预览中的准确创作方向',
                    'task':definition})
                shown=await self.pending();self.assertEqual(shown['kind'],'creative')
                self.assertEqual(shown['details']['preview']['action'],action)
                self.backend.approvals.resolve(shown['approvalId'],{'decision':decision})
                final=await self.finish(response)
            self.assertEqual(final['status'],'completed',final.get('error'))
            self.assertEqual(len(calls),2)
            return self.backend.session(self.key)['agent'].runtime.read()['task']

        args={'slug':'crossroads','expected_version_id':saved['version_id'],
            'options_version_number':saved['version_number'],'option_id':'pursue'}
        definition={'kind':'decision','decisionAction':'select','document':'crossroads','sourceVersion':saved['version_id'],
            'versionNumber':saved['version_number'],'optionId':'pursue'}
        scope=await run('select',args,definition,'decline')
        self.assertEqual(scope['grants'],0);self.assertIsNone(self.store.decision_snapshot('crossroads')['selection'])
        scope=await run('select',args,definition,'accept')
        self.assertEqual(scope['grants'],1)
        selected=self.store.decision_snapshot('crossroads');self.assertEqual(selected['selection']['option_id'],'pursue')
        with self.assertRaisesRegex(ValueError,'stopping point'):
            check_operation(self.root,scope,{'tool':'studio_select_decision','arguments':args})
        args={'slug':'crossroads','expected_version_id':selected['version_id']}
        definition={'kind':'decision','decisionAction':'withdraw','document':'crossroads','sourceVersion':selected['version_id']}
        scope=await run('withdraw',args,definition,'accept')
        self.assertEqual(scope['grants'],1);self.assertIsNone(self.store.decision_snapshot('crossroads')['selection'])
        self.assertEqual(self.store.get_document('one'),before);self.assertEqual(self.store.list_facts(),facts)


class DecisionCLITests(unittest.TestCase):
    def test_cli_studio_requires_separate_choice_and_rejects_stale_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=ProjectStore(root);store.initialize(name='CLI decision')
            source=store.create_document(kind='chapter',slug='one',title='停电',content='原文保持。',state='accepted')
            key=ConversationStore(root).create();input_path=root/'input.json'
            def invoke(operation,arguments,*extra):
                input_path.write_text(json.dumps(arguments,ensure_ascii=False),encoding='utf-8')
                stdout=io.StringIO();stderr=io.StringIO()
                with isolated_cli(root),patch.dict(os.environ,{'LITERARYGIANT_REGISTRY_HOME':str(root/'registry')}),\
                        patch('sys.stdin',io.StringIO()),redirect_stdout(stdout),redirect_stderr(stderr):
                    code=main(['-C',str(root),'--conversation',key,'--operation-mode','full_access',*extra,
                        'studio',operation,'--input',str(input_path),'--json'])
                return code,stdout.getvalue(),stderr.getvalue()
            args=options_arguments(source)
            code,out,err=invoke('decision-options',args)
            self.assertEqual(code,2,err+out);self.assertEqual(store.list_decision_snapshots(),[])
            code,out,err=invoke('decision-options',args,'--approve-fingerprint',json.loads(out)['fingerprint'])
            self.assertEqual(code,0,err+out);saved=store.decision_snapshot('crossroads');self.assertIsNone(saved['selection'])
            select={'slug':'crossroads','options_version_number':saved['version_number'],'option_id':'bargain','expected_version_id':saved['version_id']}
            code,out,err=invoke('select-decision',select)
            self.assertEqual(code,2,err+out);preview=json.loads(out)
            self.assertEqual(preview['preview']['preview']['proposed_selection']['option_id'],'bargain')
            self.assertIsNone(store.decision_snapshot('crossroads')['selection'])
            code,out,err=invoke('select-decision',select,'--approve-fingerprint',preview['fingerprint'])
            self.assertEqual(code,0,err+out);selected=store.decision_snapshot('crossroads')
            self.assertEqual(selected['selection']['option_id'],'bargain')
            count=len(store.list_versions('crossroads'))
            code,out,err=invoke('select-decision',select,'--approve-fingerprint',preview['fingerprint'])
            self.assertEqual(code,1,err+out);self.assertEqual(len(store.list_versions('crossroads')),count)
            code,out,err=invoke('withdraw-decision',{'slug':'crossroads','expected_version_id':selected['version_id']})
            self.assertEqual(code,2,err+out);self.assertIsNotNone(store.decision_snapshot('crossroads')['selection'])
            code,out,err=invoke('withdraw-decision',{'slug':'crossroads','expected_version_id':selected['version_id']},
                '--approve-fingerprint',json.loads(out)['fingerprint'])
            self.assertEqual(code,0,err+out);self.assertIsNone(store.decision_snapshot('crossroads')['selection'])
            self.assertEqual(store.get_document('one'),source);self.assertEqual(store.list_facts(),[])
