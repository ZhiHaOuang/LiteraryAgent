"""Registered cognition is a scoped projection, never a world-truth migration."""
from __future__ import annotations

from dataclasses import asdict
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lg_cli import narrative_view
from lg_cli.generation_sources import canonical_sha
from lg_cli.project_store import ProjectStore, ProjectStoreError


class NarrativeViewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = ProjectStore(Path(temporary.name))
        self.store.initialize(name='Registered cognition views')
        self.source = self.store.create_document(kind='chapter', slug='chapter', title='Source',
            content='WORLD_SOURCE_SECRET: the captain is dead.', state='accepted', sequence=900)
        self.first = self.store.create_document(kind='character', slug='first', title='First investigator',
            content='LEGACY_PROFILE_SECRET: later in the story the investigator knows everything.', state='accepted')
        self.second = self.store.create_document(kind='character', slug='second', title='Second investigator',
            content='SECOND_PROFILE_SECRET: a different perspective.', state='accepted')
        self.fact = self.store.set_fact(category='world', key='captain', value='The captain is dead.',
            source_document=self.source.id, source_version=1)

    def save(self, slug='belief', *, audience='character', holder=None, key='captain-status',
             start=0, until=None, content='The captain is alive.', stance='believes', sources=None,
             confirm=True, rationale='DECLARED_RATIONALE_PRIVATE: context, not verified author speech.'):
        if audience == 'character' and holder is None:
            holder = self.first.id
        record = self.store.propose_knowledge(slug=slug, title=slug,
            topic={'audience': audience, 'holder_document_id': holder, 'key': key, 'from_position': start},
            statement={'content': content, 'stance': stance, 'until_position': until,
                       'sources': [] if sources is None else sources},
            rationale=rationale, expected_version_id=0)
        if confirm:
            record = self.store.confirm_knowledge(slug, proposal_version_number=record['proposal_version_number'],
                                                   expected_version_id=record['version_id'])
        return record

    def view(self, position=5, *, audience='character', holder=None, limit=100):
        if audience == 'character' and holder is None:
            holder = self.first.id
        return self.store.knowledge_view(audience=audience, position=position, holder_document=holder, limit=limit)

    def repropose(self, record, *, content, sources=None):
        statement = {key: value for key, value in record['proposal'].items() if key != 'rationale'}
        statement['content'] = content
        if sources is not None:
            statement['sources'] = sources
        return self.store.propose_knowledge(slug=record['slug'], title=record['title'], topic=record['topic'],
            statement=statement, rationale='New declared context.', expected_version_id=record['version_id'])

    def test_two_holders_can_disagree_without_altering_canonical_facts(self):
        self.save(content='The captain is alive.', stance='knows')
        self.save('second-belief', holder=self.second.id, content='The captain is dead.', stance='uncertain')
        before = [asdict(fact) for fact in self.store.list_facts(limit=None)]
        history = self.store.fact_history(self.fact.id)
        first = self.view()
        second = self.view(holder=self.second.id)
        self.assertEqual([entry['content'] for entry in first['statements']], ['The captain is alive.'])
        self.assertEqual([entry['stance'] for entry in first['statements']], ['knows'])
        self.assertEqual([entry['content'] for entry in second['statements']], ['The captain is dead.'])
        self.assertEqual([entry['stance'] for entry in second['statements']], ['uncertain'])
        self.assertEqual([asdict(fact) for fact in self.store.list_facts(limit=None)], before)
        self.assertEqual(self.store.fact_history(self.fact.id), history)
        self.assertEqual(self.store.get_document('chapter'), self.source)
        self.assertEqual(self.store.list_proposals(), [])

    def test_reader_lead_and_flashback_use_independent_explicit_axes(self):
        self.save('reader-reveal', audience='reader', start=1, content='The reader has seen the later death.')
        self.save('later-character', start=10, content='The investigator learned the truth at story position 10.')
        reader = self.view(2, audience='reader')
        flashback = self.view(2)
        later = self.view(10)
        self.assertEqual((reader['view']['axis'], reader['view']['holder_document_id']), ('reading', None))
        self.assertEqual(flashback['view']['axis'], 'story')
        self.assertEqual([entry['slug'] for entry in reader['statements']], ['reader-reveal'])
        self.assertEqual(flashback['statements'], [])
        self.assertEqual([entry['slug'] for entry in later['statements']], ['later-character'])
        self.assertEqual(self.source.sequence, 900)
        self.assertEqual(reader['view']['position'], 2)
        self.assertTrue(reader['view']['registered_filter_applied'])
        self.assertFalse(reader['view']['model_input_isolation'])

    def test_half_open_intervals_include_start_and_exclude_end(self):
        self.save('before', start=0, until=10, content='Earlier belief.')
        self.save('after', start=10, until=20, content='Later belief.')
        before = self.view(-1)
        self.assertEqual((before['statements'], before['counts']['considered']), ([], 0))
        self.assertEqual([item['content'] for item in self.view(0)['statements']], ['Earlier belief.'])
        self.assertEqual([item['content'] for item in self.view(9)['statements']], ['Earlier belief.'])
        boundary = self.view(10)
        self.assertEqual([item['content'] for item in boundary['statements']], ['Later belief.'])
        self.assertEqual(boundary['counts']['outside_interval'], 1)
        end = self.view(20)
        self.assertEqual(end['statements'], [])
        self.assertEqual(end['counts']['outside_interval'], 2)

    def test_negative_unbounded_positions_are_not_inferred_from_chapter_numbers(self):
        self.save(start=-20, until=None)
        self.assertEqual(self.view(-21)['statements'], [])
        self.assertEqual(len(self.view(-20)['statements']), 1)
        self.assertEqual(len(self.view(1000000000)['statements']), 1)
        self.assertEqual(self.view(-20)['statements'][0]['topic']['from_position'], -20)

    def test_stale_pending_unavailable_sources_withhold_contents_without_withdrawing(self):
        records = []
        for status, state in (('stale', 'accepted'), ('pending', 'draft'), ('unavailable', 'archived')):
            source = self.store.create_document(kind='chapter', slug='source-' + status, title=status,
                content='PRIVATE_SOURCE_' + status, state='accepted')
            record = self.save('record-' + status, key=status, content='PRIVATE_WITHHELD_' + status,
                sources=[{'document_id': source.id, 'version_id': source.active_version_id}])
            self.store.create_version(source.id, content='PRIVATE_REPLACEMENT_' + status, state=state, activate=True)
            records.append((record, status))
        result = self.view()
        self.assertEqual(result['statements'], [])
        self.assertEqual(result['counts']['withheld'], 3)
        self.assertEqual({item['source_status'] for item in result['withheld']}, {'stale', 'pending', 'unavailable'})
        self.assertEqual(result['coverage'], 'unknown')
        for record, status in records:
            snapshot = self.store.knowledge_snapshot(record['slug'])
            self.assertEqual(snapshot['status'], 'confirmed')
            self.assertEqual(snapshot['confirmed'], record['confirmed'])
            self.assertEqual(snapshot['source_status'], status)
            self.assertNotIn('PRIVATE_WITHHELD_' + status, json.dumps(result))
            self.assertNotIn('PRIVATE_SOURCE_' + status, json.dumps(result))
            self.assertNotIn('PRIVATE_REPLACEMENT_' + status, json.dumps(result))

    def test_unbound_author_assertion_is_visible_without_verified_truth_or_rationale(self):
        record = self.save(stance='knows')
        result = self.view()
        self.assertEqual(result['statements'][0]['source_status'], 'unbound')
        self.assertEqual(result['statements'][0]['proposal_version_id'], record['confirmed']['proposal_version_id'])
        self.assertEqual(result['statements'][0]['content'], 'The captain is alive.')
        self.assertNotIn('DECLARED_RATIONALE_PRIVATE', json.dumps(result))
        self.assertEqual(self.store.get_fact(self.fact.id), self.fact)
        self.assertTrue(any('not proof' in reason for reason in result['unknown_reasons']))

    def test_reproposal_retains_exact_confirmation_and_view_fingerprint(self):
        confirmed = self.save(content='Original confirmed belief.')
        before = self.view()
        pending_source = self.store.create_version('chapter', content='Unaccepted new source.')
        reproposed = self.repropose(confirmed, content='PRIVATE_UNCONFIRMED_REPLACEMENT',
            sources=[{'document_id': self.source.id, 'version_id': pending_source.id}])
        self.assertNotEqual(reproposed['version_id'], confirmed['version_id'])
        after = self.view()
        self.assertEqual(after, before)
        self.assertEqual(after['statements'][0]['proposal_version_id'], confirmed['confirmed']['proposal_version_id'])
        self.assertNotIn('PRIVATE_UNCONFIRMED_REPLACEMENT', json.dumps(after))

    def test_withdrawal_and_later_proposal_never_fall_back_to_historical_confirmation(self):
        confirmed = self.save(content='A past confirmed belief.')
        latest = self.repropose(confirmed, content='A newer unconfirmed belief.')
        withdrawn = self.store.withdraw_knowledge(latest['slug'], expected_version_id=latest['version_id'])
        self.assertEqual(withdrawn['status'], 'withdrawn')
        result = self.view()
        self.assertEqual(result['statements'], [])
        self.assertEqual(result['counts']['unconfirmed'], 1)
        self.assertNotIn('A past confirmed belief.', json.dumps(result))
        self.repropose(withdrawn, content='Another unconfirmed proposal.')
        self.assertEqual(self.view()['statements'], [])
        history = self.store.get_version(confirmed['slug'], confirmed['version_number'])
        self.assertIsNotNone(json.loads(history.content)['confirmed'])

    def test_legacy_global_facts_profiles_timeline_and_plans_are_not_auto_visible(self):
        self.store.add_timeline_entry(label='Legacy timeline', event='LEGACY_TIMELINE_SECRET', state='canonical')
        options = self.store.save_decision_options(slug='future-plan', title='Future plan',
            topic={'scope': 'work', 'document_id': None, 'key': 'future'},
            options=[{'id': key, 'title': key, 'content': 'LEGACY_PLAN_SECRET_' + key} for key in ('A', 'B', 'C')],
            sources=[], request='Compare future plans.', expected_version_id=0)
        self.store.select_decision_option(options['slug'], options_version_number=options['options_version_number'],
            option_id='A', expected_version_id=options['version_id'])
        for audience in ('character', 'reader'):
            with self.subTest(audience=audience):
                result = self.view(audience=audience)
                self.assertEqual(result['statements'], [])
                self.assertEqual(result['counts']['considered'], 0)
                self.assertEqual(result['coverage'], 'unknown')
                encoded = json.dumps(result)
                for forbidden in ('WORLD_SOURCE_SECRET', 'LEGACY_PROFILE_SECRET', 'LEGACY_TIMELINE_SECRET',
                                  'LEGACY_PLAN_SECRET', 'SECOND_PROFILE_SECRET', 'The captain is dead.'):
                    self.assertNotIn(forbidden, encoded)
                self.assertTrue(any('legacy/global' in reason for reason in result['unknown_reasons']))

    def test_sql_filters_other_holders_and_future_records_before_scan_budget(self):
        own = self.save('own', start=0)
        reader = self.save('reader', audience='reader', start=0)
        for index in range(3):
            self.save('other-' + str(index), holder=self.second.id, key='other-' + str(index), start=100)
            self.save('future-' + str(index), key='future-' + str(index), start=100)
            self.save('future-reader-' + str(index), audience='reader', key='future-' + str(index), start=100)
        with patch.object(narrative_view, 'MAX_SCAN', 1):
            character_view = self.view(5)
            reader_view = self.view(5, audience='reader')
        for result, expected in ((character_view, own), (reader_view, reader)):
            with self.subTest(audience=result['view']['audience']):
                self.assertEqual([item['record_id'] for item in result['statements']], [expected['document_id']])
                self.assertEqual(result['counts']['considered'], 1)
                self.assertFalse(result['truncated'])

    def test_output_limit_and_scan_budget_report_distinct_omissions(self):
        for index in range(3):
            self.save('belief-' + str(index), key='key-' + str(index))
        output_limited = self.view(limit=1)
        self.assertEqual(output_limited['counts']['considered'], 3)
        self.assertEqual(output_limited['counts']['visible'], 3)
        self.assertEqual(len(output_limited['statements']), 1)
        self.assertTrue(output_limited['truncated'])
        self.assertTrue(any('output budget' in reason for reason in output_limited['unknown_reasons']))
        with patch.object(narrative_view, 'MAX_SCAN', 2):
            scan_limited = self.view()
        self.assertEqual(scan_limited['counts']['considered'], 2)
        self.assertEqual(len(scan_limited['statements']), 2)
        self.assertTrue(scan_limited['truncated'])
        self.assertTrue(any('scan budget' in reason for reason in scan_limited['unknown_reasons']))

    def test_withheld_output_is_bounded_without_returning_any_statement_contents(self):
        for index in range(3):
            self.save('withheld-' + str(index), key='key-' + str(index), content='PRIVATE_WITHHELD_CONTENT',
                sources=[{'document_id': self.source.id, 'version_id': self.source.active_version_id}])
        self.store.edit_active_document('chapter', content='Changed source.',
            expected_version_id=self.source.active_version_id, reason='Author edit')
        result = self.view(limit=1)
        self.assertEqual(result['counts']['withheld'], 3)
        self.assertEqual(len(result['withheld']), 1)
        self.assertTrue(result['truncated'])
        self.assertNotIn('PRIVATE_WITHHELD_CONTENT', json.dumps(result))

    def test_fingerprint_is_stable_read_only_and_ignores_other_or_future_records(self):
        self.save()
        before = self.view()
        self.assertEqual(before['fingerprint'], canonical_sha({key: value for key, value in before.items()
                                                             if key != 'fingerprint'}))
        self.save('other-holder', holder=self.second.id)
        self.save('future', key='other-topic', start=100)
        self.assertEqual(self.view(), before)
        with self.store._connection() as connection:
            database = list(connection.iterdump())
        with patch.object(self.store, '_connection', wraps=self.store._connection) as opened:
            same = self.view(holder='first')
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(same, before)
        with self.store._connection() as connection:
            self.assertEqual(list(connection.iterdump()), database)
        record = self.store.knowledge_snapshot('belief')
        self.store.withdraw_knowledge('belief', expected_version_id=record['version_id'])
        self.assertNotEqual(self.view()['fingerprint'], before['fingerprint'])

    def test_source_change_changes_fingerprint_and_preserves_the_confirmed_record(self):
        record = self.save(sources=[{'document_id': self.source.id, 'version_id': self.source.active_version_id}])
        before = self.view()
        self.store.edit_active_document('chapter', content='New author source.',
            expected_version_id=self.source.active_version_id, reason='Author edit')
        after = self.view()
        self.assertNotEqual(after['fingerprint'], before['fingerprint'])
        self.assertEqual(after['statements'], [])
        self.assertEqual(after['withheld'][0]['source_status'], 'stale')
        self.assertEqual(self.store.knowledge_snapshot('belief')['confirmed'], record['confirmed'])

    def test_invalid_active_record_is_excluded_without_private_content_or_history_fallback(self):
        record = self.save(content='Original valid confirmation.')
        newer = self.repropose(record, content='PRIVATE_INVALID_PROPOSAL')
        document = self.store.get_document(record['slug'])
        payload = json.loads(document.content)
        payload['proposal']['stance'] = 'PRIVATE_INVALID_ENUM'
        with self.store._connection() as connection:
            connection.execute('UPDATE document_versions SET content=? WHERE id=?',
                               (json.dumps(payload), newer['version_id']))
        result = self.view()
        self.assertEqual(result['counts']['invalid'], 1)
        self.assertEqual(result['statements'], [])
        for forbidden in ('PRIVATE_INVALID_PROPOSAL', 'PRIVATE_INVALID_ENUM', 'Original valid confirmation.'):
            self.assertNotIn(forbidden, json.dumps(result))

    def test_illegal_audience_position_holder_and_budget_are_controlled_errors(self):
        defaults = {'audience': 'character', 'position': 0, 'holder_document': self.first.id, 'limit': 100}
        invalid = [
            {'audience': 'author'}, {'audience': []}, {'audience': {}}, {'audience': True},
            {'position': True}, {'position': '1'}, {'position': 1.5}, {'position': None},
            {'position': -1000000001}, {'position': 1000000001},
            {'holder_document': None}, {'holder_document': True}, {'holder_document': self.source.id},
            {'holder_document': 'missing-character'}, {'audience': 'reader'},
            {'limit': 0}, {'limit': True}, {'limit': 1001}, {'limit': '1'},
        ]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ProjectStoreError):
                self.store.knowledge_view(**{**defaults, **changes})
        retired = self.store.create_document(kind='character', slug='retired', title='Retired', state='archived')
        with self.assertRaises(ProjectStoreError):
            self.view(holder=retired.id)


if __name__ == '__main__':
    unittest.main()
