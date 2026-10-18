import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from lg_cli.generation_sources import bind_generation_sources, make_observation, make_receipt, text_sha
from lg_cli.project_store import ProjectStore, ProjectStoreError
from lg_cli.story_context import render_story_context_with_sources, render_scene_card
from lg_cli.studio_operations import invoke
from lg_cli.writing_mcp import create_server
from tests.helpers import make_config


class SourceReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = ProjectStore(self.root)
        self.store.initialize(name='Identified source review')
        self.document = self.store.create_document(kind='chapter', slug='source', title='Source',
            content='Accepted source body.', state='accepted')
        self.target = self.store.create_document(kind='chapter', slug='target', title='Target',
            content='Target prose.', state='accepted')
        self.config = make_config(self.root)
        self.fact = self.store.set_fact(category='world', key='gate', value='The gate closes at dusk.')
        value = self.store.save_decision_options(slug='ending', title='Ending',
            topic={'scope': 'work', 'document_id': None, 'key': 'ending'},
            options=[{'id': str(i), 'title': f'Option {i}', 'content': f'PRIVATE_DIRECTION_{i}'} for i in range(3)],
            sources=[], request='Compare three directions.', expected_version_id=0)
        self.choice = self.store.select_decision_option('ending', options_version_number=value['options_version_number'],
            option_id='0', expected_version_id=value['version_id'])

    def tool(self, name, **arguments):
        return asyncio.run(invoke(create_server(self.config), name, arguments))

    def candidate(self, observations, *, receipt=None):
        receipt = receipt or make_receipt(mode='live', run_id='captured-run', observations=observations,
                                         unknown_reasons=['prior_history_unobserved'])
        with bind_generation_sources(receipt):
            return self.store.create_version('target', content='Candidate prose.')

    def review(self, candidate, **kwargs):
        return self.store.generation_source_review('target', candidate.version_number, **kwargs)

    def observations(self):
        memory = make_observation('project_memory', self.tool('project_memory'))
        full = make_observation('studio_decision', self.tool('studio_decision', slug='ending'))
        _, story = render_story_context_with_sources(self.store.context_snapshot())
        return [memory, full, story]

    def test_same_sources_reconstruct_each_producer_without_cross_hash_false_positives(self):
        observations = self.observations()
        hashes = [next(ref for ref in item['refs'] if ref['kind'] == 'creative_decision')['selection_sha256']
                  for item in observations]
        self.assertEqual(len(set(hashes)), 3)
        candidate = self.candidate(observations)
        before = self.store.database_path.read_bytes()
        review = self.review(candidate)
        self.assertEqual(review['status'], 'no_detected_change')
        self.assertEqual(review['summary']['changed'], 0)
        self.assertEqual(review['summary']['unknown'], 0)
        self.assertEqual(review['receipt_status'], 'recorded')
        self.assertEqual(review['coverage'], 'partial')
        self.assertNotIn('PRIVATE_DIRECTION', json.dumps(review))
        self.assertEqual(self.store.database_path.read_bytes(), before)
        self.assertEqual(self.store.get_version('target', candidate.version_number), candidate)

    def test_formal_selection_change_is_reviewable_without_rewriting_candidate(self):
        candidate = self.candidate(self.observations())
        self.store.select_decision_option('ending', options_version_number=self.choice['options_version_number'],
            option_id='1', expected_version_id=self.choice['version_id'])
        review = self.review(candidate)
        self.assertEqual(review['status'], 'needs_review')
        decisions = [item for item in review['items'] if item['ref']['kind'] == 'creative_decision']
        self.assertEqual(len(decisions), 3)
        self.assertTrue(all(item['status'] == 'changed' for item in decisions))
        self.assertTrue(all(item['current']['option_id'] == '1' for item in decisions))
        self.assertEqual(self.store.get_version('target', candidate.version_number), candidate)

    def test_regenerating_unselected_options_does_not_change_identified_choice(self):
        candidate = self.candidate(self.observations())
        value = self.store.decision_snapshot('ending')
        self.store.save_decision_options(slug='ending', title=value['title'], topic=value['topic'],
            options=[dict(option, content=option['content'] + ' new alternative') for option in value['options']],
            sources=[], request='New alternatives only.', expected_version_id=value['version_id'])
        self.assertEqual(self.review(candidate)['status'], 'no_detected_change')

    def test_withdraw_and_reselect_same_original_choice_compare_current_projection(self):
        candidate = self.candidate(self.observations())
        value = self.store.withdraw_decision('ending', expected_version_id=self.choice['version_id'])
        self.assertEqual(self.review(candidate)['status'], 'needs_review')
        self.store.select_decision_option('ending', options_version_number=self.choice['options_version_number'],
            option_id='0', expected_version_id=value['version_id'])
        self.assertEqual(self.review(candidate)['status'], 'no_detected_change')

    def test_fact_same_id_changed_value_is_detected_for_both_producers(self):
        candidate = self.candidate(self.observations())
        changed = self.store.set_fact(category='world', key='gate', value='The gate closes at noon.')
        self.assertEqual(changed.id, self.fact.id)
        facts = [item for item in self.review(candidate)['items'] if item['ref']['kind'] == 'fact']
        self.assertEqual(len(facts), 2)
        self.assertTrue(all(item['status'] == 'changed' for item in facts))
        self.assertEqual(self.store.list_facts(state='canonical')[0].state, 'canonical')

    def test_fact_rationale_only_changes_live_projection_not_rendered_story(self):
        candidate = self.candidate(self.observations())
        self.store.set_fact(category='world', key='gate', value=self.fact.value, rationale='An author explanation.')
        facts = {item['tool']: item['status'] for item in self.review(candidate)['items'] if item['ref']['kind'] == 'fact'}
        self.assertEqual(facts, {'project_memory': 'changed', 'render_story_context': 'current'})

    def test_active_document_change_is_detected_but_index_is_not_content(self):
        memory = make_observation('project_memory', self.tool('project_memory'))
        read = make_observation('read_document', self.tool('read_document', slug='source'))
        indexed = self.candidate([memory])
        content = self.candidate([read])
        self.store.edit_active_document('source', content='New author prose.',
            expected_version_id=self.document.active_version_id, reason='Author revised source')
        index_review = self.review(indexed)
        self.assertEqual(index_review['summary']['changed'], 0)
        self.assertTrue(any(item['status'] == 'index_only' for item in index_review['items']))
        self.assertEqual(self.review(content)['status'], 'needs_review')

    def test_explicit_historical_candidate_is_not_mistaken_for_replaced_active_input(self):
        historical = self.store.create_version('source', content='Chosen older candidate.')
        observation = {'tool': 'story_source_document', 'payload_sha256': text_sha(historical.content),
            'refs': [{'kind': 'document', 'document_id': self.document.id, 'version_id': historical.id,
                      'exposure': 'content', 'excerpt_sha256': text_sha(historical.content)}], 'coverage': 'partial'}
        candidate = self.candidate([observation])
        review = self.review(candidate)
        self.assertEqual(review['status'], 'no_detected_change')
        self.assertEqual(review['items'][0]['status'], 'historical')
        self.store.reject_version('source', historical.version_number)
        self.assertEqual(self.review(candidate)['items'][0]['status'], 'unavailable')

    def test_same_source_version_final_state_is_not_content_change(self):
        candidate = self.candidate([make_observation('read_document', self.tool('read_document', slug='source'))])
        self.store.accept_version('source', 1, final=True)
        review = self.review(candidate)
        self.assertEqual(review['items'][0]['status'], 'current')
        self.assertEqual(review['items'][0]['current']['version_state'], 'final')

    def test_unknown_projections_legacy_and_no_reads_never_claim_no_detected_change(self):
        legacy = self.store.create_version('target', content='Legacy candidate')
        self.assertEqual(self.review(legacy)['receipt_status'], 'unrecorded')
        for observations in ([], [make_observation('book_entry', 'Opaque text')],
                [{'tool': 'read_document', 'payload_sha256': 'invalid', 'coverage': 'partial',
                  'refs': [{'kind': 'document', 'document_id': self.document.id,
                            'version_id': self.document.active_version_id, 'exposure': 'content'}]}],
                [{'tool': 'future_tool', 'payload_sha256': text_sha(''), 'coverage': 'partial',
                  'refs': [{'kind': 'fact', 'fact_id': self.fact.id, 'payload_sha256': text_sha('future')}]}]):
            with self.subTest(observations=observations):
                self.assertEqual(self.review(self.candidate(observations))['status'], 'unknown')

    def test_wrong_owned_version_is_unavailable_without_reading_foreign_content(self):
        observation = {'tool': 'read_document', 'payload_sha256': text_sha('Wrong'), 'coverage': 'partial',
            'refs': [{'kind': 'document', 'document_id': self.document.id,
                      'version_id': self.target.active_version_id, 'exposure': 'content'}]}
        item = self.review(self.candidate([observation]))['items'][0]
        self.assertEqual(item['status'], 'unavailable')

    def test_scene_card_is_checked_separately_from_unchanged_prose_version(self):
        scene = self.store.create_scene(slug='scene', title='Scene', location='Gate')
        observation = {'tool': 'render_scene_card', 'payload_sha256': text_sha(render_scene_card(scene)),
            'refs': [{'kind': 'scene_card', 'document_id': scene.document.id, 'exposure': 'content'}], 'coverage': 'partial'}
        candidate = self.candidate([observation])
        self.assertEqual(self.review(candidate)['status'], 'no_detected_change')
        self.store.update_scene_card('scene', location='River')
        self.assertEqual(self.store.get_document('scene').active_version_id, scene.document.active_version_id)
        self.assertEqual(self.review(candidate)['items'][0]['status'], 'changed')

    def test_invalid_receipt_and_output_mismatch_are_fixed_unknown_diagnostics(self):
        invalid = make_receipt(mode='live', run_id='bad', observations=[])
        invalid['observations'] = 'PRIVATE_BAD_VALUE'
        candidate = self.candidate([], receipt=invalid)
        review = self.review(candidate)
        self.assertEqual(review['receipt_status'], 'invalid')
        self.assertEqual(review['status'], 'unknown')
        self.assertNotIn('PRIVATE_BAD_VALUE', json.dumps(review))
        valid = self.candidate(self.observations())
        with self.store._connection() as connection:
            connection.execute('UPDATE document_versions SET content=? WHERE id=?', ('Synthetic corruption', valid.id))
        self.assertEqual(self.review(valid)['receipt_status'], 'invalid')

    def test_display_limit_does_not_hide_detected_change_from_summary(self):
        candidate = self.candidate(self.observations())
        self.store.withdraw_decision('ending', expected_version_id=self.choice['version_id'])
        review = self.review(candidate, limit=1)
        self.assertEqual(len(review['items']), 1)
        self.assertTrue(review['truncated'])
        self.assertEqual(review['status'], 'needs_review')
        self.assertEqual(review['summary']['changed'], 3)

    def test_check_budget_exhaustion_is_explicit_unknown(self):
        from unittest.mock import patch
        candidate = self.candidate(self.observations())
        with patch('lg_cli.source_review.MAX_REFERENCES', 1):
            review = self.review(candidate)
        self.assertTrue(review['truncated'])
        self.assertEqual(review['status'], 'unknown')
        self.assertIn('source_check_budget_exhausted', review['unknown_reasons'])

    def test_exact_version_and_limits_are_validated(self):
        for number in (0, True, None):
            with self.subTest(number=number), self.assertRaises(ProjectStoreError):
                self.store.generation_source_review('target', number)
        for limit in (0, True, 1001):
            with self.subTest(limit=limit), self.assertRaises(ProjectStoreError):
                self.store.generation_source_review('target', 1, limit=limit)
