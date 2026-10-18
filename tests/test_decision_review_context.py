import json
import tempfile
import unittest
from pathlib import Path

from lg_cli.project_store import ProjectStore, ProjectStoreError


class DecisionReviewContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ProjectStore(Path(self.tmp.name))
        self.store.initialize(name='Direction review context')
        self.part = self.store.create_document(kind='part', slug='part', title='Part one', content='Part premise')
        self.chapter = self.store.create_document(kind='chapter', slug='one', title='One',
            parent=self.part.id, content='Original prose.', state='accepted')
        self.other = self.store.create_document(kind='chapter', slug='two', title='Two',
            content='Unrelated prose.', state='accepted')
        self.options = [{'id': str(i), 'title': 'Plan ' + str(i), 'content': text} for i, text in enumerate(
            ('The protagonist stays to investigate.', 'The protagonist escapes upriver.',
             'The protagonist negotiates with the rival.'))]

    def save(self, scope='chapter', *, slug='choice', target=None, key='ending'):
        document = target or {'chapter': self.chapter, 'part': self.part, 'work': None}[scope]
        return self.store.save_decision_options(slug=slug, title=slug,
            topic={'scope': scope, 'document_id': document.id if document else None, 'key': key},
            options=self.options, sources=[{'document_id': document.id, 'version_id': document.active_version_id}]
                if document else [], request='Compare these directions.', expected_version_id=0)

    def select(self, value, option='0'):
        return self.store.select_decision_option(value['slug'], options_version_number=value['options_version_number'],
            option_id=option, expected_version_id=value['version_id'])

    def test_empty_unselected_and_withdrawn_have_no_formal_plan_evidence(self):
        self.assertEqual(self.store.decision_review_context('one'), [])
        value = self.save()
        self.assertEqual(self.store.decision_review_context('one'), [])
        selected = self.select(value)
        self.assertEqual(len(self.store.decision_review_context('one')), 1)
        self.store.withdraw_decision('choice', expected_version_id=selected['version_id'])
        self.assertEqual(self.store.decision_review_context('one'), [])
        self.assertEqual(len(self.store.list_versions('choice')), 3)

    def test_relevant_work_part_and_chapter_plans_only(self):
        own = self.select(self.save())
        global_plan = self.select(self.save('work', slug='global'))
        part_plan = self.select(self.save('part', slug='part-plan'))
        other_plan = self.select(self.save(target=self.other, slug='other'))
        self.assertEqual([item['document_id'] for item in self.store.decision_review_context('one')],
            [own['document_id'], global_plan['document_id'], part_plan['document_id']])
        self.assertEqual([item['document_id'] for item in self.store.decision_review_context('two')],
            [global_plan['document_id'], other_plan['document_id']])

    def test_option_regeneration_does_not_change_review_evidence(self):
        selected = self.select(self.save())
        before = self.store.decision_review_context('one')
        self.store.save_decision_options(slug='choice', title='choice', topic=selected['topic'],
            options=[dict(item, content=item['content'] + ' A new possibility.') for item in self.options],
            sources=selected['sources'], request='New alternatives only.', expected_version_id=selected['version_id'])
        self.assertEqual(self.store.decision_review_context('one'), before)
        self.assertNotIn('New alternatives only', json.dumps(before))
        self.assertNotIn('version_id', before[0])
        self.assertNotIn('options', before[0])

    def test_changed_selection_changes_evidence_without_mutating_story(self):
        before_doc = self.store.get_document('one')
        selected = self.select(self.save())
        before = self.store.decision_review_context('one')
        self.select(selected, option='1')
        after = self.store.decision_review_context('one')
        self.assertNotEqual(after, before)
        self.assertEqual(after[0]['selection']['content'], self.options[1]['content'])
        self.assertNotIn(self.options[0]['content'], json.dumps(after))
        self.assertEqual(self.store.get_document('one'), before_doc)

    def test_actual_and_projected_source_status_match_after_adoption(self):
        self.select(self.save())
        candidate = self.store.create_version('one', content='The revised prose.')
        self.assertEqual(self.store.decision_review_context('one')[0]['source_status'], 'current')
        projected = self.store.decision_review_context('one',
            projected_versions={self.chapter.id: {'version_id': candidate.id, 'state': 'accepted'}})
        self.assertEqual(projected[0]['source_status'], 'stale')
        self.assertEqual(projected[0]['source_details'][0]['active_version_id'], candidate.id)
        self.store.accept_version('one', candidate.version_number)
        self.assertEqual(self.store.decision_review_context('one'), projected)

    def test_initial_candidate_source_state_is_projected_to_accepted(self):
        source = self.store.create_document(kind='chapter', slug='new', title='New', content='First draft', state='candidate')
        self.select(self.save(target=source))
        version = self.store.get_version('new', 1)
        projected = self.store.decision_review_context('new',
            projected_versions={source.id: {'version_id': version.id, 'state': 'accepted'}})
        self.assertEqual(projected[0]['source_status'], 'current')
        self.assertEqual(projected[0]['source_details'][0]['version_state'], 'accepted')
        self.store.accept_version('new', 1)
        self.assertEqual(self.store.decision_review_context('new'), projected)

    def test_changed_declared_basis_changes_status_but_keeps_author_choice(self):
        self.select(self.save('part'))
        before = self.store.decision_review_context('one')
        self.store.edit_active_document(self.part.id, content='New premise',
            expected_version_id=self.part.active_version_id, reason='Author changed premise')
        after = self.store.decision_review_context('one')
        self.assertEqual(after[0]['selection'], before[0]['selection'])
        self.assertEqual((before[0]['source_status'], after[0]['source_status']), ('current', 'stale'))

    def test_wrong_chapter_and_foreign_or_invalid_projection_refused(self):
        candidate = self.store.create_version('one', content='Candidate')
        for value in ([], {self.other.id: {'version_id': self.other.active_version_id, 'state': 'accepted'}},
                      {self.chapter.id: {'version_id': self.other.active_version_id, 'state': 'accepted'}},
                      {self.chapter.id: {'version_id': candidate.id, 'state': 'candidate'}},
                      {self.chapter.id: {'version_id': True, 'state': 'accepted'}},
                      {self.chapter.id: {'version_id': candidate.id, 'state': 'accepted', 'force': True}}):
            with self.subTest(value=value), self.assertRaises(ProjectStoreError):
                self.store.decision_review_context('one', projected_versions=value)
        with self.assertRaises(ProjectStoreError):
            self.store.decision_review_context(self.part.id)


if __name__ == '__main__':
    unittest.main()
