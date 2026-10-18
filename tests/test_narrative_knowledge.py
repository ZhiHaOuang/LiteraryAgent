from __future__ import annotations

import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from lg_cli import narrative_knowledge as knowledge
from lg_cli.project_store import ProjectStore, ProjectStoreError


class NarrativeKnowledgeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = ProjectStore(Path(directory.name))
        self.store.initialize(name='Explicit narrative cognition')
        self.source = self.store.create_document(kind='chapter', slug='chapter', title='Source',
            content='The captain is dead.', state='accepted', sequence=10)
        self.first = self.store.create_document(kind='character', slug='first', title='First investigator',
            content='An author-global character profile.', state='accepted')
        self.second = self.store.create_document(kind='character', slug='second', title='Second investigator',
            content='Another author-global profile.', state='accepted')
        self.fact = self.store.set_fact(category='world', key='captain', value='The captain is dead.',
            source_document=self.source.id, source_version=self.source.active_version_number)
        self.topic = {'audience': 'character', 'holder_document_id': self.first.id,
                      'key': 'captain-status', 'from_position': 0}
        self.statement = {'content': 'The captain is alive.', 'stance': 'believes', 'until_position': 10,
            'sources': [{'document_id': self.source.id, 'version_id': self.source.active_version_id}]}

    def propose(self, **changes):
        arguments = {'slug': 'belief', 'title': 'Investigator belief', 'topic': copy.deepcopy(self.topic),
            'statement': copy.deepcopy(self.statement), 'rationale': 'Declared narrative context, not verified author speech.',
            'expected_version_id': 0}
        arguments.update(changes)
        return self.store.propose_knowledge(**arguments)

    def confirm(self, record, **changes):
        arguments = {'proposal_version_number': record['proposal_version_number'],
                     'expected_version_id': record['version_id']}
        arguments.update(changes)
        return self.store.confirm_knowledge(record['slug'], **arguments)

    def family(self, slug, start, until, *, key='captain-status', audience='character', holder=None):
        if holder is None and audience == 'character':
            holder = self.first.id
        return self.propose(slug=slug, title=slug,
            topic={'audience': audience, 'holder_document_id': holder, 'key': key, 'from_position': start},
            statement={**self.statement, 'until_position': until})

    def assert_world_unchanged(self):
        self.assertEqual(self.store.get_fact(self.fact.id), self.fact)
        self.assertEqual(self.store.get_document('chapter'), self.source)
        self.assertEqual(self.store.get_document('first'), self.first)
        self.assertEqual(self.store.get_document('second'), self.second)
        self.assertEqual(self.store.list_proposals(), [])

    def test_new_proposal_is_unconfirmed_draft_with_immutable_search_projection(self):
        result = self.propose()
        self.assertEqual((result['status'], result['confirmed']), ('unconfirmed', None))
        self.assertEqual(result['proposal']['content'], self.statement['content'])
        self.assertEqual(result['source_status'], 'current')
        document = self.store.get_document('belief')
        self.assertEqual((document.kind, document.state, document.parent_id, document.sequence),
                         (knowledge.KIND, 'draft', self.first.id, 0))
        self.assertEqual(result['proposal_version_id'], document.active_version_id)
        self.assertEqual(json.loads(document.content)['schema'], knowledge.SCHEMA)
        self.assert_world_unchanged()

    def test_conflicting_holders_and_wrong_reader_belief_never_promote_world_truth(self):
        first = self.confirm(self.propose())
        second = self.confirm(self.propose(slug='second-belief', title='Second belief',
            topic={**self.topic, 'holder_document_id': self.second.id},
            statement={**self.statement, 'content': 'The captain is dead.', 'stance': 'knows'}))
        reader = self.confirm(self.propose(slug='reader', title='Reader belief',
            topic={**self.topic, 'audience': 'reader', 'holder_document_id': None},
            statement={**self.statement, 'content': 'The captain is alive.', 'stance': 'knows'}))
        self.assertEqual(first['confirmed']['content'], reader['confirmed']['content'])
        self.assertNotEqual(first['confirmed']['content'], second['confirmed']['content'])
        self.assertEqual({item['status'] for item in self.store.list_knowledge_snapshots()}, {'confirmed'})
        self.assert_world_unchanged()

    def test_exact_historical_proposal_can_be_confirmed_without_replacing_latest_proposal(self):
        original = self.propose()
        newer = self.propose(statement={**self.statement, 'content': 'A newer unconfirmed possibility.'},
            expected_version_id=original['version_id'])
        preview = self.store.knowledge_confirmation_preview('belief', proposal_version_number=1,
            expected_version_id=newer['version_id'])
        self.assertEqual(preview['proposed_confirmation']['proposal_version_id'], original['version_id'])
        self.assertEqual(len(self.store.list_versions('belief')), 2)
        confirmed = self.confirm(newer, proposal_version_number=1)
        self.assertEqual(confirmed['confirmed']['content'], self.statement['content'])
        self.assertEqual(confirmed['confirmed']['proposal_version_id'], original['version_id'])
        self.assertEqual(confirmed['proposal']['content'], 'A newer unconfirmed possibility.')
        self.assertEqual(confirmed['proposal_version_id'], newer['version_id'])
        for number in (True, 0, 999, confirmed['version_number']):
            with self.subTest(number=number), self.assertRaises(ProjectStoreError):
                self.confirm(confirmed, proposal_version_number=number)
        with self.assertRaisesRegex(ProjectStoreError, 'already confirmed'):
            self.confirm(confirmed, proposal_version_number=1)
        self.assertEqual(len(self.store.list_versions('belief')), 3)

    def test_reproposal_preserves_confirmation_and_has_separate_source_status(self):
        confirmed = self.confirm(self.propose())
        pending_source = self.store.create_version('chapter', content='Pending source.')
        proposed = self.propose(statement={**self.statement, 'content': 'An alternate belief.',
            'sources': [{'document_id': self.source.id, 'version_id': pending_source.id}]},
            expected_version_id=confirmed['version_id'])
        self.assertEqual(proposed['confirmed'], confirmed['confirmed'])
        self.assertEqual((proposed['status'], proposed['source_status'], proposed['proposal_source_status']),
                         ('confirmed', 'current', 'pending'))
        document = self.store.get_document('belief')
        self.assertEqual((document.parent_id, document.sequence, document.state), (self.first.id, 0, 'draft'))
        self.assert_world_unchanged()

    def test_withdrawal_has_no_fallback_after_new_proposals_or_history_reads(self):
        confirmed = self.confirm(self.propose())
        second = self.propose(statement={**self.statement, 'content': 'New proposition.'},
            expected_version_id=confirmed['version_id'])
        withdrawn = self.store.withdraw_knowledge('belief', expected_version_id=second['version_id'])
        self.assertEqual((withdrawn['status'], withdrawn['confirmed']), ('withdrawn', None))
        self.assertEqual(withdrawn['proposal_version_id'], second['version_id'])
        self.assertIsNotNone(json.loads(self.store.get_version('belief', confirmed['version_number']).content)['confirmed'])
        reproposed = self.propose(expected_version_id=withdrawn['version_id'])
        self.assertEqual((reproposed['status'], reproposed['confirmed']), ('withdrawn', None))
        with self.assertRaisesRegex(ProjectStoreError, 'no current'):
            self.store.withdraw_knowledge('belief', expected_version_id=reproposed['version_id'])
        self.assertIsNone(self.store.knowledge_snapshot('belief')['confirmed'])

    def test_half_open_intervals_allow_adjacency_and_refuse_overlap(self):
        self.confirm(self.family('first-interval', 0, 10))
        self.confirm(self.family('adjacent', 10, 20))
        overlap = self.family('overlap', 5, 10)
        with self.assertRaisesRegex(ProjectStoreError, 'overlap'):
            self.confirm(overlap)
        first = self.store.knowledge_snapshot('first-interval')
        self.store.withdraw_knowledge(first['slug'], expected_version_id=first['version_id'])
        self.assertEqual(self.confirm(overlap)['status'], 'confirmed')
        self.assert_world_unchanged()

    def test_unbounded_and_negative_intervals_are_explicit_author_coordinates(self):
        original = self.confirm(self.family('unbounded', -100, None))
        later = self.family('later', 999999999, 1000000000)
        with self.assertRaisesRegex(ProjectStoreError, 'overlap'):
            self.confirm(later)
        other = self.confirm(self.family('different-key', -100, None, key='other-topic'))
        self.assertEqual(other['topic']['from_position'], original['topic']['from_position'])
        self.assertEqual(self.store.get_document('unbounded').sequence, -100)
        self.assertEqual(self.source.sequence, 10)

    def test_stale_confirmation_keeps_interval_occupied_until_author_withdraws(self):
        original = self.confirm(self.propose())
        self.store.edit_active_document('chapter', content='Author changed the source.',
            expected_version_id=self.source.active_version_id, reason='Author edit')
        current = self.store.knowledge_snapshot('belief')
        self.assertEqual((current['status'], current['source_status']), ('confirmed', 'stale'))
        self.assertEqual(current['confirmed'], original['confirmed'])
        other = self.family('overlap', 5, 15)
        other = self.propose(slug='overlap', title='overlap', topic=other['topic'],
            statement={**self.statement, 'until_position': 15, 'sources': []}, expected_version_id=other['version_id'])
        with self.assertRaisesRegex(ProjectStoreError, 'overlap'):
            self.confirm(other)
        self.store.withdraw_knowledge('belief', expected_version_id=current['version_id'])
        self.assertEqual(self.confirm(other)['source_status'], 'unbound')

    def test_pending_sources_can_be_proposed_but_require_accepted_active_text_for_confirmation(self):
        pending = self.store.create_version('chapter', content='Pending source.')
        result = self.propose(statement={**self.statement,
            'sources': [{'document_id': self.source.id, 'version_id': pending.id}]})
        self.assertEqual(result['proposal_source_status'], 'pending')
        with self.assertRaisesRegex(ProjectStoreError, 'not current accepted'):
            self.confirm(result)
        self.store.accept_version('chapter', pending.version_number)
        self.assertEqual(self.confirm(result)['source_status'], 'current')

    def test_unavailable_source_refuses_confirmation_but_allows_withdrawal(self):
        confirmed = self.confirm(self.propose())
        self.store.create_version('chapter', content='Archived source.', state='archived', activate=True)
        result = self.store.knowledge_snapshot('belief')
        self.assertEqual(result['source_status'], 'unavailable')
        newer = self.propose(expected_version_id=confirmed['version_id'])
        with self.assertRaisesRegex(ProjectStoreError, 'not current accepted'):
            self.confirm(newer)
        self.assertEqual(self.store.withdraw_knowledge('belief', expected_version_id=newer['version_id'])['status'], 'withdrawn')

    def test_source_free_assertion_preserves_declared_rationale_without_truth_or_authorship_claim(self):
        rationale = 'An agent supplied this context; this is not verified author speech.'
        result = self.propose(statement={**self.statement, 'sources': []}, rationale=rationale)
        self.assertEqual(result['source_status'], 'unbound')
        confirmed = self.confirm(result)
        self.assertEqual(confirmed['confirmed']['rationale'], rationale)
        self.assertEqual(confirmed['source_status'], 'unbound')
        self.assertNotIn('verified_author', json.dumps(confirmed))
        self.assert_world_unchanged()

    def test_topic_title_and_identity_are_immutable_and_unique_including_start(self):
        original = self.propose()
        for changes in ({'title': 'Renamed'}, {'topic': {**self.topic, 'from_position': 1}},
                        {'topic': {**self.topic, 'holder_document_id': self.second.id}},
                        {'topic': {**self.topic, 'key': 'different'}}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ProjectStoreError, 'cannot change'):
                self.propose(expected_version_id=original['version_id'], **changes)
        with self.assertRaisesRegex(ProjectStoreError, 'already has a record'):
            self.propose(slug='duplicate', title='Duplicate')
        self.assertEqual(self.family('later', 10, 20)['topic']['from_position'], 10)
        with self.assertRaises(ProjectStoreError):
            self.propose(slug='first')
        self.assertEqual(len(self.store.list_versions('belief')), 1)

    def test_invalid_input_shapes_and_private_validation_instances_do_not_create_placeholders(self):
        invalid = [
            {'topic': {**self.topic, 'from_position': True}},
            {'topic': {**self.topic, 'from_position': '1'}},
            {'topic': {**self.topic, 'from_position': -1000000001}},
            {'topic': {**self.topic, 'from_position': 1000000001}},
            {'topic': {**self.topic, 'key': ' '}},
            {'topic': {**self.topic, 'key': 'K' * 129}},
            {'topic': {**self.topic, 'extra': 'PRIVATE_SCHEMA_INSTANCE'}},
            {'topic': {**self.topic, 'holder_document_id': True}},
            {'statement': {**self.statement, 'content': ' '}},
            {'statement': {**self.statement, 'content': 'X' * 12001}},
            {'statement': {**self.statement, 'stance': 'PRIVATE_SCHEMA_INSTANCE'}},
            {'statement': {**self.statement, 'until_position': 0}},
            {'statement': {**self.statement, 'until_position': True}},
            {'statement': {**self.statement, 'extra': 'PRIVATE_SCHEMA_INSTANCE'}},
            {'statement': {**self.statement, 'sources': self.statement['sources'] * 33}},
            {'rationale': ' '}, {'rationale': 'X' * 4001}, {'rationale': {'PRIVATE_SCHEMA_INSTANCE': True}},
            {'expected_version_id': None}, {'expected_version_id': True}, {'expected_version_id': -1},
        ]
        before = self.store.list_documents(limit=None)
        for changes in invalid:
            with self.subTest(changes=list(changes)), self.assertRaises(ProjectStoreError) as error:
                self.propose(**changes)
            self.assertNotIn('PRIVATE_SCHEMA_INSTANCE', str(error.exception))
        self.assertEqual(self.store.list_documents(limit=None), before)

    def test_holder_and_exact_source_ownership_are_enforced(self):
        retired = self.store.create_document(kind='character', slug='retired', title='Retired', state='archived')
        for topic in ({**self.topic, 'holder_document_id': None},
                      {**self.topic, 'holder_document_id': self.source.id},
                      {**self.topic, 'holder_document_id': retired.id},
                      {**self.topic, 'audience': 'reader'}):
            with self.subTest(topic=topic), self.assertRaises(ProjectStoreError):
                self.propose(topic=topic)
        for sources in ([{'document_id': self.first.id, 'version_id': self.source.active_version_id}],
                        [{'document_id': self.source.id, 'version_id': 999999}],
                        self.statement['sources'] * 2):
            with self.subTest(sources=sources), self.assertRaises(ProjectStoreError):
                self.propose(statement={**self.statement, 'sources': sources})
        self.assertEqual(self.store.list_knowledge_snapshots(), [])

    def test_retired_holder_cannot_confirm_but_author_can_withdraw_existing_confirmation(self):
        confirmed = self.confirm(self.propose())
        self.store.create_version('first', content='Retired character.', state='archived', activate=True)
        with self.assertRaisesRegex(ProjectStoreError, 'archived or rejected'):
            self.confirm(confirmed)
        self.assertEqual(self.store.withdraw_knowledge('belief', expected_version_id=confirmed['version_id'])['status'], 'withdrawn')

    def test_cas_and_duplicate_actions_preserve_exact_version_history(self):
        first = self.propose()
        newer = self.propose(expected_version_id=first['version_id'])
        for action in (lambda: self.confirm(first), lambda: self.propose(expected_version_id=first['version_id']),
                       lambda: self.store.withdraw_knowledge('belief', expected_version_id=first['version_id'])):
            with self.assertRaises(ProjectStoreError):
                action()
        confirmed = self.confirm(newer)
        with self.assertRaises(ProjectStoreError):
            self.confirm(confirmed)
        withdrawn = self.store.withdraw_knowledge('belief', expected_version_id=confirmed['version_id'])
        with self.assertRaises(ProjectStoreError):
            self.store.withdraw_knowledge('belief', expected_version_id=withdrawn['version_id'])
        self.assertEqual(len(self.store.list_versions('belief')), 4)
        self.assertEqual({version.state for version in self.store.list_versions('belief')}, {'draft'})

    def test_generic_writes_cannot_confirm_edit_rename_reject_or_restore_managed_records(self):
        record = self.propose()
        before = self.store.list_versions('belief')
        operations = (
            lambda: self.store.create_document(kind=knowledge.KIND, slug='forged', title='Forged', content='{}'),
            lambda: self.store.create_version('belief', content='{}', state='accepted', activate=True),
            lambda: self.store.edit_active_document('belief', content='{}', expected_version_id=record['version_id'], reason='bypass'),
            lambda: self.store.update_document('belief', title='Renamed', sequence=999),
            lambda: self.store.accept_version('belief', 1),
            lambda: self.store.reject_version('belief', 1),
            lambda: self.store.restore_version('belief', 1),
        )
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaises(ProjectStoreError):
                operation()
        self.assertEqual(self.store.list_versions('belief'), before)
        self.assertEqual(self.store.knowledge_snapshot('belief')['status'], 'unconfirmed')

    def test_list_and_previews_are_single_snapshot_read_operations_without_new_tables(self):
        with self.store._connection() as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        first = self.propose()
        self.family('second-position', 10, 20)
        self.assertEqual(len(self.store.list_knowledge_snapshots(limit=1)), 1)
        self.assertEqual(len(self.store.list_knowledge_snapshots(limit=None)), 2)
        with patch.object(self.store, '_connection', wraps=self.store._connection) as opened:
            preview = self.store.knowledge_confirmation_preview('belief', proposal_version_number=1,
                expected_version_id=first['version_id'])
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(preview['knowledge'], first)
        self.assertEqual(len(self.store.list_versions('belief')), 1)
        for limit in (0, True, 1001):
            with self.assertRaises(ProjectStoreError):
                self.store.list_knowledge_snapshots(limit=limit)
        with self.store._connection() as connection:
            self.assertEqual({row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}, tables)

    def test_legacy_profiles_and_canonical_values_are_not_auto_migrated_or_exposed_as_managed_json(self):
        self.store.set_fact(category='knowledge', key='legacy', value='Legacy author-global data.')
        self.assertEqual(self.store.list_knowledge_snapshots(), [])
        record = self.confirm(self.propose())
        context = self.store.context_snapshot()
        self.assertNotIn(record['document_id'], [document.id for document in context['documents']])
        self.assertFalse(context['view']['knowledge_filter_applied'])
        self.assertIn(self.first.id, [document.id for document in context['documents']])

    def test_concurrent_overlapping_confirmations_have_one_winner(self):
        first = self.family('first-window', 0, 10)
        second = self.family('second-window', 5, 15)
        barrier = Barrier(2)

        def attempt(record):
            barrier.wait(timeout=5)
            try:
                return self.confirm(record)['status']
            except ProjectStoreError:
                return 'rejected'

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, (first, second)))
        self.assertEqual(sorted(results), ['confirmed', 'rejected'])
        self.assertEqual(sum(item['confirmed'] is not None for item in self.store.list_knowledge_snapshots()), 1)

    def test_corrupt_body_or_search_projection_has_fixed_public_errors(self):
        record = self.propose()
        document = self.store.get_document('belief')
        for mutation in ('body', 'holder', 'position'):
            with self.subTest(mutation=mutation):
                with self.store._connection() as connection:
                    if mutation == 'body':
                        payload = json.loads(document.content)
                        payload['proposal']['stance'] = 'PRIVATE_CORRUPTED_VALUE'
                        connection.execute('UPDATE document_versions SET content=? WHERE id=?',
                            (json.dumps(payload), record['version_id']))
                    elif mutation == 'holder':
                        connection.execute('UPDATE documents SET parent_id=? WHERE id=?', (self.second.id, document.id))
                    else:
                        connection.execute('UPDATE documents SET sequence=999 WHERE id=?', (document.id,))
                with self.assertRaises(ProjectStoreError) as error:
                    self.store.knowledge_snapshot('belief')
                self.assertNotIn('PRIVATE_CORRUPTED_VALUE', str(error.exception))
                with self.store._connection() as connection:
                    connection.execute('UPDATE document_versions SET content=? WHERE id=?', (document.content, record['version_id']))
                    connection.execute('UPDATE documents SET parent_id=?,sequence=? WHERE id=?',
                        (self.first.id, 0, document.id))

    def test_source_impact_keeps_confirmed_declaration_when_source_becomes_stale(self):
        confirmed = self.confirm(self.propose())
        self.store.edit_active_document('chapter', content='Explicit author correction.',
            expected_version_id=self.source.active_version_id, reason='Author edit')
        impact = self.store.source_impact('chapter', version_number=1)
        row = next(item for item in impact['direct_dependents']['items'] if item['kind'] == knowledge.KIND)
        self.assertEqual(row['relation'], 'declared_source')
        self.assertEqual(row['source_status'], 'stale')
        self.assertEqual(row['version_id'], confirmed['version_id'])
        self.assertEqual(row['confirmed_proposal_version_id'], confirmed['confirmed']['proposal_version_id'])
        self.assertEqual(row['refs'], [{'kind': 'document', 'document_id': self.source.id,
                                      'version_id': self.source.active_version_id}])
        self.assertEqual(self.store.knowledge_snapshot('belief')['status'], 'confirmed')
        self.assertEqual(self.store.get_fact(self.fact.id), self.fact)

    def test_source_impact_tracks_confirmation_not_a_reproposed_source(self):
        original = self.propose()
        self.assertFalse(any(item['kind'] == knowledge.KIND
            for item in self.store.source_impact('chapter')['direct_dependents']['items']))
        confirmed = self.confirm(original)
        other = self.store.create_document(kind='chapter', slug='other-source', title='Other source',
            content='Another accepted source.', state='accepted')
        reproposed = self.propose(statement={**self.statement, 'content': 'Different provisional cognition.',
            'sources': [{'document_id': other.id, 'version_id': other.active_version_id}]},
            expected_version_id=confirmed['version_id'])
        current = self.store.source_impact('chapter')['direct_dependents']['items']
        row = next(item for item in current if item['kind'] == knowledge.KIND)
        self.assertEqual(row['version_id'], reproposed['version_id'])
        self.assertEqual(row['confirmed_proposal_version_id'], original['version_id'])
        self.assertEqual(row['refs'][0]['version_id'], self.source.active_version_id)
        other_impact = self.store.source_impact(other.id)['direct_dependents']['items']
        self.assertFalse(any(item['kind'] == knowledge.KIND for item in other_impact))
        new_confirmation = self.confirm(reproposed)
        old_impact = self.store.source_impact('chapter')['direct_dependents']['items']
        self.assertFalse(any(item['kind'] == knowledge.KIND for item in old_impact))
        row = next(item for item in self.store.source_impact(other.id)['direct_dependents']['items']
                   if item['kind'] == knowledge.KIND)
        self.assertEqual(row['confirmed_proposal_version_id'], new_confirmation['confirmed']['proposal_version_id'])


if __name__ == '__main__':
    unittest.main()
