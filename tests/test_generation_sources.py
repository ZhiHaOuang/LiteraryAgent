import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from lg_cli.generation_sources import (bind_generation_sources, canonical_sha, inherited_receipt,
    make_observation, make_receipt, text_sha)
from lg_cli.project_store import ProjectStore


class GenerationSourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = ProjectStore(Path(temporary.name))
        self.store.initialize(name='Captured sources')
        self.chapter = self.store.create_document(kind='chapter', slug='one', title='One',
            content='Accepted prose', state='accepted')
        self.payload = {'documents': [{'id': self.chapter.id, 'active_version_id': self.chapter.active_version_id}],
            'creative_decisions': [{'document_id': 9, 'topic': {'scope': 'work'}, 'status': 'selected',
                'selection': {'options_version_id': 11, 'option_id': 'A', 'content': 'Original direction'},
                'source_status': 'current', 'source_details': []}]}
        self.receipt = make_receipt(mode='live', run_id='run-a',
            observations=[make_observation('project_memory', self.payload)],
            unknown_reasons=['prior_history_unobserved'])

    def test_receipt_is_frozen_without_copying_source_content(self):
        observation = make_observation('project_memory', self.payload)
        receipt = make_receipt(mode='live', run_id='a', observations=[observation])
        observation['refs'].clear()
        self.payload['creative_decisions'][0]['selection']['option_id'] = 'B'
        self.assertEqual(receipt['observations'][0]['refs'][1]['option_id'], 'A')
        self.assertNotIn('Original direction', json.dumps(receipt))
        self.assertEqual(receipt['coverage'], 'partial')
        self.assertNotEqual(receipt['observations'][0]['payload_sha256'], canonical_sha(self.payload))

    def test_index_is_not_content_and_unknown_text_is_not_parsed(self):
        observation = make_observation('project_memory', self.payload)
        self.assertEqual(observation['refs'][0]['exposure'], 'index')
        opaque = make_observation('book_entry', '<document id="1" version="2">invented</document>')
        self.assertEqual(opaque['refs'], [])
        self.assertEqual(opaque['coverage'], 'unknown')
        self.assertEqual(make_receipt(mode='live', run_id='empty', observations=[])['coverage'], 'unknown')

    def test_store_binds_exact_candidate_content_and_does_not_activate(self):
        with bind_generation_sources(self.receipt):
            candidate = self.store.create_version('one', content='New prose')
        self.assertEqual(candidate.metadata['generation_sources'], self.receipt)
        self.assertEqual(candidate.metadata['generation_output_sha256'], text_sha('New prose'))
        self.assertEqual(self.store.get_document('one'), self.chapter)
        self.receipt['observations'].clear()
        self.assertTrue(candidate.metadata['generation_sources']['observations'])

    def test_initial_candidate_version_is_captured(self):
        with bind_generation_sources(self.receipt):
            document = self.store.create_document(kind='chapter', slug='two', title='Two',
                content='Initial candidate', state='candidate')
        version = self.store.get_version(document.id, 1)
        self.assertEqual(version.metadata['generation_sources'], self.receipt)
        self.assertNotIn('generation_sources', document.metadata)

    def test_explicit_host_import_preserves_receipt_without_changing_state(self):
        with bind_generation_sources(self.receipt, include_non_candidate=True):
            document = self.store.create_document(kind='outline', slug='imported', title='Imported',
                content='Existing plan', state='draft')
        version = self.store.get_version(document.id, 1)
        self.assertEqual(version.state, 'draft')
        self.assertEqual(version.metadata['generation_sources'], self.receipt)
        with bind_generation_sources(self.receipt):
            plain = self.store.create_document(kind='note', slug='next', title='Next', content='Draft')
        self.assertNotIn('generation_sources', self.store.get_version(plain.id, 1).metadata)

    def test_legacy_and_non_candidate_writes_keep_old_metadata(self):
        self.assertEqual(self.store.create_version('one', content='Legacy').metadata, {})
        with bind_generation_sources(self.receipt):
            document = self.store.create_document(kind='note', slug='note', title='Note', content='Draft')
        self.assertEqual(self.store.get_version(document.id, 1).metadata, {})

    def test_reserved_metadata_cannot_be_self_reported(self):
        before = self.store.list_versions('one')
        for key in ('generation_sources', 'generation_output_sha256'):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, 'reserved'):
                    self.store.create_version('one', content='Forged', metadata={key: self.receipt})
                with self.assertRaisesRegex(ValueError, 'reserved'):
                    self.store.create_document(kind='chapter', slug='fake', title='Fake', metadata={key: self.receipt})
                with self.assertRaisesRegex(ValueError, 'reserved'):
                    self.store.update_document('one', metadata={key: self.receipt})
        self.assertEqual(self.store.list_versions('one'), before)

    def test_restore_inherits_original_receipt_and_suppresses_current(self):
        with bind_generation_sources(self.receipt):
            candidate = self.store.create_version('one', content='A candidate')
        other = make_receipt(mode='live', run_id='new', observations=[])
        with bind_generation_sources(other):
            restored = self.store.restore_version('one', candidate.version_number)
            legacy = self.store.restore_version('one', 1)
        self.assertEqual(restored.metadata['generation_sources'], self.receipt)
        self.assertNotIn('generation_sources', legacy.metadata)

    def test_inherited_output_mismatch_is_rejected(self):
        metadata = {'generation_sources': self.receipt, 'generation_output_sha256': text_sha('Original')}
        with self.assertRaisesRegex(ValueError, 'does not match'):
            inherited_receipt(metadata, 'Changed')

    def test_explicit_clear_and_exception_restore_context(self):
        with bind_generation_sources(self.receipt):
            with self.assertRaises(RuntimeError), bind_generation_sources(None):
                plain = self.store.create_version('one', content='Plain')
                raise RuntimeError('Stop')
            captured = self.store.create_version('one', content='Captured')
        final = self.store.create_version('one', content='Final')
        self.assertNotIn('generation_sources', plain.metadata)
        self.assertEqual(captured.metadata['generation_sources'], self.receipt)
        self.assertNotIn('generation_sources', final.metadata)

    def test_task_local_context_does_not_leak_between_parallel_runs(self):
        async def run():
            ready = asyncio.Event()

            async def captured():
                with bind_generation_sources(self.receipt):
                    ready.set()
                    await asyncio.sleep(0)
                    return self.store.create_version('one', content='Captured')

            async def plain():
                await ready.wait()
                return self.store.create_version('one', content='Plain')

            return await asyncio.gather(captured(), plain())
        captured, plain = asyncio.run(run())
        self.assertEqual(captured.metadata['generation_sources'], self.receipt)
        self.assertNotIn('generation_sources', plain.metadata)

    def test_final_prepared_input_digest_has_no_prompt_content(self):
        value = make_receipt(mode='story', run_id='run', stage_id='draft', observations=[],
            prompt='Final prompt', instructions='Final instructions')
        self.assertEqual(value['input'], {'prompt_sha256': text_sha('Final prompt'),
            'instructions_sha256': text_sha('Final instructions')})
        self.assertNotIn('Final prompt', json.dumps(value))

    def test_invalid_receipt_is_rejected_before_binding(self):
        for value in ({}, dict(self.receipt, coverage='complete'), ['fake']):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'Invalid'):
                with bind_generation_sources(value):
                    self.fail('Invalid receipt bound')

    def test_library_distinguishes_capture_from_current_review_without_mutation(self):
        from lg_cli.workspace_library import entry
        from tests.helpers import make_config
        with bind_generation_sources(self.receipt):
            candidate = self.store.create_version('one', content='Candidate')
        before = self.store.list_versions('one')
        detail = entry(make_config(self.store.workspace), 'pending', f'candidate:one:{candidate.version_number}')
        self.assertIn('生成时捕获的输入', detail['body'])
        self.assertIn('部分覆盖', detail['body'])
        self.assertIn('run-a', detail['body'])
        self.assertIn('不授予采用权限', detail['body'])
        self.assertEqual(detail['actions'], ['review', 'adopt'])
        self.assertEqual(self.store.list_versions('one'), before)

    def test_library_marks_legacy_unknown_without_binding_current_context(self):
        from lg_cli.workspace_library import entry
        from tests.helpers import make_config
        candidate = self.store.create_version('one', content='Legacy candidate')
        with bind_generation_sources(self.receipt):
            detail = entry(make_config(self.store.workspace), 'pending', f'candidate:one:{candidate.version_number}')
        self.assertIn('此候选的生成来源未知', detail['body'])
        self.assertNotIn('run-a', detail['body'])

    def test_library_source_summary_is_bounded_without_changing_stored_receipt(self):
        from lg_cli.workspace_library import _generation_source_section
        observations = [make_observation('project_memory', self.payload) for _ in range(100)]
        receipt = make_receipt(mode='live', run_id='many-reads', observations=observations)
        with bind_generation_sources(receipt):
            candidate = self.store.create_version('one', content='Candidate')
        summary = _generation_source_section(candidate)
        self.assertLess(len(summary), 4100)
        self.assertIn('共 100 条输入观察', summary)
        self.assertEqual(self.store.get_version('one', candidate.version_number).metadata['generation_sources'], receipt)
