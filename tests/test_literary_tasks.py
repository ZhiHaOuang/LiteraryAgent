import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from lg_cli.literary_tasks import prepare, start, advance, read
from lg_cli.core_adapter import CoreExecutionResult
from lg_cli.project_store import ProjectStore
from tests.helpers import make_config
from tests.test_story_workflow import StoryAdapter


class TaskAdapter:
    def __init__(self, during=None): self.calls=[]; self.during=during
    def run(self, **kwargs):
        self.calls.append(kwargs)
        if self.during: self.during()
        name=kwargs['output_schema'].name
        payload=({'options':[{'id':str(i),'title':f'Choice {i}','content':f'Distinct direction {i}'}
            for i in range(1,4)]} if name=='literary-options.schema.json' else
            {'chapter':'A new provisional chapter based on the chosen direction.','summary':'Draft ready.',
                'risks':[]})
        return CoreExecutionResult(ok=True,used_stub=False,output_text=json.dumps(payload),error=None,
            command_name='synthetic',command=[],returncode=0,stdout='',stderr='',duration_seconds=0)


class LiteraryTaskTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.store=ProjectStore(self.root);self.store.initialize(name='Task tests')
        self.document=self.store.create_document(kind='chapter',slug='one',title='One',
            content='Prefix. SELECTED. Suffix.',state='accepted',sequence=1)
        self.config=replace(make_config(self.root),runtime_engine='native',protocol='responses',default_model='test')
        self.options={'strategy':'chapter','document':'one','request':'Write a chapter about resolving the siege.',
            'plot_disagreement':True}

    def choose(self, result):
        decision=result['decision']
        return self.store.select_decision_option(decision['slug'],options_version_number=decision['optionsVersion'],
            option_id='2',expected_version_id=decision['headVersion'])

    def test_disagreement_waits_for_exact_author_choice_then_creates_one_candidate(self):
        adapter=TaskAdapter();first=start(self.config,self.options,adapter=adapter)
        self.assertEqual(first['status'],'waiting_author',first)
        self.assertEqual(len(adapter.calls),1)
        self.assertIsNone(self.store.decision_snapshot(first['decision']['slug'])['selection'])
        waiting=advance(self.config,first['runId'],adapter=adapter)
        self.assertEqual(waiting['status'],'waiting_author')
        self.assertEqual(len(adapter.calls),1)
        self.choose(first)
        final=advance(self.config,first['runId'],expected_fingerprint=waiting['fingerprint'],adapter=adapter)
        self.assertEqual(final['status'],'awaiting_review',final)
        self.assertEqual(len(adapter.calls),2)
        version=self.store.get_version('one',final['candidate']['versionNumber'])
        self.assertEqual(version.metadata['base_version_id'],self.document.active_version_id)
        self.assertIn('generation_sources',version.metadata)
        self.assertEqual(version.metadata['required_decision']['document_id'],
            self.store.decision_snapshot(first['decision']['slug'])['document_id'])
        from lg_cli.generation_sources import canonical_sha
        self.assertEqual(version.metadata['required_decision']['selection_sha256'],canonical_sha(
            self.store.decision_snapshot(first['decision']['slug'])['selection']))
        self.assertEqual(self.store.get_document('one').content,self.document.content)
        self.assertTrue(Path(final['artifact']).is_file())
        again=advance(self.config,first['runId'],adapter=adapter)
        self.assertEqual(again['candidate'],final['candidate'])
        self.assertEqual(len(adapter.calls),2)

    def test_polish_is_one_stage_and_preserves_unselected_text(self):
        adapter=StoryAdapter(revision_text='REVISED.')
        result=start(self.config,{'strategy':'polish','document':'one','passage':'SELECTED.',
            'request':'Polish only the selected passage.'},adapter=adapter)
        self.assertEqual(result['status'],'awaiting_review',result)
        self.assertEqual(adapter.calls,['revision.schema.json'])
        version=self.store.get_version('one',result['candidate']['versionNumber'])
        self.assertEqual(version.content,'Prefix. REVISED. Suffix.')
        self.assertEqual(self.store.get_document('one').content,self.document.content)

    def test_stale_base_or_changed_provider_stops_before_second_model_call(self):
        for change in ('base','provider'):
            with self.subTest(change=change):
                adapter=TaskAdapter();first=start(self.config,self.options,adapter=adapter);self.choose(first)
                config=self.config
                if change=='base':
                    self.store.edit_active_document('one',content='Author changed.',
                        expected_version_id=self.store.get_document('one').active_version_id,reason='author')
                else: config=replace(config,provider='other')
                result=advance(config,first['runId'],adapter=adapter)
                self.assertEqual(result['status'],'stopped',result)
                self.assertEqual(len(adapter.calls),1)

    def test_regenerated_options_do_not_satisfy_original_wait(self):
        adapter=TaskAdapter();first=start(self.config,self.options,adapter=adapter)
        value=self.store.decision_snapshot(first['decision']['slug'])
        self.store.save_decision_options(slug=value['slug'],title=value['title'],topic=value['topic'],
            options=value['options'],sources=value['sources'],request='Changed request',expected_version_id=value['version_id'])
        result=advance(self.config,first['runId'],adapter=adapter)
        self.assertEqual(result['status'],'stopped',result)
        self.assertEqual(len(adapter.calls),1)

    def test_model_budget_and_cancellation_never_create_prose(self):
        adapter=TaskAdapter();first=start(self.config,{**self.options,'maxModelCalls':1},adapter=adapter)
        self.choose(first)
        result=advance(self.config,first['runId'],adapter=adapter)
        self.assertEqual(result['status'],'stopped')
        self.assertEqual(result['reason'],'model_call_budget_exhausted')
        self.assertEqual(len(self.store.list_versions('one')),1)
        cancelled=start(self.config,self.options,adapter=adapter,cancelled=lambda:True)
        self.assertEqual(cancelled['status'],'stopped')
        self.assertEqual(len(adapter.calls),1)

    def test_cancel_during_model_suppresses_decision_or_candidate_writes(self):
        cancelled=[];adapter=TaskAdapter(during=lambda:cancelled.append(True))
        result=start(self.config,self.options,adapter=adapter,cancelled=lambda:bool(cancelled))
        self.assertEqual(result['status'],'stopped',result)
        self.assertEqual(self.store.list_documents(kind='creative_decision'),[])
        self.assertEqual(len(self.store.list_versions('one')),1)

    def test_preview_empty_request_scope_and_context_budget_are_checked(self):
        adapter=TaskAdapter()
        with self.assertRaises(ValueError): prepare(self.config,{**self.options,'request':' '})
        with self.assertRaises(ValueError): prepare(self.config,{'strategy':'polish','document':'one','request':'edit','passage':'missing'})
        result=start(self.config,{**self.options,'maxInputChars':100},adapter=adapter)
        self.assertEqual(result['status'],'stopped')
        self.assertEqual(adapter.calls,[])
        preview=prepare(self.config,self.options)
        self.store.edit_active_document('one',content='Changed',expected_version_id=self.document.active_version_id,reason='author')
        result=start(self.config,self.options,adapter=adapter,expected_fingerprint=preview['fingerprint'])
        self.assertEqual(result['status'],'stopped')
        self.assertEqual(adapter.calls,[])

    def test_empty_chapter_container_can_generate_from_explicit_author_request(self):
        self.store.create_document(kind='chapter',slug='empty',title='New chapter',content='',sequence=2)
        adapter=TaskAdapter()
        result=start(self.config,{**self.options,'document':'empty','plot_disagreement':False},adapter=adapter)
        self.assertEqual(result['status'],'awaiting_review',result)
        self.assertEqual(len(adapter.calls),1)
        self.assertEqual(self.store.get_document('empty').content,'')

    def test_polish_no_progress_stops_without_candidate(self):
        adapter=StoryAdapter(revision_text='SELECTED.')
        result=start(self.config,{'strategy':'polish','document':'one','passage':'SELECTED.',
            'request':'Polish the selected words.'},adapter=adapter)
        self.assertEqual(result['status'],'stopped',result)
        self.assertEqual(result['reason'],'no_textual_progress')
        self.assertEqual(len(self.store.list_versions('one')),1)

    def test_uncertain_execution_is_inspect_only_and_not_replayed(self):
        from lg_cli.literary_tasks import _load, _save
        adapter=TaskAdapter();first=start(self.config,self.options,adapter=adapter)
        state=_load(self.config,first['runId']);state['status']='executing';_save(self.config,state)
        self.assertEqual(read(self.config,first['runId'])['status'],'interrupted')
        result=advance(self.config,first['runId'],adapter=adapter)
        self.assertEqual(result['status'],'interrupted')
        self.assertEqual(result['nextAction'],'inspect_run')
        self.assertEqual(len(adapter.calls),1)

    def test_declined_or_changed_selection_cannot_authorize_generation(self):
        adapter=TaskAdapter();first=start(self.config,self.options,adapter=adapter)
        chosen=self.choose(first)
        self.store.withdraw_decision(chosen['slug'],expected_version_id=chosen['version_id'])
        result=advance(self.config,first['runId'],adapter=adapter)
        self.assertEqual(result['status'],'stopped')
        self.assertEqual(len(adapter.calls),1)

    def test_actual_prepared_polish_input_budget_is_enforced_before_adapter(self):
        adapter=StoryAdapter(revision_text='REVISED.')
        result=start(self.config,{'strategy':'polish','document':'one','passage':'SELECTED.',
            'request':'Polish the selection.','maxInputChars':1000},adapter=adapter)
        self.assertEqual(result['status'],'stopped',result)
        self.assertEqual(result['reason'],'input_char_budget_exceeded')
        self.assertEqual(adapter.calls,[])
        self.assertEqual(len(self.store.list_versions('one')),1)

    def test_long_chapter_short_selection_uses_actual_prepared_prompt_budget(self):
        source='Before '*4000+'SELECTED.'+' After'*4000
        self.store.edit_active_document('one',content=source,expected_version_id=self.document.active_version_id,reason='author')
        adapter=StoryAdapter(revision_text='REVISED.')
        result=start(self.config,{'strategy':'polish','document':'one','passage':'SELECTED.',
            'request':'Polish only these words.','maxInputChars':24000},adapter=adapter)
        self.assertEqual(result['status'],'awaiting_review',result)
        self.assertEqual(adapter.calls,['revision.schema.json'])
        self.assertEqual(self.store.get_version('one',result['candidate']['versionNumber']).content,
            source.replace('SELECTED.','REVISED.'))

    def test_overlapping_passage_is_rejected_by_preview(self):
        self.store.edit_active_document('one',content='aaaa',expected_version_id=self.document.active_version_id,reason='author')
        with self.assertRaises(ValueError):
            prepare(self.config,{'strategy':'polish','document':'one','passage':'aaa','request':'Polish this.'})

    def test_validate_current_rejects_changed_empty_container_before_direction_selection(self):
        from lg_cli.literary_tasks import validate_current
        empty=self.store.create_document(kind='chapter',slug='empty',title='New',content='',sequence=2)
        first=start(self.config,{**self.options,'document':'empty'},adapter=TaskAdapter())
        self.assertEqual(first['status'],'waiting_author',first)
        self.assertTrue(first['contextBasis']['empty'])
        self.assertFalse(first['contextBasis']['sourceIsCanonical'])
        decision=self.store.decision_snapshot(first['decision']['slug'])
        self.assertEqual(decision['sources'],[{'document_id':empty.id,'version_id':empty.active_version_id}])
        self.assertEqual(validate_current(self.config,first['runId'])['status'],'waiting_author')
        self.store.edit_active_document('empty',content='Author added prose.',
            expected_version_id=empty.active_version_id,reason='author')
        with self.assertRaisesRegex(ValueError,'source_changed'):
            validate_current(self.config,first['runId'])

    def test_read_derives_adoption_without_rewriting_task_or_repeating_generation(self):
        from lg_cli.run_store import RunStore
        adapter=TaskAdapter()
        task=start(self.config,{**self.options,'plot_disagreement':False},adapter=adapter)
        path=RunStore(self.root)._path(task['runId'],'literary-task.json')
        before=path.read_bytes()
        self.store.accept_version('one',task['candidate']['versionNumber'],
            expected_version_id=self.document.active_version_id)
        public=read(self.config,task['runId'])
        self.assertEqual(public['status'],'adopted')
        self.assertEqual(public['nextAction'],'none')
        self.assertEqual(path.read_bytes(),before)
        advance(self.config,task['runId'],adapter=adapter)
        self.assertEqual(len(adapter.calls),1)

    def test_read_reports_stale_candidate_base_and_keeps_saved_identity(self):
        task=start(self.config,{**self.options,'plot_disagreement':False},adapter=TaskAdapter())
        self.store.edit_active_document('one',content='Author update after draft.',
            expected_version_id=self.document.active_version_id,reason='author')
        public=read(self.config,task['runId'])
        self.assertTrue(public['sourceStale'])
        self.assertEqual(public['sourceState'],'stale')
        self.assertEqual(public['candidate'],task['candidate'])
        self.assertEqual(public['nextAction'],'new_task')
