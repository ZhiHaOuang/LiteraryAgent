"""Direct source associations remain bounded, read-only and explicitly incomplete."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lg_cli import source_impact, source_review
from lg_cli.generation_sources import bind_generation_sources, make_observation, make_receipt
from lg_cli.project_store import ProjectStore, ProjectStoreError


class SourceImpactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = ProjectStore(Path(temporary.name))
        self.store.initialize(name='Known direct source associations')
        self.source = self.store.create_document(kind='chapter', slug='source', title='Source',
            state='accepted', content='Original source.')
        self.target = self.store.create_document(kind='chapter', slug='target', title='Target',
            state='accepted', content='Author target.')

    def observed(self, *, version=None, exposure='content', tool='read_document'):
        identity = version.id if version else self.source.active_version_id
        if exposure == 'index':
            return make_observation('project_memory', {'documents': [{'id': self.source.id,
                                    'active_version_id': identity}]})
        return {'tool': tool, 'payload_sha256': 'a' * 64, 'coverage': 'partial',
            'refs': [{'kind': 'document', 'document_id': self.source.id,
                'version_id': identity, 'exposure': exposure}]}

    def captured(self, observations=None, *, target='target', **kwargs):
        receipt = make_receipt(mode='live', run_id='captured-run',
            observations=observations if observations is not None else [self.observed()])
        with bind_generation_sources(receipt):
            return self.store.create_version(target, content='Candidate for review.', **kwargs)

    def dependents(self, reference='source', **kwargs):
        return self.store.source_impact(reference, **kwargs)['direct_dependents']

    def decision(self, *, sources=None, selected=True):
        value = self.store.save_decision_options(slug='direction', title='Direction',
            topic={'scope': 'work', 'document_id': None, 'key': 'ending'},
            options=[{'id': key, 'title': key, 'content': 'Direction ' + key} for key in ('A', 'B', 'C')],
            sources=sources if sources is not None else [{'document_id': self.source.id,
                                                       'version_id': self.source.active_version_id}],
            request='Compare alternatives.', expected_version_id=0)
        if selected:
            value = self.store.select_decision_option('direction', options_version_number=value['options_version_number'],
                                                      option_id='A', expected_version_id=value['version_id'])
        return value

    def rewrite_metadata(self, version, change):
        metadata = dict(version.metadata)
        change(metadata)
        with self.store._connection() as connection:
            connection.execute('UPDATE document_versions SET metadata_json=? WHERE id=?',
                               (json.dumps(metadata), version.id))

    def test_declared_facts_proposals_and_selected_plans_keep_independent_states(self):
        fact = self.store.set_fact(category='world', key='gate', value='The gate closes.',
            source_document='source', source_version=1)
        proposal = self.store.propose_fact(category='world', key='door', value='The door closes.',
            source_document='source', source_version=1)
        chosen = self.decision()
        self.store.edit_active_document('source', content='Author changed the source.',
            expected_version_id=self.source.active_version_id, reason='Explicit author edit')
        result = self.store.source_impact('source', version_number=1)
        items = result['direct_dependents']['items']
        self.assertEqual({item['kind'] for item in items if item['relation'] == 'declared_source'},
                         {'fact', 'proposal', 'creative_decision'})
        self.assertEqual(next(item for item in items if item['kind'] == 'fact')['id'], fact.id)
        self.assertEqual(next(item for item in items if item['kind'] == 'proposal')['id'], proposal.id)
        plan = next(item for item in items if item['kind'] == 'creative_decision')
        self.assertEqual(plan['document_id'], chosen['document_id'])
        self.assertTrue(plan['is_active'])
        self.assertTrue(all(item['source_status'] == 'stale' for item in items if item['relation'] == 'declared_source'))
        self.assertEqual(self.store.get_fact(fact.id).state, 'canonical')
        self.assertEqual(self.store.get_proposal(proposal.id).status, 'proposed')
        self.assertEqual(self.store.decision_snapshot('direction')['selection']['option_id'], 'A')
        self.assertFalse(result['semantic_impact_proven'])
        self.assertEqual(result['purpose'], 'author_global')

    def test_index_only_is_excluded_and_excerpt_is_a_known_partial_input(self):
        indexed = self.captured([self.observed(exposure='index')])
        excerpt = self.captured([self.observed(exposure='excerpt', tool='render_story_context')])
        values = self.dependents()
        edges = [item for item in values['items'] if item['relation'] == 'captured_input']
        self.assertEqual([item['version_id'] for item in edges], [excerpt.id])
        self.assertNotIn(indexed.id, [item['version_id'] for item in edges])
        self.assertEqual(edges[0]['refs'][0]['exposure'], 'excerpt')
        self.assertEqual(values['scan']['index_only_refs'], 1)

    def test_exact_version_filter_differs_from_all_versions_and_checks_owned_ids(self):
        later = self.store.create_version('source', content='An intentionally selected older candidate.')
        first = self.captured()
        second = self.captured([self.observed(version=later, tool='story_source_document')])
        values = self.dependents()
        self.assertEqual({item['version_id'] for item in values['items']}, {first.id, second.id})
        exact = self.store.source_impact('source', version_number=later.version_number)
        self.assertEqual(exact['subject']['version_id'], later.id)
        self.assertEqual(exact['generationSourceReview']['version_id'], later.id)
        self.assertEqual([item['version_id'] for item in exact['direct_dependents']['items']], [second.id])
        self.assertEqual(exact['direct_dependents']['items'][0]['refs'][0]['source_status'], 'historical')
        bad = self.observed()
        bad['refs'][0]['version_id'] = self.target.active_version_id
        forged = self.captured([bad])
        self.assertNotIn(forged.id, [item['version_id'] for item in self.dependents()['items']])

    def test_all_version_states_are_reported_separately_from_active_author_version(self):
        accepted = self.captured()
        self.store.accept_version('target', accepted.version_number)
        authored = self.store.edit_active_document('target', content='Explicit author revision.',
            expected_version_id=accepted.id, reason='Author edit')
        rejected = self.captured()
        self.store.reject_version('target', rejected.version_number)
        items = self.dependents()['items']
        accepted_item = next(item for item in items if item['version_id'] == accepted.id)
        rejected_item = next(item for item in items if item['version_id'] == rejected.id)
        self.assertEqual(accepted_item['state'], 'accepted')
        self.assertFalse(accepted_item['is_active'])
        self.assertEqual(rejected_item['state'], 'rejected')
        self.assertNotIn(authored.id, [item['version_id'] for item in items])
        self.assertNotIn('generation_sources', authored.metadata)
        self.assertGreater(self.dependents()['scan']['unknown_versions'], 0)

    def test_edit_basis_is_not_captured_content_and_author_predecessor_is_visible(self):
        candidate = self.store.create_version('source', content='Candidate based on author head.',
            expected_version_id=self.source.active_version_id)
        authored = self.store.edit_active_document('source', content='New author head.',
            expected_version_id=self.source.active_version_id, reason='Author edit')
        edges = self.dependents(version_number=1)['items']
        self.assertEqual({item['version_id'] for item in edges}, {candidate.id, authored.id})
        self.assertTrue(all(item['relation'] == 'edit_basis' for item in edges))
        self.assertTrue(next(item for item in edges if item['version_id'] == authored.id)['is_active'])
        self.assertEqual({ref['field'] for item in edges for ref in item['refs']},
                         {'base_version_id', 'previous_version_id'})

    def test_same_version_can_have_two_explicit_relation_items(self):
        candidate = self.captured(target='source', expected_version_id=self.source.active_version_id)
        items = self.dependents(version_number=1)['items']
        self.assertEqual({item['relation'] for item in items if item['version_id'] == candidate.id},
                         {'captured_input', 'edit_basis'})
        self.assertEqual(self.dependents()['discovered'], 2)

    def test_regeneration_and_withdrawal_do_not_invent_selected_plan_sources(self):
        initial = self.decision(selected=False)
        self.assertFalse(any(item['kind'] == 'creative_decision' for item in self.dependents()['items']))
        chosen = self.store.select_decision_option('direction', options_version_number=initial['options_version_number'],
            option_id='A', expected_version_id=initial['version_id'])
        regenerated = self.store.save_decision_options(slug='direction', title='Direction', topic=chosen['topic'],
            options=[{**item, 'content': item['content'] + ' new'} for item in chosen['options']],
            sources=[], request='Consider a new direction.', expected_version_id=chosen['version_id'])
        self.assertEqual(regenerated['selection'], chosen['selection'])
        self.assertEqual(len([item for item in self.dependents()['items'] if item['kind'] == 'creative_decision']), 1)
        self.store.withdraw_decision('direction', expected_version_id=regenerated['version_id'])
        self.assertFalse(any(item['kind'] == 'creative_decision' for item in self.dependents()['items']))

    def test_selected_and_unselected_decision_exposures_use_option_sets_not_heads(self):
        initial = self.decision(selected=False)
        unselected = make_observation('project_memory', {'creative_decisions': self.store.decision_context()['decisions']})
        no_choice = self.captured([unselected])
        chosen = self.store.select_decision_option('direction', options_version_number=initial['options_version_number'],
            option_id='A', expected_version_id=initial['version_id'])
        selected = make_observation('studio_decision', self.store.decision_snapshot('direction'))
        choice = self.captured([selected])
        items = [item for item in self.dependents('direction')['items'] if item['relation'] == 'captured_input']
        self.assertEqual({item['version_id'] for item in items}, {no_choice.id, choice.id})
        no_choice_item = next(item for item in items if item['version_id'] == no_choice.id)
        self.assertEqual(no_choice_item['refs'][0]['exposure'], 'unselected')
        self.assertEqual(no_choice_item['refs'][0]['source_status'], 'changed')
        exact = self.dependents('direction', version_number=initial['options_version_number'])
        self.assertEqual([item['version_id'] for item in exact['items'] if item['relation'] == 'captured_input'], [choice.id])
        self.assertEqual(exact['scan']['unresolved_version_refs'], 1)
        self.assertEqual(self.dependents('direction', version_number=chosen['version_number'])['items'], [])

    def test_fact_and_scene_inputs_are_not_transitively_promoted_to_prose_dependencies(self):
        fact = self.store.set_fact(category='world', key='gate', value='The gate closes.', source_document='source')
        observation = {'tool': 'project_memory', 'payload_sha256': 'a' * 64, 'coverage': 'partial',
            'refs': [{'kind': 'fact', 'fact_id': fact.id, 'payload_sha256': 'a' * 64, 'exposure': 'facts'},
                     {'kind': 'scene_card', 'document_id': self.source.id, 'exposure': 'content'}]}
        candidate = self.captured([observation])
        self.assertNotIn(candidate.id, [item.get('version_id') for item in self.dependents()['items']])

    def test_scene_card_is_an_identified_input_without_an_invented_prose_version(self):
        from lg_cli.generation_sources import text_sha
        from lg_cli.story_context import render_scene_card
        scene = self.store.create_scene(slug='gate', title='Gate', location='Author location')
        observation = {'tool': 'render_scene_card', 'payload_sha256': text_sha(render_scene_card(scene)),
            'coverage': 'partial', 'refs': [{'kind': 'scene_card', 'document_id': scene.document.id,
                                           'exposure': 'content'}]}
        candidate = self.captured([observation])
        self.store.update_scene_card('gate', location='New author location')
        items = self.dependents('gate')['items']
        item = next(value for value in items if value['version_id'] == candidate.id)
        self.assertEqual(item['refs'][0]['kind'], 'scene_card')
        self.assertEqual(item['refs'][0]['source_status'], 'changed')
        self.assertNotIn('version_id', item['refs'][0])
        exact = self.dependents('gate', version_number=scene.document.active_version_number)
        self.assertEqual(exact['items'], [])
        self.assertEqual(exact['scan']['unresolved_version_refs'], 1)

    def test_more_than_one_thousand_versions_do_not_hide_an_early_receipt(self):
        early = self.captured()
        for index in range(1001):
            self.store.create_version('target', content=f'Later version {index}.')
        self.assertNotIn(early.id, [version.id for version in self.store.list_versions('target', limit=100000)])
        result = self.dependents()
        self.assertIn(early.id, [item['version_id'] for item in result['items']])
        table = result['scan']['tables']['document_versions']
        self.assertEqual(table['scanned'], table['total'])
        self.assertFalse(table['truncated'])

    def test_output_limit_and_table_scan_limit_are_independently_explicit(self):
        for index in range(3):
            self.store.set_fact(category='world', key=str(index), value='Declared source.', source_document='source')
        result = self.dependents(limit=1)
        self.assertEqual(result['discovered'], 3)
        self.assertEqual(len(result['items']), 1)
        self.assertTrue(result['truncated'])
        self.assertFalse(result['scan']['tables']['story_facts']['truncated'])
        with patch.object(source_impact, 'MAX_SCAN', 2):
            limited = self.store.source_impact('source')
        table = limited['direct_dependents']['scan']['tables']['story_facts']
        self.assertEqual((table['total'], table['scanned'], table['truncated']), (3, 2, True))
        self.assertTrue(limited['direct_dependents']['truncated'])
        self.assertTrue(any('lower bound' in reason for reason in limited['unknown_reasons']))

    def test_output_binding_corruption_and_oversized_metadata_remain_unknown(self):
        candidate = self.captured()
        with self.store._connection() as connection:
            connection.execute('UPDATE document_versions SET content=? WHERE id=?', ('PRIVATE_CHANGED_OUTPUT', candidate.id))
        result = self.store.source_impact('source')
        self.assertEqual(result['direct_dependents']['items'], [])
        self.assertEqual(result['direct_dependents']['scan']['invalid_receipts'], 1)
        self.assertNotIn('PRIVATE_CHANGED_OUTPUT', json.dumps(result))
        with patch.object(source_impact, 'MAX_METADATA_BYTES', 10):
            bounded = self.store.source_impact('source')
        self.assertEqual(bounded['direct_dependents']['items'], [])
        self.assertEqual(bounded['direct_dependents']['scan']['invalid_receipts'], 1)

    def test_unknown_projection_is_not_promoted_to_captured_input(self):
        candidate = self.captured([self.observed(tool='unrecognized_reader')])
        result = self.store.source_impact('source')
        self.assertNotIn(candidate.id, [item.get('version_id') for item in result['direct_dependents']['items']])
        self.assertTrue(any('could not be reconstructed' in reason for reason in result['unknown_reasons']))

    def test_reference_detail_budget_and_duplicate_reads_do_not_inflate_relations(self):
        later = self.store.create_version('source', content='Another explicitly selected version.')
        first = self.observed(tool='story_source_document')
        second = self.observed(version=later, tool='story_source_document')
        candidate = self.captured([first, first, second])
        full = self.dependents()
        item = next(item for item in full['items'] if item['version_id'] == candidate.id)
        self.assertEqual(len(item['refs']), 2)
        self.assertEqual(full['discovered'], 1)
        with patch.object(source_impact, 'MAX_REFS_PER_ITEM', 1):
            limited = self.dependents()
        item = next(item for item in limited['items'] if item['version_id'] == candidate.id)
        self.assertEqual(len(item['refs']), 1)
        self.assertTrue(item['refs_truncated'])
        self.assertTrue(limited['truncated'])
        self.assertEqual(limited['discovered'], 1)

    def test_diagnostic_uses_one_read_snapshot_even_during_an_author_source_edit(self):
        self.captured()
        original = source_review._check_reference
        writer = ProjectStore(self.store.workspace)
        updated = []

        def check(*args):
            if not updated:
                updated.append(writer.edit_active_document('source', content='Concurrent author revision.',
                    expected_version_id=self.source.active_version_id, reason='Author edit'))
            return original(*args)

        with patch.object(self.store, '_connection', wraps=self.store._connection) as opened, \
                patch.object(source_review, '_check_reference', side_effect=check), \
                patch.object(self.store, 'fact_sources', side_effect=AssertionError('Separate snapshot')), \
                patch.object(self.store, 'list_versions', side_effect=AssertionError('Capped public scan')):
            result = self.store.source_impact('source')
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(result['subject']['active_version_id'], self.source.active_version_id)
        self.assertEqual(result['direct_dependents']['items'][0]['refs'][0]['source_status'], 'current')
        self.assertEqual(self.store.get_document('source').active_version_id, updated[0].id)

    def test_invalid_selectors_are_rejected_and_read_does_not_mutate_story(self):
        self.captured()
        with self.store._connection() as connection:
            before = list(connection.iterdump())
        for kwargs in ({'limit': 0}, {'limit': True}, {'limit': 1001}, {'version_number': True},
                       {'version_number': 0}, {'version_number': '1'}, {'version_number': 999}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ProjectStoreError):
                self.store.source_impact('source', **kwargs)
        result = self.store.source_impact('source')
        self.assertEqual(result['coverage'], 'partial')
        with self.store._connection() as connection:
            self.assertEqual(list(connection.iterdump()), before)


if __name__ == '__main__':
    unittest.main()
