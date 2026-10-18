"""Context reads preserve author state while exposing provenance and bounded prose order."""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from lg_cli.project_store import ProjectStore
from lg_cli.story_context import render_story_context
from lg_cli.story_workflow import StoryWorkflowService
from lg_cli.writing_mcp import create_server
from tests.helpers import make_config
from tests.test_story_workflow import StoryAdapter


class ContextProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=ProjectStore(self.root)
        self.store.initialize(name='Synthetic context')

    def chapter(self,slug,sequence,content='Accepted prose',**kwargs):
        return self.store.create_document(kind='chapter',slug=slug,title=slug,sequence=sequence,
            content=content,state='accepted',**kwargs)

    def memory(self):
        async def call():
            result=await create_server(make_config(self.root)).call_tool('project_memory',{})
            return result[1] if isinstance(result,tuple) else json.loads(result[0].text)
        return asyncio.run(call())

    def test_memory_separates_states_without_changing_them(self):
        facts=[self.store.add_fact(category='test',key=state,value=state,state=state)
            for state in ('canonical','idea','archived','rejected')]
        memory=self.memory()
        self.assertEqual([f['state'] for f in memory['facts']],['canonical'])
        self.assertEqual([f['state'] for f in memory['ideas']],['idea'])
        self.assertEqual(memory['facts'][0]['source']['status'],'unbound')
        self.assertFalse(memory['facts'][0]['source']['needs_review'])
        self.assertEqual(memory['view']['purpose'],'author_global')
        self.assertFalse(memory['view']['knowledge_filter_applied'])
        self.assertEqual([self.store.get_fact(f.id) for f in facts],facts)

    def test_changed_source_is_reviewable_without_downgrading_confirmed_fact(self):
        chapter=self.chapter('source',1,'The father died.')
        fact=self.store.add_fact(category='world',key='father',value='The father died.',
            state='canonical',source_document=chapter.id)
        history=self.store.fact_history(fact.id)
        self.assertEqual(self.store.fact_sources([fact])[fact.id]['status'],'current')
        version=self.store.edit_active_document(chapter.id,content='The father survived.',
            expected_version_id=chapter.active_version_id,reason='Author correction')
        snapshot=self.store.context_snapshot();source=snapshot['fact_sources'][fact.id]
        self.assertEqual(source['status'],'stale');self.assertTrue(source['needs_review'])
        self.assertEqual(source['versionId'],chapter.active_version_id)
        self.assertEqual(source['activeVersionId'],version.id)
        self.assertIn(fact,snapshot['canonical_facts'])
        rendered=render_story_context(snapshot)
        self.assertIn('source=stale NEEDS SOURCE REVIEW',rendered)
        self.assertIn(fact.value,rendered)
        self.assertNotIn('established project truth',rendered)
        self.assertEqual(self.memory()['facts'][0]['source']['status'],'stale')
        self.assertEqual(self.store.get_fact(fact.id),fact)
        self.assertEqual(self.store.fact_history(fact.id),history)

    def test_pending_and_archived_sources_are_not_current_evidence(self):
        draft=self.store.create_document(kind='chapter',slug='draft',title='Draft',content='Draft idea')
        pending=self.store.add_fact(category='world',key='draft',value='Draft idea',source_document=draft.id)
        archived=self.store.create_document(kind='note',slug='archive',title='Archive',content='Old',state='archived')
        old=self.store.add_fact(category='world',key='old',value='Old',source_document=archived.id)
        sources=self.store.fact_sources([pending,old])
        self.assertEqual(sources[pending.id]['status'],'pending')
        self.assertEqual(sources[old.id]['status'],'unavailable')
        self.assertTrue(all(s['needs_review'] for s in sources.values()))

    def test_scene_selection_excludes_later_prose_before_applying_budget(self):
        previous=self.chapter('previous',1,'NEEDED_PRIOR needle')
        current=self.chapter('current',2,'CURRENT_WHOLE_CHAPTER_WITH_LATE_SCENES')
        scene=self.store.create_scene(slug='target',title='needle',chapter=current.id,sequence=2)
        before=self.store.create_scene(slug='before',title='Before',chapter=current.id,sequence=1)
        self.store.create_version(before.document.id,content='NEEDED_SIBLING',state='accepted',activate=True)
        after=self.store.create_scene(slug='after',title='needle',chapter=current.id,sequence=3)
        self.store.create_version(after.document.id,content='FUTURE_SIBLING needle',state='accepted',activate=True)
        for index in range(20):self.chapter(f'future-{index}',index+3,'FUTURE_TEXT needle')
        for fallback in (False,True):
            with self.subTest(fallback=fallback):
                context=patch('lg_cli.project_store._fts_query',side_effect=sqlite3.OperationalError('No FTS')) if fallback else nullcontext()
                with context:snapshot=self.store.context_snapshot(scene=scene.document.id,query='needle',max_documents=2)
                self.assertEqual({d.id for d in snapshot['documents']},{previous.id,before.document.id})
                text=render_story_context(snapshot)
                self.assertNotIn('FUTURE_',text);self.assertNotIn('CURRENT_WHOLE_',text)
                self.assertIn('NEEDED_PRIOR',text);self.assertIn('NEEDED_SIBLING',text)
                self.assertEqual(snapshot['view']['target']['version_id'],scene.document.active_version_id)

    def test_unknown_order_does_not_use_database_ids_as_story_order(self):
        chapter=self.chapter('chapter',None,'WHOLE_UNKNOWN_CHAPTER')
        before=self.store.create_scene(slug='before',title='Before',chapter=chapter.id,sequence=1)
        self.store.create_version(before.document.id,content='EARLIER_DATABASE_ID',state='accepted',activate=True)
        target=self.store.create_scene(slug='target',title='Target',chapter=chapter.id)
        self.chapter('other',1,'OTHER_UNKNOWN_POSITION')
        snapshot=self.store.context_snapshot(scene=target.document.id)
        self.assertFalse(snapshot['documents'])
        self.assertIn('complete_prose_order',snapshot['view']['missing'])
        orphan=self.store.create_scene(slug='orphan',title='Orphan',sequence=10)
        self.assertFalse(self.store.context_snapshot(scene=orphan.document.id)['documents'])

    def test_numbering_in_other_volume_and_archived_siblings_are_not_prior_text(self):
        first=self.store.create_document(kind='part',slug='part1',title='Part 1',sequence=1)
        other=self.store.create_document(kind='part',slug='part2',title='Part 2',sequence=2)
        chapter=self.chapter('chapter',5,parent=first.id)
        self.chapter('other-volume',1,'OTHER_VOLUME_NUMBER_ONE',parent=other.id)
        archived=self.store.create_scene(slug='archived',title='Archived',chapter=chapter.id,sequence=1)
        self.store.create_version(archived.document.id,content='ARCHIVED_SIBLING',state='archived',activate=True)
        target=self.store.create_scene(slug='target',title='Target',chapter=chapter.id,sequence=2)
        snapshot=self.store.context_snapshot(scene=target.document.id)
        text=render_story_context(snapshot)
        self.assertNotIn('OTHER_VOLUME_',text);self.assertNotIn('ARCHIVED_SIBLING',text)
        self.assertIn('cross_volume_order',snapshot['view']['missing'])

    def test_global_author_planning_retains_later_text_and_scene_references_are_explicit(self):
        early=self.chapter('early',1)
        future=self.chapter('future',20,'FUTURE_REVEAL')
        plan=self.store.create_document(kind='outline',slug='outline',title='Outline',content='AUTHOR_FUTURE_PLAN')
        scene=self.store.create_scene(slug='scene',title='Scene',chapter=early.id,sequence=1,pov='Lin',time_label='Flashback')
        self.assertIn(future.id,[d.id for d in self.store.context_snapshot()['documents']])
        snapshot=self.store.context_snapshot(scene=scene.document.id)
        self.assertIn(plan.id,[d.id for d in snapshot['documents']])
        self.assertEqual(snapshot['view']['reference_scope'],'author_global')
        self.assertFalse(snapshot['view']['knowledge_filter_applied'])
        self.assertIn('not a character or reader knowledge view',render_story_context(snapshot))
        self.assertEqual(snapshot['view']['target']['story_time_label'],'Flashback')

    def test_selection_reports_omitted_facts_and_text_without_mutation(self):
        for index in range(3):
            self.store.set_fact(category='world',key=str(index),value='Known')
            self.chapter(f'chapter-{index}',index,'Text')
        snapshot=self.store.context_snapshot(max_documents=1,max_facts=1)
        self.assertTrue(snapshot['view']['truncated']['documents'])
        self.assertTrue(snapshot['view']['truncated']['canonical_facts'])
        self.assertEqual(len(snapshot['documents']),1);self.assertEqual(len(snapshot['canonical_facts']),1)
        self.assertEqual(len(self.store.list_facts(state='canonical')),3)

    def test_scene_workflow_model_input_receives_source_warning_and_no_later_prose(self):
        chapter=self.chapter('early',1,'Old source')
        fact=self.store.set_fact(category='world',key='claim',value='Old claim',source_document=chapter.id)
        self.store.edit_active_document(chapter.id,content='Changed source',expected_version_id=chapter.active_version_id,reason='Author edit')
        self.chapter('future',20,'MODEL_MUST_NOT_RECEIVE_FUTURE_PROSE')
        scene=self.store.create_scene(slug='target',title='Target',chapter=chapter.id,sequence=1)
        adapter=StoryAdapter()
        result=StoryWorkflowService(make_config(self.root),project=self.store,adapter=adapter).plan_scene(scene.document.id)
        self.assertTrue(result.ok,result.error)
        self.assertEqual(len(adapter.prompts),1)
        self.assertNotIn('MODEL_MUST_NOT_RECEIVE_FUTURE_PROSE',adapter.prompts[0])
        self.assertIn('source=stale NEEDS SOURCE REVIEW',adapter.prompts[0])
        self.assertIn('author_scene_support',adapter.prompts[0])
        self.assertEqual(self.store.get_fact(fact.id),fact)

    def test_archived_or_rejected_parent_excludes_accepted_child_text(self):
        for state in ('archived','rejected'):
            parent=self.store.create_document(kind='chapter',slug=state,title=state,sequence=1,state=state)
            child=self.store.create_scene(slug=state+'-child',title='Child',chapter=parent.id,sequence=1)
            self.store.create_version(child.document.id,content='INACTIVE_PARENT_PROSE',state='accepted',activate=True)
        chapter=self.chapter('current',2)
        target=self.store.create_scene(slug='target',title='Target',chapter=chapter.id,sequence=1)
        self.assertNotIn('INACTIVE_PARENT_PROSE',render_story_context(self.store.context_snapshot(scene=target.document.id)))

    def test_empty_cards_cannot_evict_nonempty_prior_chapter(self):
        prior=self.chapter('prior',1,'NEEDED_PREVIOUS_CHAPTER')
        current=self.chapter('current',2)
        for index in (1,2):self.store.create_scene(slug=f'empty-{index}',title='Empty',chapter=current.id,sequence=index)
        target=self.store.create_scene(slug='target',title='Target',chapter=current.id,sequence=3)
        snapshot=self.store.context_snapshot(scene=target.document.id,max_documents=2)
        self.assertEqual(snapshot['documents'][0].id,prior.id)
        self.assertEqual(len(snapshot['documents']),2)
        self.assertFalse(snapshot['documents'][1].content)

    def test_full_fact_budget_preserves_prior_text_and_announces_truncation(self):
        for index in range(80):self.store.set_fact(category='world',key=str(index),value='Long confirmed fact. '*30)
        for index in range(30):self.store.add_fact(category='idea',key=str(index),value='Optional idea. '*30)
        for index in range(1,4):self.chapter(f'previous-{index}',index,'NEEDED_PRIOR_TEXT '+('Long prose. '*200))
        current=self.chapter('current',4)
        target=self.store.create_scene(slug='target',title='Target',chapter=current.id,sequence=1)
        snapshot=self.store.context_snapshot(scene=target.document.id)
        self.assertEqual(len(snapshot['documents']),3)
        for budget in (2000,4000,24000):
            text=render_story_context(snapshot,max_chars=budget)
            self.assertLessEqual(len(text),budget)
            self.assertIn('NEEDED_PRIOR_TEXT',text)
            self.assertIn('LG omitted context',text)
        adapter=StoryAdapter()
        result=StoryWorkflowService(make_config(self.root),project=self.store,adapter=adapter).plan_scene(target.document.id)
        self.assertTrue(result.ok,result.error)
        self.assertIn('NEEDED_PRIOR_TEXT',adapter.prompts[0])

    def test_reference_documents_do_not_hide_selected_prior_prose(self):
        for kind in ("outline", "world", "character", "style"):
            self.store.create_document(
                kind=kind, slug=kind, title=kind, content=f"REFERENCE_{kind}"
            )
        prior = self.chapter("prior", 1, "NEEDED_PRIOR_PROSE")
        current = self.chapter("current", 2, "CURRENT_CHAPTER_NOT_PRIOR")
        target = self.store.create_scene(
            slug="target", title="Target", chapter=current.id, sequence=1
        )
        snapshot = self.store.context_snapshot(scene=target.document.id)
        self.assertIn(prior.id, [document.id for document in snapshot["documents"]])
        for budget in (2000, 4000, 24000):
            with self.subTest(budget=budget):
                text = render_story_context(snapshot, max_chars=budget)
                self.assertLessEqual(len(text), budget)
                self.assertIn("NEEDED_PRIOR_PROSE", text)
                self.assertNotIn("CURRENT_CHAPTER_NOT_PRIOR", text)
                if budget == 24000:
                    for kind in ("outline", "world", "character", "style"):
                        self.assertIn(f"REFERENCE_{kind}", text)
        adapter = StoryAdapter()
        result = StoryWorkflowService(
            make_config(self.root), project=self.store, adapter=adapter
        ).plan_scene(target.document.id)
        self.assertTrue(result.ok, result.error)
        self.assertIn("NEEDED_PRIOR_PROSE", adapter.prompts[0])

    def test_revision_context_identifies_the_selected_candidate_version(self):
        self.store.create_document(kind='note',slug='offset',title='Offset')
        scene=self.store.create_scene(slug='target',title='Target')
        candidate=self.store.create_version(scene.document.id,content='SELECTED_CANDIDATE')
        self.assertNotEqual(candidate.id,candidate.version_number)
        adapter=StoryAdapter()
        result=StoryWorkflowService(make_config(self.root),project=self.store,adapter=adapter).revise_document(
            scene.document.id,mode='light',source_version=candidate.version_number)
        self.assertTrue(result.ok,result.error)
        context=self.store.context_snapshot(scene=scene.document.id,scene_version=candidate.version_number)
        self.assertEqual(context['view']['target']['version_id'],candidate.id)
        self.assertEqual(context['view']['target']['active_version_id'],scene.document.active_version_id)
        self.assertIn(f'"version_id": {candidate.id}',adapter.prompts[0])
