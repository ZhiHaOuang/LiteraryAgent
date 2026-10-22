"""R5's local polish contract crosses the real Native HTTP boundary."""
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from lg_cli.generation_sources import text_sha
from lg_cli.literary_tasks import _load, _save, advance, prepare, read, start
from lg_cli.native_model import NativeModel
from lg_cli.run_store import RunStore
from lg_cli.story_workflow import POLISH_PROFILE, StoryWorkflowService
from tests import test_execution_adapter as fixtures


class PolishProfileTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.NativeStageTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root,self.config,self.store=self.fixture.root,self.fixture.config,self.fixture.project
        self.source=self.store.edit_active_document('one',content='Before. SELECTED. After.',
            expected_version_id=self.fixture.original.active_version_id,reason='synthetic author')
        self.options={'strategy':'polish','document':'one','passage':'SELECTED.',
            'request':'Polish only the selection.'}

    def test_polish_http_contract_omits_whole_book_outputs_and_keeps_exact_scope(self):
        with patch.dict('os.environ',{'LG_WORKFLOW_GUIDANCE':'LATEST_GUIDANCE'}):
            result=start(self.config,self.options)
        self.assertEqual(result['status'],'awaiting_review',result)
        self.assertEqual(len(self.fixture.requests),1)
        request=self.fixture.requests[0]
        schema=request['text']['format']['schema']
        self.assertEqual(set(schema['properties']),{'summary','revision_markdown','change_log','risks'})
        self.assertEqual(set(schema['required']),set(schema['properties']))
        self.assertFalse(schema['additionalProperties'])
        prompt=request['input'][0]['content']
        for unused in ('Include story_index','Book overview:','Assigned Subagent','Assigned Skill','Current Book Analyses'):
            self.assertNotIn(unused,prompt)
        self.assertEqual(prompt.count('LATEST_GUIDANCE'),1)
        self.assertEqual(request['tools'],[])
        self.assertEqual(self.store.get_document('one').active_version_id,self.source.id)
        candidate=self.store.get_version('one',result['candidate']['versionNumber'])
        self.assertTrue(candidate.content.startswith('Before. '))
        self.assertTrue(candidate.content.endswith(' After.'))
        self.assertEqual(candidate.metadata['base_version_id'],self.source.id)
        self.assertIn('generation_sources',candidate.metadata)

    def test_polish_keeps_project_guidance_facts_and_saved_input_receipt_without_side_writes(self):
        fact=self.store.set_fact(category='world',key='gate',value='KNOWN_GATE_FACT')
        config=replace(self.config,project_instructions='PROJECT_RULES')
        from lg_cli.book_overview import save_overview, RELATIVE_PATH
        save_overview(self.root,'作者保留的全书概览',source={'state':'accepted'})
        overview=self.root/RELATIVE_PATH
        before=overview.read_bytes()
        with patch.dict('os.environ',{'LG_WORKFLOW_GUIDANCE':'LATEST_GUIDANCE'}), \
                patch('lg_cli.book_analysis.BookAnalysisStore.context',side_effect=AssertionError('No book analysis for polish')), \
                patch('lg_cli.story_workflow.StoryWorkflowService._store_memory_proposals',side_effect=AssertionError('No proposals for polish')):
            result=start(config,self.options)
        self.assertEqual(result['status'],'awaiting_review',result)
        request=self.fixture.requests[0]
        prompt=request['input'][0]['content']
        for required in ('PROJECT_RULES','LATEST_GUIDANCE','KNOWN_GATE_FACT','<selected_passage>','Canonical facts'):
            self.assertIn(required,prompt)
        self.assertNotIn('作者保留的全书概览',prompt)
        self.assertEqual(overview.read_bytes(),before)
        self.assertEqual(self.store.list_proposals(),[])
        candidate=self.store.get_version('one',result['candidate']['versionNumber'])
        receipt=candidate.metadata['generation_sources']
        self.assertEqual(receipt['input'],{'prompt_sha256':text_sha(prompt),
            'instructions_sha256':text_sha(request['instructions'])})
        refs=[r for o in receipt['observations'] for r in o['refs']]
        self.assertTrue(any(r.get('fact_id')==fact.id for r in refs))
        self.assertTrue(any(r.get('version_id')==self.source.id and r.get('exposure')=='excerpt' for r in refs))
        stages=RunStore(self.root).handle(result['stageRunId']).directory/'stages'
        self.assertEqual((stages/'revision.prompt.md').read_text(),prompt)
        self.assertEqual((stages/'revision.instructions.md').read_text(),request['instructions'])
        self.assertEqual(json.loads((stages/'revision.schema/polish.schema.json').read_text()),request['text']['format']['schema'])

    def test_author_edit_during_real_model_response_stops_without_candidate_or_overwrite(self):
        original=NativeModel.generate
        owner=self
        async def editing(model,request,delta):
            response=await original(model,request,delta)
            owner.store.edit_active_document('one',content='Author changed outside the selection.',
                expected_version_id=owner.source.id,reason='synthetic concurrent author')
            return response
        with patch.object(NativeModel,'generate',editing): result=start(self.config,self.options)
        self.assertEqual(result['status'],'stopped',result)
        self.assertEqual(result['reason'],'source_changed')
        self.assertEqual(self.store.get_document('one').content,'Author changed outside the selection.')
        self.assertFalse(any(v.state=='candidate' for v in self.store.list_versions('one')))
        self.assertEqual(len(self.fixture.requests),1)

    def test_extra_or_missing_output_fields_fail_without_silent_repair(self):
        valid={'summary':'Changed.','revision_markdown':'New selection.','change_log':[],'risks':[]}
        invalid=[{**valid,'memory_proposals':[]},{**valid,'book_overview':'Unrequested'},
            {**valid,'story_index':{}},{k:v for k,v in valid.items() if k!='risks'}]
        for value in invalid:
            with self.subTest(keys=list(value)),patch('tests.test_execution_adapter.specimen',return_value=value):
                result=start(self.config,self.options)
            self.assertEqual(result['status'],'failed',result)
            self.assertNotIn('candidate',result)
        self.assertEqual(len(self.fixture.requests),len(invalid))
        self.assertEqual(self.store.get_document('one').active_version_id,self.source.id)
        self.assertFalse(any(v.state=='candidate' for v in self.store.list_versions('one')))

    def test_default_generic_revision_retains_original_contract(self):
        result=StoryWorkflowService(self.config).revise_document('one',mode='light',passage='SELECTED.')
        self.assertTrue(result.ok,result.error)
        request=self.fixture.requests[0]
        self.assertIn('book_overview',request['text']['format']['schema']['required'])
        self.assertIn('memory_proposals',request['text']['format']['schema']['required'])
        self.assertIn('story_index',request['text']['format']['schema']['properties'])
        self.assertIn('## Assigned Skill',request['input'][0]['content'])
        self.assertIn('Include story_index',request['input'][0]['content'])

    def test_profile_cannot_be_used_for_other_modes_documents_or_historical_versions(self):
        service=StoryWorkflowService(self.config)
        self.store.create_document(kind='world',slug='world',title='World',content='SELECTED.')
        for values in ({'mode':'rewrite','passage':'SELECTED.'}, {'mode':'light'},
                {'mode':'light','passage':'SELECTED.','source_version':1}):
            with self.subTest(values=values),self.assertRaises(ValueError):
                service.revise_document('one',input_profile=POLISH_PROFILE,**values)
        with self.assertRaises(ValueError):
            service.revise_document('world',mode='light',passage='SELECTED.',input_profile=POLISH_PROFILE)
        with self.assertRaises(ValueError):
            service.revise_document('one',mode='light',passage='SELECTED.',input_profile='unknown')
        self.assertEqual(self.fixture.requests,[])

    def test_profile_change_rejects_old_preview_and_during_request(self):
        preview=prepare(self.config,self.options)
        with patch('lg_cli.literary_tasks.polish_profile_fingerprint',return_value='changed'):
            result=start(self.config,self.options,expected_fingerprint=preview['fingerprint'])
        self.assertEqual(result['reason'],'preview_changed')
        self.assertEqual(self.fixture.requests,[])
        original=NativeModel.generate
        change=patch('lg_cli.literary_tasks.polish_profile_fingerprint',return_value='changed')
        self.addCleanup(change.stop)
        async def changing(model,request,delta):
            response=await original(model,request,delta)
            change.start()
            return response
        with patch.object(NativeModel,'generate',changing): result=start(self.config,self.options)
        self.assertEqual(result['status'],'stopped',result)
        self.assertEqual(result['reason'],'input_profile_changed')
        self.assertNotIn('candidate',result)
        self.assertFalse(any(v.state=='candidate' for v in self.store.list_versions('one')))

    def test_legacy_polish_candidate_remains_readable_but_advance_never_reuses_old_profile(self):
        result=start(self.config,self.options)
        state=_load(self.config,result['runId'])
        state.pop('inputProfile')
        _save(self.config,state)  # Simulate a valid pre-profile task snapshot in this synthetic book.
        saved=read(self.config,result['runId'])
        self.assertTrue(saved['inputPolicyChanged'])
        self.assertEqual(saved['candidate'],result['candidate'])
        self.assertEqual(saved['nextAction'],'inspect_saved_candidate')
        resumed=advance(self.config,result['runId'])
        self.assertEqual(resumed['status'],'stopped')
        self.assertEqual(resumed['reason'],'input_profile_changed')
        self.assertEqual(resumed['candidate'],result['candidate'])
        self.assertEqual(len(self.fixture.requests),1)

    def test_missing_profile_resources_preserve_saved_candidate_visibility(self):
        result=start(self.config,self.options)
        with patch('lg_cli.literary_tasks.polish_profile_fingerprint',side_effect=OSError('resource unavailable')):
            saved=read(self.config,result['runId'])
            resumed=advance(self.config,result['runId'])
            with self.assertRaises(OSError): prepare(self.config,self.options)
            with self.assertRaises(OSError): start(self.config,self.options)
        self.assertTrue(saved['inputPolicyChanged'])
        self.assertEqual(saved['candidate'],result['candidate'])
        self.assertEqual(saved['nextAction'],'inspect_saved_candidate')
        self.assertEqual(resumed['reason'],'input_profile_changed')
        self.assertEqual(resumed['candidate'],result['candidate'])
        self.assertEqual(len(self.fixture.requests),1)

    def test_profile_change_never_hides_actual_adoption_on_read_or_advance(self):
        result=start(self.config,self.options)
        from lg_cli.book_analysis import BookAnalysisStore
        from lg_cli.supervision import DEFAULT_CATEGORIES
        records=BookAnalysisStore(self.store)
        candidate=self.store.get_version('one',result['candidate']['versionNumber'])
        base=records.snapshot('one')
        snapshot=records.candidate_snapshot('one',candidate)
        # This status test uses explicit synthetic review records; it does not
        # evaluate literary quality or bypass the production acceptance gate.
        for category in DEFAULT_CATEGORIES:
            records.save(snapshot,category,{'summary':'Synthetic status fixture.','entries':[],'findings':[]},
                'synthetic-adoption-status',candidate_id=candidate.id,base_snapshot=base)
        self.store.accept_version('one',result['candidate']['versionNumber'])
        state=_load(self.config,result['runId'])
        state.pop('inputProfile')
        _save(self.config,state)
        with patch('lg_cli.literary_tasks.polish_profile_fingerprint',side_effect=OSError('resource unavailable')):
            for value in (read(self.config,result['runId']),advance(self.config,result['runId'])):
                self.assertEqual(value['status'],'adopted')
                self.assertEqual(value['nextAction'],'none')
                self.assertTrue(value['inputPolicyChanged'])
                self.assertEqual(value['candidate'],result['candidate'])
        self.assertEqual(len(self.fixture.requests),1)
        self.assertEqual(self.store.get_document('one').active_version_id,result['candidate']['versionId'])

    def test_frozen_schema_change_before_execution_or_after_response_rejects_candidate(self):
        original=StoryWorkflowService._write_prompt
        owner=self
        for timing in ('before_execution','after_response'):
            paths=[]
            def prepare_stage(service,*args,**kwargs):
                value,preview=original(service,*args,**kwargs)
                paths.append(value.schema_path)
                if timing=='before_execution': corrupt()
                return value,preview
            def corrupt():
                value=json.loads(paths[-1].read_text())
                value['description']='Changed frozen contract'
                paths[-1].write_text(json.dumps(value))
            generate=NativeModel.generate
            async def changed_response(model,request,delta):
                response=await generate(model,request,delta)
                if timing=='after_response': corrupt()
                return response
            before=len(self.fixture.requests)
            with self.subTest(timing=timing),patch.object(StoryWorkflowService,'_write_prompt',prepare_stage), \
                    patch.object(NativeModel,'generate',changed_response):
                result=start(self.config,self.options)
            self.assertEqual(result['status'],'failed',result)
            self.assertNotIn('candidate',result)
            self.assertEqual(len(owner.fixture.requests)-before,0 if timing=='before_execution' else 1)
        self.assertFalse(any(v.state=='candidate' for v in self.store.list_versions('one')))
