"""Source diagnostics reuse read-only Studio routes without adding write grants."""
import asyncio
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from lg_cli.backend import Backend
from lg_cli.backend_protocol import contract
from lg_cli.book_analysis import fingerprint
from lg_cli.codex_policy import WritingLaunchPolicy
from lg_cli.generation_sources import bind_generation_sources, make_observation, make_receipt
from lg_cli.main import main
from lg_cli.mcp_approval import READ_TOOLS, WRITE_TOOLS, project_fingerprint
from lg_cli.native import writing_settings
from lg_cli.native_tools import NativeTools
from lg_cli.project_store import ProjectStore
from lg_cli.studio import review_source
from lg_cli.studio_operations import OPERATIONS, READ_ONLY, invoke
from lg_cli.task_scope import bind_task, check_operation
from lg_cli.workspace_library import entry
from lg_cli.writing_mcp import create_server
from tests.helpers import make_config
from tests.test_backend import FakeAgent, start_waiting_run
from tests.test_cli_backend import isolated_cli


class SourceImpactToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.env=patch.dict(os.environ,{'LITERARYGIANT_REGISTRY_HOME':str(self.root/'registry')})
        self.env.start()
        self.store=ProjectStore(self.root);self.store.initialize(name='Read-only impact tools')
        self.chapter=self.store.create_document(kind='chapter',slug='one',title='One',content='Original prose.',state='accepted',sequence=1)
        self.source=self.store.create_document(kind='note',slug='source',title='Source',content='Original source.',state='accepted')
        self.store.add_fact(category='world',key='gate',value='The gate requires a key.',state='canonical')
        receipt=make_receipt(mode='live',run_id='captured-input-run',observations=[make_observation('read_document',asdict(self.source))],
            unknown_reasons=['prior_history_unobserved'])
        with bind_generation_sources(receipt):
            self.candidate=self.store.create_version('one',content='Candidate prose.',expected_version_id=self.chapter.active_version_id)
        self.config=replace(make_config(self.root),environment='impact-tools-'+self.root.name);self.events=[]
        self.backend=Backend(self.config,self.events.append,agent_factory=FakeAgent)
        self.key=(await self.backend.dispatch('session/create',{}))['sessionId']

    async def asyncTearDown(self):
        await self.backend.close();self.env.stop();self.tmp.cleanup()

    def saved(self):
        return (self.store.list_documents(limit=None),self.store.list_versions('one'),self.store.list_facts(limit=None),
            project_fingerprint(self.root))

    async def test_registered_read_tools_match_native_core_and_wire_contract(self):
        registered={tool.name for tool in await create_server(self.config).list_tools()}
        self.assertEqual(len(registered),34);self.assertEqual(registered,READ_TOOLS|WRITE_TOOLS)
        self.assertEqual(len(READ_TOOLS),15);self.assertIn('studio_source_impact',READ_TOOLS)
        self.assertNotIn('studio_source_impact',WRITE_TOOLS)
        self.assertEqual(OPERATIONS['source-impact'],'studio_source_impact');self.assertIn('source-impact',READ_ONLY)
        settings=writing_settings(self.config)
        self.assertEqual(set(settings['mcp_servers.lg_writing.enabled_tools']),registered)
        self.assertEqual(len(writing_settings(self.config,control_socket='/synthetic/socket')['mcp_servers.lg_writing.enabled_tools']),37)
        review=json.loads(WritingLaunchPolicy().prepare(self.config,review_only=True).settings_json)
        self.assertEqual(set(review['mcp_servers.lg_writing.enabled_tools']),
            {'project_memory','read_document','reference_search','document_history'})
        native=NativeTools(self.config,None,review_only=True)
        try:
            await native.start()
            self.assertEqual(set(native.tools),READ_TOOLS)
        finally:await native.close()
        schema=Path(__file__).resolve().parents[1]/'docs/backend-request-schema.json'
        self.assertEqual(json.loads(schema.read_text()),contract())

    async def test_backend_read_has_no_approval_runtime_start_or_write_grant(self):
        state=self.backend.session(self.key)
        state['agent'].runtime.update(task=bind_task(self.root,{'kind':'planning','stage':'plan'}))
        before=self.saved();scope=state['agent'].runtime.read()['task']
        for mode in ('ask','full_access'):
            state['agent'].runtime.update(mode=mode,creative_mode='ask')
            with patch('lg_cli.backend.runtime_factory',side_effect=AssertionError('Read started a runtime')):
                value=await self.backend.dispatch('studio/call',{'operation':'source-impact',
                    'arguments':{'slug':'one','version_number':self.candidate.version_number}})
            self.assertEqual(value,self.store.source_impact('one',version_number=self.candidate.version_number))
            self.assertEqual(value['schema'],'lg.source-impact.v1')
            self.assertEqual(state['agent'].runtime.read()['task'],scope)
            self.assertFalse(self.backend.tasks);self.assertFalse(self.backend.approvals.pending)
        self.assertEqual(self.saved(),before)

    async def test_invalid_arguments_never_start_a_run_or_change_the_book(self):
        before=self.saved()
        for arguments in ({'slug':'one','confirm':True},{'slug':'one','generation_sources':{}},
                {'slug':'one','limit':0},{'slug':'one','version_number':0},{'slug':'not-saved'}):
            with self.subTest(arguments=arguments),self.assertRaises(Exception):
                await self.backend.dispatch('studio/call',{'operation':'source-impact','arguments':arguments})
        self.assertFalse(self.backend.tasks);self.assertFalse(self.backend.approvals.pending)
        self.assertEqual(self.saved(),before)

    async def test_generation_review_is_outside_formal_snapshot_and_fingerprint(self):
        server=create_server(self.config)
        args={'slug':'one','version_number':self.candidate.version_number}
        first=await invoke(server,'studio_review_context',args)
        self.assertEqual(first['sourceFingerprint'],fingerprint(review_source(self.store,'one',self.candidate.version_number)))
        self.assertNotIn('generationSourceReview',first['snapshot'])
        self.assertEqual(first['generationSourceReview'],self.store.generation_source_review('one',self.candidate.version_number))
        self.store.edit_active_document('source',content='Author replaced only the captured external source.',
            expected_version_id=self.source.active_version_id,reason='author')
        changed=await invoke(server,'studio_review_context',args)
        self.assertEqual(changed['snapshot'],first['snapshot'])
        self.assertEqual(changed['sourceFingerprint'],first['sourceFingerprint'])
        self.assertEqual(changed['generationSourceReview']['status'],'needs_review')
        self.assertEqual(self.store.get_document('one').active_version_id,self.chapter.active_version_id)

    async def test_library_summary_distinguishes_known_unchanged_changed_unknown_and_reference_candidate(self):
        key=f'candidate:one:{self.candidate.version_number}'
        before=entry(self.config,'pending',key)
        self.assertIn('已捕获来源的当前复核',before['body'])
        self.assertIn('所识别来源未发现变化，仍非完整依赖',before['body'])
        self.assertIn('生成时捕获的输入',before['body'])
        self.store.edit_active_document('source',content='New source.',expected_version_id=self.source.active_version_id,reason='author')
        changed=entry(self.config,'pending',key)
        self.assertIn('待作者复核',changed['body']);self.assertEqual(changed['actions'],before['actions'])
        legacy=self.store.create_version('one',content='Legacy candidate without a receipt.',expected_version_id=self.chapter.active_version_id)
        unknown=entry(self.config,'pending',f'candidate:one:{legacy.version_number}')
        self.assertIn('当前无法核对来源，不表示全部无影响',unknown['body'])
        reference=self.store.create_document(kind='outline',slug='plan',title='Plan',content='Original plan.',state='accepted')
        version=self.store.create_version('plan',content='Reference candidate.',expected_version_id=reference.active_version_id)
        detail=entry(self.config,'pending',f'candidate:plan:{version.version_number}')
        self.assertIn('已捕获来源的当前复核',detail['body']);self.assertEqual(detail['actions'],['adopt'])
        self.assertLess(len(detail['body']),60000)

    async def test_invalid_receipt_remains_readable_without_repair_or_private_error(self):
        original_actions=entry(self.config,'pending',f'candidate:one:{self.candidate.version_number}')['actions']
        for malformed in ('output_hash','missing_run_id','missing_observation_tool'):
            with self.subTest(malformed=malformed):
                metadata=json.loads(json.dumps(self.candidate.metadata))
                if malformed=='output_hash':metadata['generation_output_sha256']='bad'
                elif malformed=='missing_run_id':metadata['generation_sources'].pop('run_id')
                else:metadata['generation_sources']['observations'][0].pop('tool')
                with sqlite3.connect(self.root/'.literarygiant/story.sqlite3') as connection:
                    connection.execute('UPDATE document_versions SET metadata_json=? WHERE id=?',
                        (json.dumps(metadata),self.candidate.id))
                before=self.saved()
                detail=entry(self.config,'pending',f'candidate:one:{self.candidate.version_number}')
                self.assertIn('来源记录与候选内容不匹配或记录无效',detail['body'])
                self.assertIn('已捕获来源的当前复核',detail['body'])
                self.assertEqual(detail['actions'],original_actions)
                self.assertEqual(self.saved(),before)

    async def test_model_read_is_opaque_and_cannot_expand_planning_scope(self):
        state=self.backend.session(self.key)
        state['agent'].runtime.update(task=bind_task(self.root,{'kind':'planning','stage':'plan'}),mode='full_access')
        await start_waiting_run(self.backend,self.key)
        endpoint=await state['control'].endpoint();before=self.saved()
        server=create_server(self.config,control_socket=endpoint)
        value=await asyncio.to_thread(lambda:asyncio.run(invoke(server,'studio_source_impact',{'slug':'source'})))
        ledger=state['agent'].runtime.read()['generation_sources']
        self.assertEqual(ledger['observations'],[make_observation('studio_source_impact',value)])
        self.assertEqual(ledger['observations'][0]['refs'],[])
        self.assertEqual(ledger['observations'][0]['coverage'],'unknown')
        scope=state['agent'].runtime.read()['task'];self.assertEqual(scope['grants'],0)
        with self.assertRaisesRegex(ValueError,'outside'):
            check_operation(self.root,scope,{'tool':'studio_accept','arguments':{}})
        self.assertFalse(self.backend.approvals.pending);self.assertEqual(self.saved(),before)


class SourceImpactCLITests(unittest.TestCase):
    def test_cli_source_impact_is_read_only_without_confirmation(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);store=ProjectStore(root);store.initialize(name='CLI source impact')
            source=store.create_document(kind='note',slug='source',title='Source',content='Read-only source.')
            input_path=root/'read.json';input_path.write_text(json.dumps({'slug':'source'}))
            before=project_fingerprint(root);stdout=io.StringIO();stderr=io.StringIO()
            with patch('lg_cli.config.read_profiles',return_value={'active':None,'profiles':{}}),\
                    isolated_cli(root),patch.dict(os.environ,{'LITERARYGIANT_REGISTRY_HOME':str(root/'registry')}),\
                    patch('lg_cli.backend.runtime_factory',side_effect=AssertionError('CLI read started a runtime')),\
                    patch('sys.stdin',io.StringIO()),redirect_stdout(stdout),redirect_stderr(stderr):
                code=main(['-C',str(root),'studio','source-impact','--input',str(input_path),'--json'])
            self.assertEqual(code,0,stderr.getvalue()+stdout.getvalue())
            self.assertEqual(json.loads(stdout.getvalue()),store.source_impact(source.slug))
            self.assertEqual(project_fingerprint(root),before)
