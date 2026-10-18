"""Run-bound source capture uses the same private socket as real LG MCP tools."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lg_cli.backend import Backend
from lg_cli.generation_sources import bind_generation_sources, make_observation, make_receipt, canonical_sha
from lg_cli.project_store import ProjectStore
from lg_cli.studio_operations import invoke
from lg_cli.workflow_control import control_request
from lg_cli.writing_mcp import create_server
from tests.helpers import make_config
from tests.test_backend import FakeAgent, start_waiting_run


class LiveSourceTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=ProjectStore(self.root);self.store.initialize(name='Transport capture')
        self.document=self.store.create_document(kind='chapter',slug='one',title='One',content='Original author text.',state='accepted',sequence=1)
        self.config=make_config(self.root);self.events=[]
        self.backend=Backend(self.config,self.events.append,agent_factory=FakeAgent)
        self.key=(await self.backend.dispatch('session/create',{}))['sessionId']
        self.run=await start_waiting_run(self.backend,self.key)
        self.state=self.backend.session(self.key);self.endpoint=await self.state['control'].endpoint()

    async def asyncTearDown(self):
        await self.backend.close();self.tmp.cleanup()

    async def socket(self,action,**params):
        return await asyncio.to_thread(control_request,self.endpoint,action,**params)

    async def tool(self,name,arguments=None):
        server=create_server(self.config,control_socket=self.endpoint)
        return await asyncio.to_thread(lambda:asyncio.run(invoke(server,name,arguments or {})))

    def draft(self,content='New candidate.'):
        return {'slug':'one','title':'One','sequence':1,'content':content,
            'expected_version_id':self.document.active_version_id,'reason':'Synthetic author request.'}

    def receipt(self):
        return self.state['agent'].runtime.read()['generation_sources']

    async def test_actual_return_is_recorded_before_delivery_and_does_not_write_story_database(self):
        before=(self.root/'.literarygiant/story.sqlite3').read_bytes()
        result=await self.tool('project_memory')
        receipt=self.receipt()
        self.assertEqual(receipt['run_id'],self.run['runId'])
        self.assertEqual(receipt['coverage'],'partial')
        self.assertEqual(receipt['observations'],[make_observation('project_memory',result)])
        self.assertEqual(receipt['observations'][0]['payload_sha256'],canonical_sha(result))
        self.assertEqual((self.root/'.literarygiant/story.sqlite3').read_bytes(),before)
        self.assertIn('prior_history_unobserved',receipt['unknown_reasons'])
        with self.assertRaises(Exception):await self.tool('read_document',{'slug':'not-found'})
        self.assertEqual(self.state['source_leases'],{})
        self.assertEqual(len(self.receipt()['observations']),1)

    async def test_single_use_lease_rejects_mismatched_tool_and_cross_session(self):
        lease=await self.socket('source_begin',tool='project_memory')
        bad=make_observation('read_document',{})
        with self.assertRaisesRegex(ValueError,'Invalid generation source'):
            await self.socket('source_observed',lease=lease,observation=bad)
        with self.assertRaisesRegex(ValueError,'expired'):
            await self.socket('source_observed',lease=lease,observation=make_observation('project_memory',{}))
        lease=await self.socket('source_begin',tool='read_document')
        other=(await self.backend.dispatch('session/create',{}))['sessionId']
        other_run=await start_waiting_run(self.backend,other)
        other_endpoint=await self.backend.session(other)['control'].endpoint()
        with self.assertRaisesRegex(ValueError,'expired'):
            await asyncio.to_thread(control_request,other_endpoint,'source_observed',lease=lease,
                observation=make_observation('read_document',{}))
        self.assertNotEqual(other_run['runId'],self.run['runId'])
        self.assertEqual(self.receipt()['observations'],[])

    async def test_cancel_new_run_and_reopen_never_reuse_old_observations_or_lease(self):
        await self.tool('read_document',{'slug':'one'})
        lease=await self.socket('source_begin',tool='project_memory')
        await self.backend.cancel(self.key)
        await self.backend.tasks[self.run['runId']][1]
        with self.assertRaises((ValueError,OSError)):
            await self.socket('source_observed',lease=lease,observation=make_observation('project_memory',{}))
        with self.assertRaisesRegex(ValueError,'no longer active'):
            await self.state['control'].source_handler('source_observed',{'lease':lease,
                'observation':make_observation('project_memory',{})})
        run=await start_waiting_run(self.backend,self.key)
        self.endpoint=await self.state['control'].endpoint()
        self.assertEqual(self.receipt()['run_id'],run['runId'])
        self.assertEqual(self.receipt()['observations'],[]);self.assertEqual(self.receipt()['coverage'],'unknown')
        with self.assertRaisesRegex(ValueError,'expired'):
            await self.socket('source_observed',lease=lease,observation=make_observation('project_memory',{}))
        await self.backend.close()
        self.backend=Backend(self.config,self.events.append,agent_factory=FakeAgent)
        await self.backend.open(self.key)
        run=await start_waiting_run(self.backend,self.key);self.state=self.backend.session(self.key)
        self.endpoint=await self.state['control'].endpoint()
        self.assertEqual(self.receipt()['run_id'],run['runId']);self.assertEqual(self.receipt()['coverage'],'unknown')
        self.assertEqual(self.receipt()['observations'],[])

    async def test_approval_entry_freezes_receipt_before_later_reads(self):
        first=await self.tool('project_memory')
        pending=asyncio.create_task(self.state['control'].approval_handler({'tool':'studio_draft','arguments':self.draft()}))
        for _ in range(100):
            if self.backend.approvals.pending:break
            await asyncio.sleep(.01)
        self.assertTrue(self.backend.approvals.pending)
        await self.tool('read_document',{'slug':'one'})
        self.backend.approvals.resolve(next(iter(self.backend.approvals.pending)),{'decision':'accept'})
        result=await pending
        self.assertEqual(result['generation_sources']['observations'],[make_observation('project_memory',first)])
        self.assertEqual(len(self.receipt()['observations']),2)
        with self.assertRaisesRegex(ValueError,'expired'):
            self.backend.approvals.resolve(next(e['params']['approvalId'] for e in self.events if e['method']=='approval/requested'),{'decision':'accept'})

    async def test_persistence_failure_returns_fixed_tool_error_without_delivering_data(self):
        with patch.object(self.state['agent'].runtime,'update',side_effect=OSError('PRIVATE_RUNTIME_PATH')):
            with self.assertRaises(Exception) as error:
                await self.tool('read_document',{'slug':'one'})
        self.assertIn('capture failed',str(error.exception));self.assertNotIn('PRIVATE_RUNTIME_PATH',str(error.exception))
        self.assertNotIn('Original author text',str(error.exception))
        self.assertEqual(self.receipt()['observations'],[])
        self.assertEqual(len(self.store.list_versions('one')),1)

    async def test_capture_limits_are_explicit_and_ledger_remains_bounded(self):
        handler=self.state['control'].source_handler
        oversized=make_observation('project_memory',{'documents':[{'id':number,'active_version_id':number}
            for number in range(1,20001)]})
        self.assertGreater(len(json.dumps([oversized],ensure_ascii=False).encode()),1024*1024)
        lease=await self.socket('source_begin',tool='project_memory')
        result=await self.socket('source_observed',lease=lease,observation=oversized)
        self.assertTrue(result['truncated']);self.assertEqual(self.receipt()['observations'],[])
        self.assertEqual(self.receipt()['coverage'],'unknown')
        self.assertLess(self.state['agent'].runtime.path.stat().st_size,10000)
        observation=make_observation('studio_status',{'status':'synthetic'})
        for _ in range(101):
            lease=await handler('source_begin',{'tool':'studio_status'})
            result=await handler('source_observed',{'lease':lease,'observation':observation})
        self.assertTrue(result['truncated']);self.assertFalse(result['recorded'])
        self.assertEqual(len(self.receipt()['observations']),100)
        self.assertIn('additional_read_observations_omitted_by_capture_limit',self.receipt()['unknown_reasons'])
        self.assertEqual(self.receipt()['coverage'],'partial')

    async def test_write_injects_frozen_read_receipt_without_model_source_arguments(self):
        observed=await self.tool('project_memory')
        self.state['agent'].runtime.update(mode='full_access')
        result=await self.tool('studio_draft',self.draft())
        version=self.store.get_version('one',result['version']['version_number'])
        self.assertEqual(version.metadata['generation_sources']['observations'],[make_observation('project_memory',observed)])
        self.assertEqual(version.metadata['generation_sources']['run_id'],self.run['runId'])
        tools={tool.name:tool for tool in await create_server(self.config).list_tools()}
        self.assertNotIn('generation_sources',tools['studio_draft'].inputSchema['properties'])
        self.assertEqual(self.store.get_document('one').active_version_id,self.document.active_version_id)

    async def test_direct_author_calls_clear_ambient_receipt_and_restore_inherits_saved_source(self):
        old=make_receipt(mode='live',run_id='old-source-run',observations=[make_observation('read_document',{'id':1,'active_version_id':1})])
        with bind_generation_sources(old):
            saved=self.store.create_version('one',content='Old source candidate.',expected_version_id=self.document.active_version_id)
        current=make_receipt(mode='live',run_id='unrelated-ambient-run',observations=[])
        async def approve(_details):return {'decision':'accept','generation_sources':current}
        server=create_server(self.config,approval_handler=approve)
        with bind_generation_sources(current):
            result=await invoke(server,'restore_manuscript',{'slug':'one','version_number':saved.version_number,
                'expected_version_id':self.document.active_version_id})
        restored=self.store.get_version('one',result['version_number'])
        self.assertEqual(restored.metadata['generation_sources'],old)
        result=await invoke(server,'restore_manuscript',{'slug':'one','version_number':1,
            'expected_version_id':self.document.active_version_id})
        self.assertNotIn('generation_sources',self.store.get_version('one',result['version_number']).metadata)
        direct=create_server(self.config)
        with bind_generation_sources(current):
            result=await invoke(direct,'studio_draft',self.draft('Explicit author candidate.'))
        self.assertNotIn('generation_sources',self.store.get_version('one',result['version']['version_number']).metadata)
