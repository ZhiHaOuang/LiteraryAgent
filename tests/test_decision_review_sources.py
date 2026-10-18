"""Formal review evidence includes relevant selected plans, separately from facts."""
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from lg_cli.book_analysis import (BookAnalysisStore, ChapterAnalysisService, fingerprint,
    resolve_evidence, source_spans, validate_report)
from lg_cli.core_adapter import CoreExecutionResult
from lg_cli.project_store import ProjectStore, ProjectStoreError
from lg_cli.review_policy import LiteraryReviewPolicy
from lg_cli.supervision import candidate_reports, configure_supervision, DEFAULT_CATEGORIES
from lg_cli.task_scope import bind_task
from tests.helpers import make_config, reference_card


class DecisionReviewSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=ProjectStore(self.root);self.store.initialize(name='Review decision sources')
        self.part=self.store.create_document(kind='part',slug='part',title='Part',content='Part scope.')
        self.other_part=self.store.create_document(kind='part',slug='other-part',title='Other part',content='Other scope.')
        self.chapter=self.store.create_document(kind='chapter',slug='one',title='One',parent=self.part.id,
            sequence=1,content='The protagonist opens the gate to rescue a worker.',state='accepted')
        self.other=self.store.create_document(kind='chapter',slug='other',title='Other',parent=self.other_part.id,
            sequence=1,content='An unrelated chapter stays unchanged.',state='accepted')
        self.fact=self.store.add_fact(category='world',key='gate',value='The gate requires a key.',state='canonical')
        self.records=BookAnalysisStore(self.store)
        self.options=[{'id':key,'title':'Direction '+key,'content':content} for key,content in
            [('A','SELECTED_PLAN_A: rescue the worker before pursuing the saboteur.'),
             ('B','ALTERNATIVE_PLAN_B: pursue the saboteur before returning to the gate.'),
             ('C','ALTERNATIVE_PLAN_C: wait for a team and negotiate passage.')]]

    def save(self,*,slug='direction',scope='work',document=None,key='next-step',sources=None):
        return self.store.save_decision_options(slug=slug,title=slug,
            topic={'scope':scope,'document_id':document,'key':key},options=self.options,sources=sources or [],
            request='Compare three plans, without treating any plan as an established fact.',expected_version_id=0)

    def select(self,saved,option='A'):
        return self.store.select_decision_option(saved['slug'],options_version_number=saved['options_version_number'],
            option_id=option,expected_version_id=saved['version_id'])

    def report(self,*,evidence=None,category='Worldview'):
        return {'summary':'Checked the supplied sources.', 'entries':[{'instance_card':reference_card(category,stored=True),
            'key':'gate','label':'Gate choice','analysis':'Compare observed action with author intention.',
            'evidence':evidence or [{'document_id':self.chapter.id,'version_id':self.chapter.active_version_id,
                'quote':'opens the gate'}]}], 'findings':[]}

    def reviews(self,candidate,*,final=False):
        configure_supervision(self.store)
        base=self.records.snapshot('one');snapshot=self.records.candidate_snapshot('one',candidate,final=final)
        for category in DEFAULT_CATEGORIES:
            self.records.save(snapshot,category,{'summary':'Contract-valid synthetic review.','entries':[],'findings':[]},
                'synthetic-review',candidate_id=candidate.id,base_snapshot=base)
        return snapshot

    def test_no_selected_direction_retains_legacy_snapshot_and_report_hash(self):
        expected={'project_id':self.store.project_info().project_id,'chapter':'one',
            'documents':[{'id':self.chapter.id,'version_id':self.chapter.active_version_id,'title':self.chapter.title,
                'content':self.chapter.content,'state':'accepted'}],'canonical':[self.fact.__dict__]}
        before=self.records.snapshot('one')
        self.assertEqual(before,expected);self.assertNotIn('creative_decisions',before)
        self.records.save(before,'Worldview',self.report(),'old-compatible')
        self.save()
        self.assertEqual(fingerprint(self.records.snapshot('one')),fingerprint(expected))
        self.assertEqual(self.records.list('one')[0]['status'],'current')
        volume=self.records.volume_snapshot()
        self.assertNotIn('creative_decisions',volume)

    def test_selected_change_and_withdrawal_invalidate_formal_reports(self):
        chosen=self.select(self.save())
        snapshot=self.records.snapshot('one');self.records.save(snapshot,'Worldview',self.report(),'A')
        self.assertEqual(snapshot['creative_decisions'][0]['selection']['option_id'],'A')
        self.assertEqual(self.records.list('one')[0]['status'],'current')
        changed=self.select(chosen,'B')
        self.assertEqual(self.records.list('one')[0]['status'],'stale')
        self.assertNotEqual(fingerprint(snapshot),fingerprint(self.records.snapshot('one')))
        snapshot=self.records.snapshot('one');self.records.save(snapshot,'Worldview',self.report(),'B')
        self.store.withdraw_decision(changed['slug'],expected_version_id=changed['version_id'])
        self.assertEqual(self.records.list('one')[0]['status'],'stale')
        self.assertNotIn('creative_decisions',self.records.snapshot('one'))

    def test_regeneration_and_unrelated_chapter_or_part_do_not_change_review_source(self):
        chosen=self.select(self.save())
        snapshot=self.records.snapshot('one');self.records.save(snapshot,'Worldview',self.report(),'stable')
        self.store.save_decision_options(slug=chosen['slug'],title=chosen['title'],topic=chosen['topic'],
            options=[dict(option,content='NEW_OPTION_'+option['id']) for option in self.options],sources=[],
            request='Regenerate alternatives while retaining the current choice.',expected_version_id=chosen['version_id'])
        self.select(self.save(slug='other-chapter-choice',scope='chapter',document=self.other.id,
            sources=[{'document_id':self.other.id,'version_id':self.other.active_version_id}]))
        self.select(self.save(slug='other-part-choice',scope='part',document=self.other_part.id,
            sources=[{'document_id':self.other_part.id,'version_id':self.other_part.active_version_id}]))
        self.assertEqual(self.records.snapshot('one'),snapshot)
        self.assertEqual(self.records.list('one')[0]['status'],'current')
        text=json.dumps(snapshot)
        self.assertNotIn('NEW_OPTION',text);self.assertNotIn('ALTERNATIVE_PLAN_B',text)
        self.assertNotIn('request',snapshot['creative_decisions'][0]['selection'])

    def test_declared_source_basis_change_invalidates_even_when_reviewed_prose_is_unchanged(self):
        source=self.store.create_document(kind='note',slug='basis',title='Basis',content='Author source v1.')
        self.select(self.save(sources=[{'document_id':source.id,'version_id':source.active_version_id}]))
        snapshot=self.records.snapshot('one');self.records.save(snapshot,'Worldview',self.report(),'basis-v1')
        self.store.edit_active_document(source.id,content='Author source v2.',expected_version_id=source.active_version_id,reason='author')
        current=self.records.snapshot('one')
        self.assertEqual(current['documents'],snapshot['documents']);self.assertEqual(current['canonical'],snapshot['canonical'])
        self.assertEqual(current['creative_decisions'][0]['source_status'],'stale')
        self.assertEqual(self.records.list('one')[0]['status'],'stale')

    def test_candidate_projection_matches_accepted_and_final_publication(self):
        sibling=self.store.create_scene(slug='draft-sibling',title='Unchanged draft scene',chapter=self.chapter.id,sequence=1)
        self.assertEqual(sibling.document.state,'draft')
        self.select(self.save(scope='chapter',document=self.chapter.id,
            sources=[{'document_id':self.chapter.id,'version_id':self.chapter.active_version_id}]))
        self.assertEqual(self.records.snapshot('one')['creative_decisions'][0]['source_status'],'current')
        candidate=self.store.create_version('one',content='The protagonist rescues the worker through the gate.',
            expected_version_id=self.chapter.active_version_id)
        projected=self.reviews(candidate)
        sibling_projection=next(doc for doc in projected['documents'] if doc['id']==sibling.document.id)
        self.assertEqual((sibling_projection['state'],sibling_projection['content']),('draft',''))
        self.assertEqual(projected['creative_decisions'][0]['source_status'],'stale')
        self.assertEqual(projected['creative_decisions'][0]['source_details'][0]['active_version_id'],candidate.id)
        self.store.accept_version('one',candidate.version_number,expected_version_id=self.chapter.active_version_id)
        self.assertEqual(self.records.snapshot('one'),projected)
        self.assertTrue(all(row['status']=='current' for row in self.records.list('one')))
        final=self.reviews(self.store.get_version('one',candidate.version_number),final=True)
        self.store.accept_version('one',candidate.version_number,final=True,expected_version_id=candidate.id)
        self.assertEqual(self.records.snapshot('one'),final)
        self.assertEqual(len(self.records.list('one')),5)
        self.assertEqual(self.store.get_document(sibling.document.id),sibling.document)

    def test_old_candidate_needs_current_direction_reviews_before_explicit_adoption(self):
        chosen=self.select(self.save())
        candidate=self.store.create_version('one',content='An existing candidate remains available.',
            expected_version_id=self.chapter.active_version_id)
        self.reviews(candidate)
        self.select(chosen,'B')
        with self.assertRaisesRegex(ProjectStoreError,'stale'):
            candidate_reports(self.store,self.store.get_document('one'),candidate)
        with self.assertRaisesRegex(ProjectStoreError,'stale'):
            self.store.accept_version('one',candidate.version_number,expected_version_id=self.chapter.active_version_id)
        self.assertEqual(self.store.get_document('one').active_version_id,self.chapter.active_version_id)
        snapshot=self.reviews(candidate)
        self.store.accept_version('one',candidate.version_number,expected_version_id=self.chapter.active_version_id)
        self.assertEqual(self.records.snapshot('one'),snapshot)
        self.assertTrue(all(row['status']=='current' for row in self.records.list('one')))

    def test_exact_selected_plan_quotes_only_and_plan_only_canon_conflict_is_rejected(self):
        chosen=self.select(self.save())
        snapshot=self.records.snapshot('one');spans=source_spans(snapshot)
        plan_key=next(key for key,value in spans.items() if value['document_id']==chosen['document_id'])
        quote=spans[plan_key]
        self.assertEqual(quote['version_id'],chosen['selection']['options_version_id'])
        self.assertIn(quote['quote'],self.options[0]['content'])
        response=self.report();response['entries'][0]['evidence']=[{'span_id':plan_key}]
        resolved=resolve_evidence(snapshot,response)
        self.assertEqual(resolved['entries'][0]['evidence'],[quote])
        validate_report(snapshot,resolved,category='Worldview')
        for change in ({'quote':self.options[1]['content']},{'version_id':chosen['version_id']}):
            invalid=copy.deepcopy(resolved);invalid['entries'][0]['evidence'][0].update(change)
            with self.subTest(change=change),self.assertRaises(ProjectStoreError):validate_report(snapshot,invalid,category='Worldview')
        invalid=copy.deepcopy(resolved)
        invalid['findings']=[{'kind':'canon_conflict','explanation':'A plan alone cannot contradict an established fact.',
            'evidence':[quote],'fact_id':self.fact.id,'fact_quote':self.fact.value}]
        with self.assertRaisesRegex(ProjectStoreError,'plan|prose|manuscript'):
            validate_report(snapshot,invalid,category='Worldview')
        invalid['findings'][0]['evidence'].append({'document_id':self.chapter.id,
            'version_id':self.chapter.active_version_id,'quote':'opens the gate'})
        validate_report(snapshot,invalid,category='Worldview')
        self.assertTrue(all(self.options[1]['content'] not in value['quote'] for value in spans.values()))

    def test_volume_collects_only_related_selected_directions_and_deduplicates_work_choice(self):
        work=self.select(self.save())
        part=self.select(self.save(slug='part-direction',scope='part',document=self.part.id,
            sources=[{'document_id':self.part.id,'version_id':self.part.active_version_id}]))
        chapter=self.select(self.save(slug='chapter-direction',scope='chapter',document=self.chapter.id,
            sources=[{'document_id':self.chapter.id,'version_id':self.chapter.active_version_id}]))
        other=self.select(self.save(slug='other-direction',scope='part',document=self.other_part.id,
            sources=[{'document_id':self.other_part.id,'version_id':self.other_part.active_version_id}]))
        limited=self.records.volume_snapshot('part')['creative_decisions']
        self.assertEqual({item['document_id'] for item in limited},{work['document_id'],part['document_id'],chapter['document_id']})
        all_choices=self.records.volume_snapshot()['creative_decisions']
        self.assertEqual(len(all_choices),4)
        self.assertIn(other['document_id'],{item['document_id'] for item in all_choices})

    def test_analysis_and_expert_inputs_separate_selected_plan_intent_from_story_facts(self):
        chosen=self.select(self.save())
        candidate=self.store.create_version('one',content='A candidate under review.',expected_version_id=self.chapter.active_version_id)
        scope=bind_task(self.root,{'kind':'review','document':'one','sourceVersion':self.chapter.active_version_id,
            'versionNumber':candidate.version_number})
        policy=LiteraryReviewPolicy(self.root)
        prepared=policy.prepare(scope,{'experts':['critic']},context_limit=200000)
        payload=json.loads(prepared.experts[0].prompt)
        self.assertEqual(payload['snapshot']['creative_decisions'][0]['selection']['option_id'],'A')
        self.assertIn('plan',prepared.experts[0].instructions.lower());self.assertIn('not',prepared.experts[0].instructions.lower())
        record={'taskId':prepared.task_id,'taskFingerprint':prepared.task_fingerprint,'sourceFingerprint':prepared.source_fingerprint}
        self.assertTrue(policy.unchanged(scope,record))
        calls=[]
        class Adapter:
            def run(self,**kwargs):
                calls.append(kwargs)
                return CoreExecutionResult(True,False,json.dumps({'summary':'Checked plan and prose separately.','entries':[],'findings':[]}),
                    None,'synthetic',[],0,'','',.01)
        config=replace(make_config(self.root),enable_reference=False,max_stage_context_chars=200000)
        ChapterAnalysisService(config,adapter=Adapter()).analyze('one',categories=['Worldview'],candidate=candidate)
        self.assertIn('creative_decisions',calls[0]['prompt'])
        self.assertIn('plan',calls[0]['prompt'].lower())
        self.assertNotIn(self.options[1]['content'],calls[0]['prompt'])
        self.select(chosen,'B')
        self.assertFalse(policy.unchanged(scope,record))
