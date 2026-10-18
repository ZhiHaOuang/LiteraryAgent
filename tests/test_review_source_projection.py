import copy
import json
import os
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from lg_cli.book_analysis import BookAnalysisStore
from lg_cli.project_store import ProjectStore, ProjectStoreError
from lg_cli.supervision import (CanonConflictError, DEFAULT_CATEGORIES, candidate_reports,
                                configure_supervision, supervision_policy)
from lg_cli.workspace_library import entry
from tests.helpers import make_config


class ReviewSourceProjectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch.dict(os.environ, {'LITERARYGIANT_REGISTRY_HOME': str(self.root / 'registry')}))
        self.store = ProjectStore(self.root)
        self.store.initialize(name='审读方向投影')
        self.config = make_config(self.root)
        self.chapter = self.store.create_document(kind='chapter', slug='one', title='第一章',
            content='The mechanic inspects the gate.', state='accepted', sequence=1)
        self.candidate = self.store.create_version(self.chapter.id, content='The mechanic opens the gate.',
            expected_version_id=self.chapter.active_version_id)
        self.store.add_fact(category='world', key='gate', value='The gate is mechanical.', state='canonical')
        configure_supervision(self.store)
        self.records = BookAnalysisStore(self.store)
        self.options = [{'id': name, 'title': '方向 ' + name, 'content': 'REVIEW_DIRECTION_' + name}
                        for name in ('A', 'B', 'C')]
        self.report = {'summary': 'The supplied prose and Canonical sources were checked.', 'entries': [], 'findings': []}

    def options_record(self, slug='ending', *, topic=None, sources=None):
        return self.store.save_decision_options(slug=slug, title='结局方向',
            topic=topic or {'scope': 'work', 'document_id': None, 'key': slug},
            options=self.options, sources=sources or [], request='比较三种结局，暂不选择。', expected_version_id=0)

    def choose(self, record, option='A'):
        return self.store.select_decision_option(record['slug'],
            options_version_number=record['options_version_number'], option_id=option,
            expected_version_id=record['version_id'])

    def detail(self):
        return entry(self.config, 'pending', f'candidate:one:{self.candidate.version_number}')

    def manuscript_state(self):
        return (self.store.get_document('one'), self.store.list_versions('one'),
                [asdict(fact) for fact in self.store.list_facts(limit=None)])

    def save_reviews(self):
        base = self.records.snapshot('one')
        source = self.records.candidate_snapshot('one', self.candidate)
        for category in DEFAULT_CATEGORIES:
            self.records.save(source, category, copy.deepcopy(self.report), 'projection-review',
                              candidate_id=self.candidate.id, base_snapshot=base)
        return source

    def report_files(self):
        return {category: self.records._path('one', category, candidate_id=self.candidate.id).read_bytes()
                for category in DEFAULT_CATEGORIES}

    def assert_reports_current(self):
        self.assertEqual(len(candidate_reports(self.store, self.store.get_document('one'), self.candidate)),
                         len(DEFAULT_CATEGORIES))

    def assert_reports_stale_without_canon_conflict(self):
        with self.assertRaises(ProjectStoreError) as failure:
            candidate_reports(self.store, self.store.get_document('one'), self.candidate)
        self.assertNotIsInstance(failure.exception, CanonConflictError)
        self.assertRegex(str(failure.exception), 'Missing or stale')

    def test_selected_directions_are_current_review_basis_without_generation_claim(self):
        chosen = self.choose(self.options_record())
        self.save_reviews()
        before = self.manuscript_state(), self.report_files()
        detail = self.detail()
        self.assertEqual(detail['actions'], ['review', 'adopt'])
        self.assertIn('当前审读依据：相关已选创作方向', detail['body'])
        self.assertIn(self.options[0]['content'], detail['body'])
        self.assertNotIn(self.options[1]['content'], detail['body'])
        self.assertIn('不证明候选生成时使用了这些方向', detail['body'])
        self.assertIn('不等于生成依赖凭据', detail['body'])
        self.assertIn('不是 Canonical', detail['body'])
        self.assertIn('采纳此候选后的方向来源状态（审读投影）：未绑定文档来源', detail['body'])
        self.assertIn(str(chosen['selection']['options_version_id']), detail['body'])
        self.assertEqual(detail['body'].count('当前有效且无未解决冲突'), len(DEFAULT_CATEGORIES))
        self.assertEqual((self.manuscript_state(), self.report_files()), before)

    def test_new_formal_choice_stales_reports_until_original_candidate_is_reviewed_again(self):
        chosen = self.choose(self.options_record())
        self.save_reviews()
        files = self.report_files()
        before = self.manuscript_state()
        self.choose(chosen, 'B')
        self.assert_reports_stale_without_canon_conflict()
        detail = self.detail()
        self.assertIn(self.options[1]['content'], detail['body'])
        self.assertNotIn(self.options[0]['content'], detail['body'])
        self.assertIn('需按当前方向重新审读', detail['body'])
        self.assertEqual(detail['body'].count('缺失、过期或有待解决冲突'), len(DEFAULT_CATEGORIES))
        self.assertEqual(detail['actions'], ['review', 'adopt'])
        self.assertEqual(self.report_files(), files)
        self.assertEqual(self.manuscript_state(), before)
        self.save_reviews()
        self.assert_reports_current()
        self.assertEqual(self.store.get_version('one', self.candidate.version_number).state, 'candidate')
        self.assertEqual(self.store.get_document('one'), self.chapter)
        self.assertEqual(self.detail()['body'].count('当前有效且无未解决冲突'), len(DEFAULT_CATEGORIES))

    def test_option_regeneration_preserves_review_basis_and_existing_reports(self):
        chosen = self.choose(self.options_record())
        old_source = self.save_reviews()
        files = self.report_files()
        self.store.save_decision_options(slug=chosen['slug'], title=chosen['title'], topic=chosen['topic'],
            options=[dict(option, content='NEW_UNSELECTED_' + option['id']) for option in self.options],
            sources=[], request='重新生成方案，尚未选择。', expected_version_id=chosen['version_id'])
        self.assertEqual(self.records.candidate_snapshot('one', self.candidate), old_source)
        self.assert_reports_current()
        detail = self.detail()
        self.assertIn(self.options[0]['content'], detail['body'])
        self.assertNotIn('NEW_UNSELECTED_', detail['body'])
        self.assertEqual(self.report_files(), files)

    def test_other_chapter_topic_does_not_change_this_candidates_review_basis(self):
        self.choose(self.options_record())
        old_source = self.save_reviews()
        other = self.store.create_document(kind='chapter', slug='other', title='另一章', content='Other chapter.',
                                           state='accepted', sequence=2)
        unrelated = self.options_record(slug='other-ending',
            topic={'scope': 'chapter', 'document_id': other.id, 'key': 'ending'},
            sources=[{'document_id': other.id, 'version_id': other.active_version_id}])
        self.choose(unrelated, 'C')
        self.assertEqual(self.records.candidate_snapshot('one', self.candidate), old_source)
        self.assert_reports_current()
        detail = self.detail()
        self.assertNotIn(self.options[2]['content'], detail['body'])
        self.assertIn(self.options[0]['content'], detail['body'])

    def test_withdrawal_does_not_reuse_old_direction_and_does_not_reject_candidate(self):
        chosen = self.choose(self.options_record())
        self.save_reviews()
        before = self.manuscript_state()
        self.store.withdraw_decision(chosen['slug'], expected_version_id=chosen['version_id'])
        self.assert_reports_stale_without_canon_conflict()
        detail = self.detail()
        self.assertNotIn('当前审读依据：相关已选创作方向', detail['body'])
        self.assertNotIn(self.options[0]['content'], detail['body'])
        self.assertEqual(detail['actions'], ['review', 'adopt'])
        self.assertEqual(self.manuscript_state(), before)

    def test_stale_direction_source_is_warning_not_automatic_canon_conflict(self):
        source = self.store.create_document(kind='note', slug='source', title='方向来源', content='Source version one.', state='accepted')
        self.choose(self.options_record(sources=[{'document_id': source.id, 'version_id': source.active_version_id}]))
        self.save_reviews()
        self.store.edit_active_document(source.id, content='Source version two.',
            expected_version_id=source.active_version_id, reason='作者更新方向来源')
        before = self.manuscript_state()
        self.assert_reports_stale_without_canon_conflict()
        detail = self.detail()
        self.assertIn('采纳此候选后的方向来源状态（审读投影）：来源需复核', detail['body'])
        self.assertIn('不自动构成 Canonical 冲突', detail['body'])
        self.assertEqual(detail['actions'], ['review', 'adopt'])
        self.save_reviews()
        self.assert_reports_current()
        self.assertEqual(self.manuscript_state(), before)

    def test_candidate_detail_uses_the_same_projected_source_status_as_its_review_snapshot(self):
        chosen = self.choose(self.options_record(topic={'scope': 'chapter', 'document_id': self.chapter.id, 'key': 'ending'},
            sources=[{'document_id': self.chapter.id, 'version_id': self.chapter.active_version_id}]))
        self.assertEqual(chosen['source_status'], 'current')
        snapshot = self.records.candidate_snapshot('one', self.candidate)
        self.assertEqual(snapshot['creative_decisions'][0]['source_status'], 'stale')
        detail = self.detail()
        self.assertIn('采纳此候选后的方向来源状态（审读投影）：来源需复核', detail['body'])
        self.assertIn(json.dumps(snapshot['creative_decisions'][0]['source_details'], ensure_ascii=False), detail['body'])
        self.assertEqual(self.store.decision_snapshot(chosen['slug'])['source_status'], 'current')

    def test_no_selected_direction_keeps_legacy_candidate_detail(self):
        self.options_record()
        self.save_reviews()
        detail = self.detail()
        self.assertNotIn('当前审读依据：相关已选创作方向', detail['body'])
        self.assertNotIn('REVIEW_DIRECTION_', detail['body'])
        self.assertEqual(detail['actions'], ['review', 'adopt'])
        self.assert_reports_current()

    def test_disabled_supervision_displays_basis_without_claiming_a_hard_gate(self):
        root = self.root / 'legacy-book'
        store = ProjectStore(root)
        store.initialize(name='未启用监督的旧作品')
        document = store.create_document(kind='chapter', slug='one', title='第一章', content='Old prose.', state='accepted')
        candidate = store.create_version(document.id, content='Kept prose.', expected_version_id=document.active_version_id)
        choice = store.save_decision_options(slug='ending', title='结局', topic={'scope': 'work', 'document_id': None, 'key': 'ending'},
            options=self.options, sources=[], request='比较三方案。', expected_version_id=0)
        store.select_decision_option(choice['slug'], options_version_number=choice['options_version_number'], option_id='B',
                                     expected_version_id=choice['version_id'])
        self.assertIsNone(supervision_policy(store))
        detail = entry(make_config(root), 'pending', f'candidate:one:{candidate.version_number}')
        self.assertIn('当前审读依据：相关已选创作方向', detail['body'])
        self.assertIn('未启用章节监督策略', detail['body'])
        self.assertIn('不新增硬性采用门槛', detail['body'])
        self.assertNotIn('不能绕过 CLI 采用校验', detail['body'])
        self.assertEqual(detail['actions'], ['review', 'adopt'])
        self.assertEqual(store.accept_version(document.id, candidate.version_number).content, candidate.content)


if __name__ == '__main__':
    unittest.main()
