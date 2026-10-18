import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lg_cli.init_project import init_workspace
from lg_cli.project_store import ProjectStore
from lg_cli.story_context import render_story_context
from lg_cli.studio import PROFILE_SLUG, StudioProfile, profile
from lg_cli.task_scope import bind_task, check_operation
from lg_cli.workspace_library import entry, library, writing_context
from tests.helpers import make_config


class DecisionProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.enterContext(patch.dict(os.environ, {'LITERARYGIANT_REGISTRY_HOME': str(self.root / 'registry')}))
        init_workspace(self.root)
        self.store = ProjectStore(self.root)
        self.config = make_config(self.root)
        self.options = [
            {'id': 'a', 'title': '保留悬念', 'content': 'OPTION_A_UNSELECTED：维修师暂时保留故障线索。'},
            {'id': 'b', 'title': '公开线索', 'content': 'OPTION_B_SELECTED：维修师向工友公开故障线索。'},
            {'id': 'c', 'title': '先行试验', 'content': 'OPTION_C_UNSELECTED：维修师先验证故障假设。'},
        ]

    def save(self, slug='direction', *, topic=None, sources=None):
        return self.store.save_decision_options(slug=slug, title='救援方向',
            topic=topic or {'scope': 'work', 'document_id': None, 'key': 'rescue'},
            options=self.options, sources=sources or [], request='为当前救援方向提出三个可比较的方案。',
            expected_version_id=0)

    def select(self, saved):
        return self.store.select_decision_option(saved['slug'],
            options_version_number=saved['version_number'], option_id='b', expected_version_id=saved['version_id'])

    def detail(self, value):
        return entry(self.config, 'pending', f"decision:{value['document_id']}")

    def context_section(self):
        text = render_story_context(self.store.context_snapshot())
        return text.split('## Author Creative Decisions\n', 1)[1].split('## Canonical Facts', 1)[0]

    def test_new_options_are_readable_without_selection_or_generic_adoption(self):
        saved = self.save()
        before = self.store.decision_snapshot(saved['slug'])
        page = library(self.config, 'pending')
        self.assertEqual(page['total'], 4)
        row = page['entries'][0]
        self.assertEqual(row['key'], f"decision:{saved['document_id']}")
        self.assertEqual(row['status'], 'unselected')
        self.assertEqual(row['actions'], [])
        detail = self.detail(saved)
        self.assertEqual(detail['versions'], [])
        self.assertIn('当前无正式选择', detail['body'])
        self.assertIn('当前版本：v1', detail['body'])
        self.assertIn('未绑定文档来源', detail['body'])
        self.assertIn('声明的创作请求上下文', detail['body'])
        for option in self.options:
            self.assertIn(option['title'], detail['body'])
            self.assertIn(option['content'], detail['body'])
        with self.assertRaisesRegex(ValueError, 'not available'):
            writing_context(self.config, 'pending', detail['key'], 'adopt', detail['fingerprint'])
        self.assertEqual(self.store.decision_snapshot(saved['slug']), before)
        self.assertEqual(self.store.list_facts(), [])

    def test_selected_context_contains_only_exact_choice_and_explicit_scope(self):
        part = self.store.create_document(kind='part', slug='part', title='卷一', content='卷一范围。', state='accepted')
        selected = self.select(self.save(topic={'scope': 'part', 'document_id': part.id, 'key': 'ending'},
            sources=[{'document_id': part.id, 'version_id': part.active_version_id}]))
        section = self.context_section()
        self.assertIn('scope=part; topic=ending; scope_document_id=' + str(part.id), section)
        self.assertIn('decision=selected; source=current', section)
        self.assertIn('option_id=b', section)
        self.assertIn(self.options[1]['content'], section)
        self.assertNotIn(self.options[0]['content'], section)
        self.assertNotIn(self.options[2]['content'], section)
        self.assertNotIn(selected['request'], section)
        self.assertIn('not an established story fact', section)
        self.assertIn('options_version_id=' + str(selected['selection']['options_version_id']), section)
        self.assertIn('分卷 · 文档 ' + str(part.id), self.detail(selected)['body'])

    def test_unselected_and_withdrawn_never_reuse_options_or_legacy_profile_as_choice(self):
        saved = self.save()
        self.store.create_document(kind='book', slug=PROFILE_SLUG, title='工作规划',
            content=StudioProfile(volumes=['LEGACY_PROFILE_CHOICE']).model_dump_json())
        section = self.context_section()
        self.assertIn('decision=unselected', section)
        self.assertIn('No current official choice', section)
        for option in self.options:
            self.assertNotIn(option['content'], section)
        self.assertNotIn('LEGACY_PROFILE_CHOICE', section)
        chosen = self.select(saved)
        withdrawn = self.store.withdraw_decision(chosen['slug'], expected_version_id=chosen['version_id'])
        self.assertIsNone(withdrawn['selection'])
        section = self.context_section()
        self.assertIn('decision=withdrawn', section)
        self.assertIn('No current official choice', section)
        self.assertIn('do not reuse a historical choice', section)
        self.assertNotIn(self.options[1]['content'], section)
        detail = self.detail(withdrawn)
        self.assertEqual(detail['status'], 'withdrawn')
        self.assertIn('当前无正式选择', detail['body'])
        self.assertIn(self.options[1]['content'], detail['body'], 'History remains readable as option data')
        self.assertEqual(profile(self.store)[1].volumes, ['LEGACY_PROFILE_CHOICE'])

    def test_stale_selected_source_is_retained_and_warned_without_mutation(self):
        chapter = self.store.create_document(kind='chapter', slug='chapter', title='来源章', content='已采纳的救援行动。', state='accepted')
        sources = [{'document_id': chapter.id, 'version_id': chapter.active_version_id}]
        selected = self.select(self.save(topic={'scope': 'chapter', 'document_id': chapter.id, 'key': 'aftermath'}, sources=sources))
        self.store.edit_active_document(chapter.id, content='作者改写了救援行动。',
            expected_version_id=chapter.active_version_id, reason='作者修订来源')
        before = self.store.decision_snapshot(selected['slug'])
        self.assertEqual(before['source_status'], 'stale')
        section = self.context_section()
        self.assertIn('decision=selected; source=stale', section)
        self.assertIn(self.options[1]['content'], section)
        self.assertIn('NEEDS AUTHOR REVIEW', section)
        self.assertIn('do not treat it as a current executable direction', section)
        self.assertIn(json.dumps(sources, ensure_ascii=False), section)
        detail = self.detail(before)
        self.assertEqual(detail['status'], 'selected')
        self.assertEqual(detail['actions'], ['withdraw'])
        self.assertIn('来源已过期，待作者复核', detail['body'])
        self.assertIn(self.options[1]['content'], detail['body'])
        self.assertEqual(self.store.decision_snapshot(selected['slug']), before)

    def test_managed_documents_do_not_consume_prose_budget_or_become_plain_candidates(self):
        chapter = self.store.create_document(kind='chapter', slug='prose', title='正文', content='PROSE_BUDGET_SENTINEL', state='accepted')
        for number in range(3):
            self.save(slug='direction-' + str(number), topic={'scope': 'work', 'document_id': None, 'key': 'topic-' + str(number)})
        snapshot = self.store.context_snapshot(max_documents=1)
        self.assertEqual([doc.id for doc in snapshot['documents']], [chapter.id])
        self.assertEqual(len(snapshot['creative_decisions']), 1)
        self.assertTrue(snapshot['view']['truncated']['creative_decisions'])
        page = library(self.config, 'pending')
        self.assertEqual(page['total'], 12)
        self.assertEqual(sum(row['key'].startswith('decision:') for row in page['entries']), 3)
        self.assertEqual(sum(row['key'].startswith('decision-option:') for row in page['entries']), 9)
        self.assertTrue(all('adopt' not in row['actions'] for row in page['entries']))
        self.assertNotIn('creative_decision', [doc.kind for doc in snapshot['documents']])

    def test_pending_decisions_keep_pagination_and_changed_preview_guard(self):
        saved = self.save()
        for number in range(25):
            self.store.propose_fact(category='world', key='proposal-' + str(number), value='待确认资料')
        first = library(self.config, 'pending')
        second = library(self.config, 'pending', offset=first['nextOffset'])
        self.assertEqual(first['total'], 29)
        self.assertEqual(len(first['entries']), 24)
        self.assertEqual(len(second['entries']), 5)
        self.assertEqual(len({row['key'] for row in first['entries'] + second['entries']}), 29)
        old = self.detail(saved)
        self.select(saved)
        with self.assertRaisesRegex(ValueError, 'changed after preview'):
            writing_context(self.config, 'pending', old['key'], 'adopt', old['fingerprint'])

    def test_exact_option_click_previews_and_binds_only_its_selection(self):
        saved = self.save()
        key = f"decision-option:{saved['document_id']}:{saved['options_version_number']}:b"
        detail = entry(self.config, 'pending', key)
        self.assertEqual(detail['actions'], ['select'])
        self.assertEqual(detail['versions'], [])
        self.assertIn(self.options[1]['content'], detail['body'])
        prepared = writing_context(self.config, 'pending', key, 'select', detail['fingerprint'])
        self.assertEqual(prepared['task'], {'kind': 'decision', 'decisionAction': 'select',
            'document': saved['slug'], 'sourceVersion': saved['version_id'],
            'versionNumber': saved['options_version_number'], 'optionId': 'b'})
        payload = json.loads(prepared['prompt'].rsplit('\n', 1)[1])
        arguments = {'slug': saved['slug'], 'expected_version_id': saved['version_id'],
                     'options_version_number': saved['options_version_number'], 'option_id': 'b'}
        self.assertEqual(payload['arguments'], arguments)
        self.assertIn('studio_select_decision', prepared['prompt'])
        self.assertIn(self.options[1]['content'], prepared['prompt'])
        self.assertNotIn(self.options[0]['content'], prepared['prompt'])
        self.assertNotIn(self.options[2]['content'], prepared['prompt'])
        scope = bind_task(self.root, prepared['task'])
        self.assertEqual(check_operation(self.root, scope, {'tool': 'studio_select_decision', 'arguments': arguments}), 'grants')
        invalid = [dict(arguments, option_id='a'), dict(arguments, options_version_number=2),
                   dict(arguments, expected_version_id=saved['version_id'] + 1), dict(arguments, slug='another')]
        for value in invalid:
            with self.subTest(arguments=value), self.assertRaises(ValueError):
                check_operation(self.root, scope, {'tool': 'studio_select_decision', 'arguments': value})
        for tool in ('studio_withdraw_decision', 'studio_decision_options', 'studio_save', 'studio_accept', 'edit_manuscript'):
            with self.subTest(tool=tool), self.assertRaises(ValueError):
                check_operation(self.root, scope, {'tool': tool, 'arguments': arguments})
        self.assertEqual(self.store.decision_snapshot(saved['slug']), saved)

    def test_stale_source_disables_selection_but_retains_exact_withdraw_action(self):
        source = self.store.create_document(kind='chapter', slug='source', title='来源', content='版本一。', state='accepted')
        selected = self.select(self.save(sources=[{'document_id': source.id, 'version_id': source.active_version_id}]))
        self.store.edit_active_document(source.id, content='版本二。', expected_version_id=source.active_version_id, reason='作者修改')
        page = library(self.config, 'pending')
        option_rows = [row for row in page['entries'] if row['key'].startswith('decision-option:')]
        self.assertEqual(len(option_rows), 3)
        self.assertTrue(all(row['actions'] == [] for row in option_rows))
        option = entry(self.config, 'pending', option_rows[0]['key'])
        with self.assertRaisesRegex(ValueError, 'not available'):
            writing_context(self.config, 'pending', option['key'], 'select', option['fingerprint'])
        detail = self.detail(selected)
        self.assertEqual(detail['actions'], ['withdraw'])
        prepared = writing_context(self.config, 'pending', detail['key'], 'withdraw', detail['fingerprint'])
        self.assertEqual(prepared['task'], {'kind': 'decision', 'decisionAction': 'withdraw',
            'document': selected['slug'], 'sourceVersion': selected['version_id']})
        arguments = json.loads(prepared['prompt'].rsplit('\n', 1)[1])['arguments']
        self.assertEqual(arguments, {'slug': selected['slug'], 'expected_version_id': selected['version_id']})
        scope = bind_task(self.root, prepared['task'])
        self.assertEqual(check_operation(self.root, scope, {'tool': 'studio_withdraw_decision', 'arguments': arguments}), 'grants')
        with self.assertRaises(ValueError):
            check_operation(self.root, scope, {'tool': 'studio_select_decision', 'arguments': arguments})
        self.assertEqual(self.store.decision_snapshot(selected['slug'])['selection'], selected['selection'])

    def test_only_latest_options_get_rows_and_old_selection_remains_explicit(self):
        saved = self.save()
        selected = self.select(saved)
        selected_key = f"decision-option:{saved['document_id']}:{saved['options_version_number']}:b"
        self.assertEqual(entry(self.config, 'pending', selected_key)['actions'], [])
        options = [dict(option, content='NEW_OPTIONS_' + option['id']) for option in self.options]
        latest = self.store.save_decision_options(slug=saved['slug'], title=saved['title'], topic=saved['topic'],
            options=options, sources=[], request='作者请求另一组三方案。', expected_version_id=selected['version_id'])
        page = library(self.config, 'pending')
        rows = [row for row in page['entries'] if row['key'].startswith('decision-option:')]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row['key'].startswith(f"decision-option:{saved['document_id']}:{latest['options_version_number']}:") for row in rows))
        self.assertTrue(all(row['actions'] == ['select'] for row in rows))
        with self.assertRaisesRegex(ValueError, 'not available'):
            writing_context(self.config, 'pending', selected_key, 'select', page['fingerprint'])
        key = f"decision-option:{saved['document_id']}:{latest['options_version_number']}:b"
        detail = entry(self.config, 'pending', key)
        prepared = writing_context(self.config, 'pending', key, 'select', detail['fingerprint'])
        self.assertEqual(prepared['task']['versionNumber'], latest['options_version_number'])
        self.assertEqual(prepared['task']['sourceVersion'], latest['version_id'])
        self.assertIn('NEW_OPTIONS_b', prepared['prompt'])
        self.assertEqual(self.store.decision_snapshot(saved['slug'])['selection'], selected['selection'])
        self.assertIn(self.options[1]['content'], self.context_section())
        self.assertNotIn('NEW_OPTIONS_b', self.context_section())

    def test_latest_options_source_controls_selection_independently_of_old_choice_source(self):
        for old_bound in (True, False):
            with self.subTest(old_bound=old_bound):
                suffix = str(old_bound).lower()
                source = self.store.create_document(kind='chapter', slug='source-' + suffix, title='来源', content='第一版。', state='accepted')
                sources = [{'document_id': source.id, 'version_id': source.active_version_id}]
                saved = self.save(slug='direction-' + suffix,
                    topic={'scope': 'work', 'document_id': None, 'key': 'topic-' + suffix},
                    sources=sources if old_bound else [])
                selected = self.select(saved)
                if old_bound:
                    self.store.edit_active_document(source.id, content='第二版。', expected_version_id=source.active_version_id, reason='作者更新')
                current_source = self.store.get_document(source.id)
                latest = self.store.save_decision_options(slug=saved['slug'], title=saved['title'], topic=saved['topic'],
                    options=[dict(option, content='NEW_SOURCE_OPTIONS_' + option['id']) for option in self.options],
                    sources=[{'document_id': source.id, 'version_id': current_source.active_version_id}],
                    request='依据当前来源重新比较三个方向。', expected_version_id=selected['version_id'])
                if not old_bound:
                    self.store.edit_active_document(source.id, content='第二版。', expected_version_id=source.active_version_id, reason='作者更新')
                state = self.store.decision_snapshot(saved['slug'])
                self.assertEqual(state['source_status'], 'stale' if old_bound else 'unbound')
                self.assertEqual(state['options_source_status'], 'current' if old_bound else 'stale')
                key = f"decision-option:{saved['document_id']}:{latest['options_version_number']}:b"
                detail = entry(self.config, 'pending', key)
                self.assertEqual(detail['actions'], ['select'] if old_bound else [])
                self.assertEqual(self.detail(state)['actions'], ['withdraw'])
                self.assertIn('方案集来源状态：' + ('当前有效' if old_bound else '来源已过期'), self.detail(state)['body'])
                if old_bound:
                    prepared = writing_context(self.config, 'pending', key, 'select', detail['fingerprint'])
                    self.assertEqual(prepared['task']['versionNumber'], latest['options_version_number'])
                else:
                    with self.assertRaisesRegex(ValueError, 'not available'):
                        writing_context(self.config, 'pending', key, 'select', detail['fingerprint'])
                self.assertEqual(self.store.decision_snapshot(saved['slug'])['selection'], selected['selection'])

    def test_author_change_during_host_preview_invalidates_click_without_selection(self):
        saved = self.save()
        key = f"decision-option:{saved['document_id']}:{saved['options_version_number']}:b"
        detail = entry(self.config, 'pending', key)
        original = ProjectStore.decision_selection_preview
        def change_after_preview(store, *args, **kwargs):
            preview = original(store, *args, **kwargs)
            self.store.create_document(kind='note', slug='author-note', title='作者备注', content='审批前作者新增的资料。')
            return preview
        with patch.object(ProjectStore, 'decision_selection_preview', change_after_preview):
            with self.assertRaisesRegex(ValueError, 'changed while binding'):
                writing_context(self.config, 'pending', key, 'select', detail['fingerprint'])
        self.assertIsNone(self.store.decision_snapshot(saved['slug'])['selection'])

    def test_context_render_remains_bounded_and_legacy_snapshot_is_supported(self):
        self.select(self.save())
        snapshot = self.store.context_snapshot()
        for budget in (0, 100, 1200, 24000):
            with self.subTest(budget=budget):
                self.assertLessEqual(len(render_story_context(snapshot, max_chars=budget)), budget)
        snapshot.pop('creative_decisions')
        legacy = render_story_context(snapshot)
        self.assertNotIn('Author Creative Decisions', legacy)
        snapshot['creative_decisions'] = []
        self.assertEqual(render_story_context(snapshot), legacy)


if __name__ == '__main__':
    unittest.main()
