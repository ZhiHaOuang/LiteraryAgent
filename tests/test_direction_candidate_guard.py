"""Direction identity remains a precondition through review, restoration and revision."""
import tempfile
import unittest
from pathlib import Path

from lg_cli.literary_tasks import start, advance
from lg_cli.project_store import ProjectStore, ProjectStoreError
from lg_cli.story_workflow import StoryWorkflowService
from tests.helpers import make_config
from tests.test_literary_tasks import TaskAdapter
from tests.test_story_workflow import StoryAdapter


class DirectionCandidateGuardTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.root=Path(directory.name).resolve();self.store=ProjectStore(self.root);self.store.initialize()
        self.document=self.store.create_document(kind='chapter',slug='one',title='One',content='Original.',state='accepted')
        self.config=make_config(self.root)
        waiting=start(self.config,{'strategy':'chapter','document':'one','request':'Write the next confrontation.',
            'plot_disagreement':True},adapter=TaskAdapter())
        decision=waiting['decision']
        self.selection=self.store.select_decision_option(decision['slug'],
            options_version_number=decision['optionsVersionNumber'],option_id='2',
            expected_version_id=decision['expectedVersionId'])
        result=advance(self.config,waiting['runId'],adapter=TaskAdapter())
        self.assertEqual(result['status'],'awaiting_review',result)
        self.candidate=self.store.get_version('one',result['candidate']['versionNumber'])

    def withdraw(self):
        self.store.withdraw_decision(self.selection['slug'],expected_version_id=self.selection['version_id'])

    def test_changed_direction_refuses_even_without_supervision_policy(self):
        self.withdraw()
        with self.assertRaisesRegex(ProjectStoreError,'direction changed'):
            self.store.accept_version('one',self.candidate.version_number,expected_version_id=self.document.active_version_id)
        self.assertEqual(self.store.get_document('one').content,'Original.')

    def test_unchanged_direction_allows_author_acceptance(self):
        actual=self.store.accept_version('one',self.candidate.version_number,expected_version_id=self.document.active_version_id)
        self.assertEqual(actual.active_version_id,self.candidate.id)

    def test_restore_does_not_erase_required_direction(self):
        restored=self.store.restore_version('one',self.candidate.version_number)
        self.assertEqual(restored.metadata['required_decision'],self.candidate.metadata['required_decision'])
        self.withdraw()
        with self.assertRaisesRegex(ProjectStoreError,'direction changed'):
            self.store.accept_version('one',restored.version_number)

    def test_revision_of_selected_candidate_keeps_direction_precondition(self):
        revised=StoryWorkflowService(self.config,adapter=StoryAdapter()).revise_document('one',mode='light',
            source_version=self.candidate.version_number)
        self.assertTrue(revised.ok,revised.error)
        self.assertEqual(revised.version.metadata['required_decision'],self.candidate.metadata['required_decision'])
        self.withdraw()
        with self.assertRaisesRegex(ProjectStoreError,'direction changed'):
            self.store.accept_version('one',revised.version.version_number)
