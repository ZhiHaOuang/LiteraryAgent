"""All Story candidates retain their actual edit basis, including historical sources."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from lg_cli.backend import Backend
from lg_cli.project_store import ProjectStore, ProjectStoreError
from lg_cli.story_workflow import StoryWorkflowService
from tests.helpers import make_config
from tests.test_backend import FakeAgent
from tests.test_story_workflow import StoryAdapter


class CandidateBasisTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.store = ProjectStore(self.root)
        self.store.initialize(name='Synthetic candidate bases')
        self.document = self.store.create_document(kind='chapter', slug='one', title='One',
            content='Before\nSELECTED\nAfter', state='accepted')

    def service(self, adapter=None):
        return StoryWorkflowService(make_config(self.root), project=self.store, adapter=adapter or StoryAdapter())

    def author_edit(self):
        current = self.store.get_document('one')
        return self.store.edit_active_document('one', content='Author changed outside\nSELECTED\nAfter',
            expected_version_id=current.active_version_id, reason='Synthetic concurrent author save')

    def test_generation_concurrent_author_edit_preserves_candidate_but_refuses_adoption(self):
        parent = self
        class ConcurrentAdapter(StoryAdapter):
            def run(self, **kwargs):
                parent.author_edit()
                return super().run(**kwargs)
        result = self.service(ConcurrentAdapter()).revise_document('one', mode='light', passage='SELECTED')
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.version.metadata.get('base_version_id'), self.document.active_version_id)
        current = self.store.get_document('one')
        with self.assertRaisesRegex(ProjectStoreError, 'older active version'):
            self.store.accept_version('one', result.version.version_number, expected_version_id=current.active_version_id)
        self.assertIn('Author changed outside', self.store.get_document('one').content)

    def test_current_revision_can_be_accepted_and_keeps_outside_selection(self):
        result = self.service().revise_document('one', mode='light', passage='SELECTED')
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.version.metadata.get('base_version_id'), self.document.active_version_id)
        adopted = self.store.accept_version('one', result.version.version_number,
            expected_version_id=self.document.active_version_id)
        self.assertEqual(adopted.content, 'Before\nRevised candidate.\nAfter')

    def test_revising_historical_candidate_inherits_old_basis_instead_of_current_head(self):
        candidate = self.store.create_version('one', content='Before\nSELECTED\nAfter',
            expected_version_id=self.document.active_version_id)
        current = self.author_edit()
        result = self.service().revise_document('one', mode='light', source_version=candidate.version_number)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.version.metadata.get('base_version_id'), self.document.active_version_id)
        with self.assertRaisesRegex(ProjectStoreError, 'older active version'):
            self.store.accept_version('one', result.version.version_number, expected_version_id=current.id)

    def test_revising_historical_accepted_version_does_not_rebind_to_current_head(self):
        current = self.author_edit()
        result = self.service().revise_document('one', mode='light', source_version=1)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.version.metadata.get('base_version_id'), self.document.active_version_id)
        with self.assertRaisesRegex(ProjectStoreError, 'older active version'):
            self.store.accept_version('one', result.version.version_number, expected_version_id=current.id)

    def test_revising_unbound_legacy_candidate_retains_source_as_basis(self):
        legacy = self.store.create_version('one', content='Legacy selected source')
        result = self.service().revise_document('one', mode='light', source_version=legacy.version_number)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.version.metadata.get('base_version_id'), legacy.id)
        with self.assertRaisesRegex(ProjectStoreError, 'older active version'):
            self.store.accept_version('one', result.version.version_number)

    def test_scene_with_empty_text_keeps_pre_generation_basis(self):
        scene = self.store.create_scene(slug='empty', title='Empty', chapter='one')
        self.assertEqual(scene.document.content, '')
        self.store.set_scene_plan('empty', 'A concrete approved plan.')
        self.store.approve_scene('empty')
        parent = self
        class SceneAdapter(StoryAdapter):
            def run(self, **kwargs):
                parent.store.create_version('empty', content='Concurrent author scene', activate=True, state='accepted')
                return super().run(**kwargs)
        result = self.service(SceneAdapter()).draft_scene('empty')
        self.assertTrue(result.ok, result.error)
        self.assertIn('base_version_id', result.version.metadata)
        self.assertEqual(result.version.metadata['base_version_id'], scene.document.active_version_id)
        with self.assertRaisesRegex(ProjectStoreError, 'older active version'):
            self.store.accept_version('empty', result.version.version_number)

    def test_explicit_empty_basis_is_not_an_unbound_legacy_candidate(self):
        candidate = self.store.create_version('one', content='Based on no active text', metadata={'base_version_id': None})
        with self.assertRaisesRegex(ProjectStoreError, 'older active version'):
            self.store.accept_version('one', candidate.version_number)

    def test_foreign_and_invalid_explicit_bases_are_rejected(self):
        other = self.store.create_document(kind='chapter', slug='two', title='Two', content='Other')
        for invalid in (other.active_version_id, -1, True, '1'):
            with self.subTest(base=invalid):
                with self.assertRaises(ProjectStoreError):
                    self.store.create_version('one', content='Invalid basis', metadata={'base_version_id': invalid})

    def test_backend_fresh_approval_does_not_make_stale_story_basis_valid(self):
        result = self.service().revise_document('one', mode='light', passage='SELECTED')
        self.author_edit()
        async def execute():
            events = []
            backend = Backend(make_config(self.root), events.append, agent_factory=FakeAgent)
            try:
                key = (await backend.dispatch('session/create', {}))['sessionId']
                request = await backend.dispatch('creative/apply', {'sessionId': key, 'action': 'acceptVersion',
                    'document': 'one', 'version': result.version.version_number})
                for _ in range(500):
                    if backend.approvals.pending:
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(backend.approvals.pending)
                backend.approvals.resolve(next(iter(backend.approvals.pending)), {'decision': 'accept'})
                await backend.tasks[request['runId']][1]
                completed = next(e['params'] for e in reversed(events) if e['method'] == 'run/completed')
                self.assertEqual(completed['status'], 'failed')
                self.assertIn('older active version', completed['error'])
            finally:
                await backend.close()
        asyncio.run(execute())
        self.assertIn('Author changed outside', self.store.get_document('one').content)
