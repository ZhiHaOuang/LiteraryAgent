from __future__ import annotations

import copy
import asyncio
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from lg_cli.project_store import ProjectStore, ProjectStoreError


class CreativeDecisionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = ProjectStore(Path(self.directory.name))
        self.store.initialize(name='Creative choices')
        self.chapter = self.store.create_document(kind='chapter', slug='ch1', title='Chapter one',
            content='The original chapter remains intact.', state='accepted', sequence=1)
        self.fact = self.store.set_fact(category='world', key='river', value='The river runs north.')
        self.topic = {'scope': 'chapter', 'document_id': self.chapter.id, 'key': 'ending'}
        self.options = [{'id': key, 'title': title, 'content': content} for key, title, content in (
            ('A', 'Escape', 'The ferryman helps the protagonist escape.'),
            ('B', 'Stay', 'The protagonist stays and exposes the conspiracy.'),
            ('C', 'Bargain', 'The protagonist bargains with the rival family.'))]
        self.sources = [{'document_id': self.chapter.id, 'version_id': self.chapter.active_version_id}]

    def save(self, **changes):
        arguments = dict(slug='ending-choice', title='Choose the ending', topic=self.topic,
            options=self.options, sources=self.sources, request='Explore three possible endings.', expected_version_id=0)
        arguments.update(changes)
        return self.store.save_decision_options(**arguments)

    def select(self, snapshot, **changes):
        arguments = dict(options_version_number=1, option_id='B', expected_version_id=snapshot['version_id'])
        arguments.update(changes)
        return self.store.select_decision_option(snapshot['slug'], **arguments)

    def assert_story_unchanged(self):
        self.assertEqual(self.store.get_document(self.chapter.id), self.chapter)
        self.assertEqual(self.store.get_fact(self.fact.id), self.fact)

    def test_three_real_options_start_unselected_and_do_not_change_story(self):
        result = self.save()
        self.assertEqual((result['status'], result['selection']), ('unselected', None))
        self.assertEqual(result['options'], self.options)
        document = self.store.get_document(result['slug'])
        self.assertEqual((document.kind, document.state, document.active_version_id),
            ('creative_decision', 'draft', result['version_id']))
        self.assertEqual(json.loads(document.content)['selection'], None)
        self.assertEqual(len(self.store.list_versions(document.id)), 1)
        self.assert_story_unchanged()

    def test_exact_old_set_can_be_selected_after_new_options_are_saved(self):
        first = self.save()
        options = [dict(item, content=item['content'] + ' Alternative set.') for item in self.options]
        second = self.save(options=options, expected_version_id=first['version_id'])
        self.assertIsNone(second['selection'])
        selected = self.select(second)
        self.assertEqual(selected['status'], 'selected')
        self.assertEqual(selected['selection']['options_version_id'], first['version_id'])
        self.assertEqual(selected['selection']['content'], self.options[1]['content'])
        self.assertEqual(selected['options'], options)
        self.assertEqual(self.store.decision_snapshot(first['slug']), selected)
        self.assert_story_unchanged()

    def test_regeneration_preserves_exact_choice_and_never_selects_new_options(self):
        selected = self.select(self.save())
        options = [dict(item, content=item['content'] + ' Newer possibility.') for item in self.options]
        regenerated = self.save(options=options, expected_version_id=selected['version_id'])
        self.assertEqual(regenerated['selection'], selected['selection'])
        self.assertEqual(regenerated['status'], 'selected')
        self.assertEqual(regenerated['options'], options)
        self.assert_story_unchanged()

    def test_withdrawal_has_no_fallback_and_keeps_history_and_real_options(self):
        selected = self.select(self.save())
        cleared = self.store.withdraw_decision(selected['slug'], expected_version_id=selected['version_id'])
        self.assertEqual((cleared['status'], cleared['selection']), ('withdrawn', None))
        self.assertEqual(cleared['options'], self.options)
        history = self.store.list_versions(selected['slug'])
        self.assertEqual(len(history), 3)
        self.assertIsNotNone(json.loads(history[1].content)['selection'])
        self.assertIsNone(self.store.list_decision_snapshots()[0]['selection'])
        regenerated = self.save(expected_version_id=cleared['version_id'])
        self.assertEqual((regenerated['status'], regenerated['selection']), ('withdrawn', None))
        with self.assertRaises(ProjectStoreError):
            self.store.withdraw_decision(selected['slug'], expected_version_id=regenerated['version_id'])
        self.assert_story_unchanged()

    def test_stale_source_refuses_selection_but_does_not_prevent_withdrawal(self):
        first = self.save()
        selected = self.select(first)
        self.store.edit_active_document(self.chapter.id, content='Author changed the premise.',
            expected_version_id=self.chapter.active_version_id, reason='Author edit')
        current = self.store.decision_snapshot(first['slug'])
        self.assertEqual((current['source_status'], current['status']), ('stale', 'selected'))
        self.assertEqual(current['selection'], selected['selection'])
        with self.assertRaisesRegex(ProjectStoreError, 'source changed'):
            self.select(current, option_id='A')
        cleared = self.store.withdraw_decision(first['slug'], expected_version_id=current['version_id'])
        self.assertIsNone(cleared['selection'])
        self.assertEqual(cleared['source_status'], 'stale')
        self.assertEqual(len(self.store.list_versions(first['slug'])), 3)
        self.assertEqual(self.store.get_fact(self.fact.id), self.fact)

    def test_stale_head_preview_and_duplicate_submission_do_not_add_versions(self):
        first = self.save()
        preview = self.store.decision_selection_preview(first['slug'], options_version_number=1,
            option_id='B', expected_version_id=first['version_id'])
        self.assertEqual(preview['proposed_selection']['content'], self.options[1]['content'])
        self.assertEqual(len(self.store.list_versions(first['slug'])), 1)
        selected = self.select(first)
        for action in (
            lambda: self.select(first),
            lambda: self.select(selected),
            lambda: self.store.withdraw_decision(first['slug'], expected_version_id=first['version_id']),
            lambda: self.save(expected_version_id=first['version_id']),
        ):
            with self.assertRaises(ProjectStoreError):
                action()
        self.assertEqual(len(self.store.list_versions(first['slug'])), 2)
        self.assert_story_unchanged()

    def test_generic_document_mutators_cannot_bypass_decision_transitions(self):
        first = self.save()
        selected = self.select(first)
        document = self.store.get_document(first['slug'])
        for action in (
            lambda: self.store.create_document(kind='creative_decision', slug='injected', title='Injected',
                content=document.content, state='accepted'),
            lambda: self.store.update_document(document.id, metadata={'selection': 'C'}),
            lambda: self.store.update_document(document.id, title='Hijacked'),
            lambda: self.store.create_version(document.id, content='Injected', state='accepted', activate=True),
            lambda: self.store.create_version(document.id, content='Injected'),
            lambda: self.store.edit_active_document(document.id, content='Injected',
                expected_version_id=selected['version_id'], reason='Generic author edit'),
            lambda: self.store.accept_version(document.id, 1),
            lambda: self.store.restore_version(document.id, 1),
            lambda: self.store.reject_version(document.id, 1),
        ):
            with self.subTest(action=action), self.assertRaisesRegex(ProjectStoreError, 'dedicated'):
                action()
            self.assertEqual(self.store.get_document(document.id), document)
        self.assertEqual(len(self.store.list_versions(document.id)), 2)
        self.assert_story_unchanged()

    def test_topic_is_immutable_unique_and_bound_to_real_scope(self):
        first = self.save()
        for changes in (
            {'topic': {**self.topic, 'key': 'betrayal'}, 'expected_version_id': first['version_id']},
            {'slug': 'duplicate-topic'},
            {'slug': 'wrong-kind', 'topic': {'scope': 'part', 'document_id': self.chapter.id, 'key': 'ending'}},
            {'slug': 'missing-source', 'sources': []},
            {'slug': 'global-target', 'topic': {'scope': 'work', 'document_id': self.chapter.id, 'key': 'ending'}},
            {'slug': 'missing-target', 'topic': {'scope': 'chapter', 'key': 'ending'}},
        ):
            with self.subTest(changes=changes), self.assertRaises(ProjectStoreError):
                self.save(**changes)
        self.assertEqual(len(self.store.list_documents(kind='creative_decision')), 1)
        self.assertEqual(self.store.decision_snapshot(first['slug']), first)

    def test_request_only_new_work_persists_actual_request_with_no_fake_source(self):
        result = self.save(topic={'scope': 'work', 'document_id': None, 'key': 'premise'}, sources=[],
            request='A river city story about choosing loyalty.')
        self.assertEqual(result['source_status'], 'unbound')
        self.assertEqual(result['sources'], [])
        self.assertEqual(result['request'], 'A river city story about choosing loyalty.')
        self.assertEqual(len(result['request_sha256']), 64)
        selected = self.select(result)
        self.assertEqual(selected['selection']['request'], result['request'])
        self.assertEqual(len(self.store.list_documents()), 2)

    def test_invalid_option_sets_and_sources_are_rejected_without_placeholder(self):
        duplicate_id = copy.deepcopy(self.options)
        duplicate_id[1]['id'] = 'A'
        duplicate_content = copy.deepcopy(self.options)
        duplicate_content[1]['content'] = duplicate_content[0]['content']
        empty = copy.deepcopy(self.options)
        empty[1]['content'] = ' '
        unrelated = self.store.create_document(kind='note', slug='other-source', title='Other', content='Other content')
        for changes in (
            {'options': self.options[:2]}, {'options': [*self.options, self.options[0]]},
            {'options': duplicate_id}, {'options': duplicate_content}, {'options': empty},
            {'sources': [{'document_id': self.chapter.id, 'version_id': unrelated.active_version_id}]},
            {'sources': [self.sources[0], self.sources[0]]},
            {'expected_version_id': True},
            {'topic': {'scope': 'work'}, 'sources': [], 'request': ''},
        ):
            with self.subTest(changes=changes), self.assertRaises(ProjectStoreError):
                self.save(**changes)
        self.assertEqual(self.store.list_documents(kind='creative_decision'), [])
        self.assert_story_unchanged()

    def test_wrong_option_version_and_foreign_document_do_not_select(self):
        first = self.save()
        selected = self.select(first)
        for changes in ({'option_id': 'missing'}, {'options_version_number': 2},
                        {'options_version_number': True}, {'options_version_number': 999}):
            with self.subTest(changes=changes), self.assertRaises(ProjectStoreError):
                self.select(selected, **changes)
        with self.assertRaises(ProjectStoreError):
            self.store.select_decision_option(self.chapter.slug, options_version_number=1, option_id='A',
                expected_version_id=self.chapter.active_version_id)
        self.assertEqual(self.store.decision_snapshot(first['slug']), selected)

    def test_current_and_options_source_status_are_independent_after_regeneration(self):
        selected = self.select(self.save())
        revised = self.store.edit_active_document(self.chapter.id, content='New chapter premise.',
            expected_version_id=self.chapter.active_version_id, reason='Author edit')
        options = [dict(item, content=item['content'] + ' Reconciled.') for item in self.options]
        result = self.save(options=options, sources=[{'document_id': self.chapter.id, 'version_id': revised.id}],
            expected_version_id=selected['version_id'])
        self.assertEqual((result['source_status'], result['options_source_status']), ('stale', 'current'))
        self.assertEqual(result['selection'], selected['selection'])
        fresh = self.select(result, options_version_number=result['version_number'], option_id='A')
        self.assertEqual(fresh['source_status'], 'current')

    def test_concurrent_selections_have_one_winner_and_no_overwrite(self):
        first = self.save()
        def choose(option_id):
            try:
                store = ProjectStore(self.store.workspace)
                return store.select_decision_option(first['slug'], options_version_number=1,
                    option_id=option_id, expected_version_id=first['version_id'])
            except ProjectStoreError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(choose, ['A', 'B']))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.store.list_versions(first['slug'])), 2)
        self.assert_story_unchanged()

    def test_concurrent_initial_topic_creation_has_one_authoritative_record(self):
        def create(slug):
            try:
                return self.save(slug=slug)
            except ProjectStoreError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, ['topic-one', 'topic-two']))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.store.list_documents(kind='creative_decision')), 1)

    def test_generic_context_excludes_raw_options_and_reports_choice_state(self):
        first = self.save()
        context = self.store.context_snapshot(query='ferryman')
        self.assertNotIn(first['document_id'], [item.id for item in context['documents']])
        self.assertIsNone(context['creative_decisions'][0]['selection'])
        self.assertNotIn('options', context['creative_decisions'][0])
        self.assertNotIn('request', context['creative_decisions'][0])
        selected = self.select(first)
        context = self.store.context_snapshot()
        self.assertEqual(context['creative_decisions'][0]['selection']['content'], self.options[1]['content'])
        self.assertNotIn(self.options[0]['content'], json.dumps(context['creative_decisions']))
        self.store.withdraw_decision(first['slug'], expected_version_id=selected['version_id'])
        self.assertIsNone(self.store.context_snapshot()['creative_decisions'][0]['selection'])

    def test_live_project_memory_uses_same_current_choice_projection(self):
        from lg_cli.studio_operations import invoke
        from lg_cli.writing_mcp import create_server
        from tests.helpers import make_config
        first = self.save()
        selected = self.select(first)
        async def memory():
            return await invoke(create_server(make_config(self.store.workspace)), 'project_memory', {})
        value = asyncio.run(memory())
        self.assertEqual(value['creative_decisions'], self.store.decision_context()['decisions'])
        text = json.dumps(value['creative_decisions'])
        self.assertIn(self.options[1]['content'], text)
        self.assertNotIn(self.options[0]['content'], text)
        self.assertNotIn(self.options[2]['content'], text)
        self.assertEqual(value['view']['limits']['creative_decisions'], 100)
        self.assertFalse(value['view']['truncated']['creative_decisions'])
        self.store.withdraw_decision(first['slug'], expected_version_id=selected['version_id'])
        value = asyncio.run(memory())
        self.assertIsNone(value['creative_decisions'][0]['selection'])
        self.assertNotIn(self.options[1]['content'], json.dumps(value['creative_decisions']))

    def test_context_has_explicit_decision_limit_and_truncation(self):
        self.save()
        self.save(slug='other-choice', topic={**self.topic, 'key': 'opening'})
        value = self.store.decision_context(limit=1)
        self.assertEqual((value['total'], value['limit'], len(value['decisions']), value['truncated']), (2, 1, 1, True))
        context = self.store.context_snapshot(max_documents=1)
        self.assertTrue(context['view']['truncated']['creative_decisions'])
        self.assertEqual(context['view']['limits']['creative_decisions'], 1)
        self.assertNotIn('options', context['creative_decisions'][0])
        self.assertEqual(len(self.store.list_decision_snapshots(limit=None)), 2)
        for limit in (True, 0, 1001, None):
            with self.subTest(limit=limit), self.assertRaises(ProjectStoreError):
                self.store.decision_context(limit=limit)


if __name__ == '__main__':
    unittest.main()
