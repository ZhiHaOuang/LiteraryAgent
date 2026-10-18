"""Generation receipts describe captured inputs, never inferred complete dependencies."""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from lg_cli.generation_sources import bind_generation_sources, make_receipt, text_sha
from lg_cli.project_store import ProjectStore
from lg_cli.run_store import RunStore
from lg_cli.story_context import render_story_context, render_story_context_with_sources
from lg_cli.story_workflow import StoryWorkflowService

from tests.helpers import make_config
from tests.test_story_workflow import StoryAdapter


class CapturingAdapter(StoryAdapter):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.inputs = []

    def run(self, **kwargs):
        self.inputs.append(kwargs.copy())
        return super().run(**kwargs)


class StoryGenerationSourcesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = ProjectStore(self.root)
        self.project.initialize(name='Host captured generation inputs')
        self.document = self.project.create_document(kind='chapter', slug='chapter', title='Chapter',
            content='Author prefix.\nSELECTED\nAuthor suffix.')
        self.scene = self.project.create_scene(slug='gate', title='Gate', location='Author location')
        self.project.set_scene_plan('gate', 'Author approved plan.')
        self.project.approve_scene('gate')
        self.project.set_fact(category='world', key='gate', value='The gate closes at dusk.')
        self.config = make_config(self.root)
        self.runs = RunStore(self.root)

    def service(self, adapter=None):
        return StoryWorkflowService(self.config, project=self.project, adapter=adapter or CapturingAdapter(),
                                    runs=self.runs)

    def invoke(self, service, operation, **kwargs):
        if operation == 'revision':
            return service.revise_document('chapter', mode='light', **kwargs)
        if operation == 'scene-plan':
            return service.plan_scene('gate', **kwargs)
        if operation == 'scene-draft':
            return service.draft_scene('gate', **kwargs)
        return service.curate_document('chapter', **kwargs)

    def add_decision(self):
        value = self.project.save_decision_options(slug='ending', title='Ending direction',
            topic={'scope': 'work', 'document_id': None, 'key': 'ending'},
            options=[{'id': str(index), 'title': f'Ending {index}', 'content': f'PRIVATE_PLAN_CONTENT_{index}'}
                     for index in range(1, 4)], sources=[], request='Consider three endings.', expected_version_id=0)
        return self.project.select_decision_option('ending', options_version_number=value['options_version_number'],
            option_id='1', expected_version_id=value['version_id'])

    def assert_receipt(self, result, stage, adapter):
        manifest = self.runs.load(result.run_id)
        receipt = manifest['generation_sources']
        self.assertEqual(receipt['schema'], 'lg.generation-sources.v1')
        self.assertEqual(receipt['run_id'], result.run_id)
        self.assertEqual(receipt['stage_id'], stage)
        self.assertEqual(receipt['input'], {'prompt_sha256': text_sha(adapter.inputs[-1]['prompt']),
            'instructions_sha256': text_sha(adapter.inputs[-1]['instructions'])})
        metadata = self.runs.read_stage_metadata(result.run_id, stage)
        self.assertEqual(metadata['generation_sources'], receipt)
        self.assertEqual(metadata['generation_output_sha256'], text_sha(self.runs.read_stage(result.run_id, stage)))
        if result.version is not None:
            version = self.project.get_version(result.document.id, result.version.version_number)
            self.assertEqual(version.metadata['generation_sources'], receipt)
            self.assertEqual(version.metadata['generation_output_sha256'], text_sha(version.content))
        self.assertEqual(manifest['artifact_sha256'], text_sha(result.artifact_path.read_text()))
        return receipt

    def test_all_story_operations_share_frozen_candidate_stage_and_run_inputs(self):
        for operation in ('revision', 'scene-draft', 'bible-curate', 'scene-plan'):
            with self.subTest(operation=operation):
                adapter = CapturingAdapter()
                with patch.dict('os.environ', {'LG_WORKFLOW_GUIDANCE': 'LATEST_AUTHOR_GUIDANCE'}):
                    result = self.invoke(self.service(adapter), operation)
                self.assertTrue(result.ok, result.error)
                receipt = self.assert_receipt(result, operation, adapter)
                self.assertIn('LATEST_AUTHOR_GUIDANCE', adapter.inputs[0]['prompt'])
                self.assertNotIn('LATEST_AUTHOR_GUIDANCE', json.dumps(receipt))
                self.assertTrue(receipt['unknown_reasons'])
                self.assertIn(receipt['coverage'], ('partial', 'unknown'))

    def test_exact_older_selection_and_surroundings_are_not_full_document_delivery(self):
        source = self.project.create_version('chapter', content='Old prefix.\nSELECTED\nOld suffix.')
        self.project.create_version('chapter', content='Unselected newest candidate.')
        adapter = CapturingAdapter(revision_text='Changed passage.')
        result = self.service(adapter).revise_document('chapter', mode='light', source_version=source.version_number,
                                                       passage='SELECTED')
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.version.content, 'Old prefix.\nChanged passage.\nOld suffix.')
        receipt = self.assert_receipt(result, 'revision', adapter)
        observed = next(item for item in receipt['observations'] if item['tool'] == 'story_source_document')
        self.assertEqual(observed['refs'], [{'kind': 'document', 'document_id': self.document.id,
            'version_id': source.id, 'exposure': 'excerpt', 'excerpt_sha256': text_sha('SELECTED')}])
        self.assertNotIn('Unselected newest candidate.', adapter.inputs[0]['prompt'])
        self.assertNotEqual(result.version.metadata['generation_output_sha256'], text_sha('Changed passage.'))

    def test_context_observation_tracks_only_retained_sections_and_no_prose(self):
        selected = self.add_decision()
        self.project.create_document(kind='note', slug='long', title='Long', content='PRIVATE_BODY_' * 4000)
        snapshot = self.project.context_snapshot(query='long')
        text, observation = render_story_context_with_sources(snapshot)
        self.assertEqual(type(render_story_context(snapshot)), str)
        self.assertEqual(render_story_context(snapshot), text)
        self.assertEqual(observation['payload_sha256'], text_sha(text))
        self.assertIn('[document middle omitted]', text)
        self.assertTrue(any(ref['kind'] == 'document' and ref['exposure'] == 'excerpt'
                            for ref in observation['refs']))
        decision = next(ref for ref in observation['refs'] if ref['kind'] == 'creative_decision')
        self.assertEqual(decision['options_version_id'], selected['selection']['options_version_id'])
        encoded = json.dumps(observation)
        self.assertNotIn('PRIVATE_PLAN_CONTENT', encoded)
        self.assertNotIn('PRIVATE_BODY_', encoded)
        tiny, limited = render_story_context_with_sources(snapshot, max_chars=80)
        self.assertLessEqual(len(tiny), 80)
        self.assertEqual(limited['refs'], [])
        self.assertEqual(limited['coverage'], 'unknown')
        self.assertEqual(render_story_context_with_sources(snapshot, max_chars=0)[1]['refs'], [])

    def test_truncated_document_section_does_not_claim_unbounded_snapshot(self):
        document = self.project.create_document(kind='note', slug='huge-title', title='T' * 4000,
                                                content='PRIVATE_BODY_NOT_DELIVERED')
        snapshot = self.project.context_snapshot(query='huge-title')
        snapshot['documents'] = [self.project.get_document(document.id)]
        text, observation = render_story_context_with_sources(snapshot, max_chars=2000)
        self.assertNotIn('PRIVATE_BODY_NOT_DELIVERED', text)
        self.assertFalse(any(ref.get('document_id') == document.id for ref in observation['refs']))

    def test_direction_changed_during_model_does_not_rebind_captured_receipt(self):
        chosen = self.add_decision()
        project = self.project

        class ChangingAdapter(CapturingAdapter):
            def run(self, **kwargs):
                project.withdraw_decision('ending', expected_version_id=chosen['version_id'])
                return super().run(**kwargs)

        adapter = ChangingAdapter()
        result = self.service(adapter).revise_document('chapter', mode='light')
        self.assertTrue(result.ok, result.error)
        receipt = self.assert_receipt(result, 'revision', adapter)
        choice = next(ref for item in receipt['observations'] for ref in item['refs']
                      if ref['kind'] == 'creative_decision')
        self.assertEqual(choice['option_id'], '1')
        self.assertEqual(self.project.decision_snapshot('ending')['status'], 'withdrawn')

    def test_input_receipt_save_failure_prevents_model_and_domain_changes(self):
        adapter = CapturingAdapter()
        before = self.project.list_versions('chapter')
        with patch.object(self.runs, 'write_generation_sources', side_effect=OSError('PRIVATE_SAVE_FAILURE')):
            result = self.service(adapter).revise_document('chapter', mode='light')
        self.assertFalse(result.ok)
        self.assertEqual(adapter.calls, [])
        self.assertEqual(self.project.list_versions('chapter'), before)
        self.assertNotIn('PRIVATE_SAVE_FAILURE', self.runs.load(result.run_id)['error'])

    def test_changed_prepared_request_is_not_executed_with_original_receipt(self):
        adapter = CapturingAdapter()
        service = self.service(adapter)
        prepare = service._write_prompt

        def changed(*args):
            prepared, preview = prepare(*args)
            return replace(prepared, prompt=prepared.prompt + '\nChanged after capture.'), preview

        before = self.project.list_versions('chapter')
        with patch.object(service, '_write_prompt', side_effect=changed):
            result = service.revise_document('chapter', mode='light')
        self.assertFalse(result.ok)
        self.assertEqual(adapter.calls, [])
        self.assertEqual(self.project.list_versions('chapter'), before)

    def test_artifact_import_inherits_original_inputs_not_current_ambient_receipt(self):
        result = self.service().revise_document('chapter', mode='light')
        original = self.runs.load(result.run_id)['generation_sources']
        target = self.project.create_document(kind='chapter', slug='target', title='Target', content='Author source.')
        ambient = make_receipt(mode='live', run_id='unrelated-current-run', observations=[])
        with bind_generation_sources(ambient):
            imported = self.runs.adopt(result.run_id, slug='target', kind='chapter', title='Target')
        version = self.project.get_version(target.id, imported['version'])
        self.assertEqual(version.metadata['generation_sources'], original)
        self.assertEqual(version.metadata['generation_output_sha256'], text_sha(version.content))
        self.assertEqual(version.content, result.artifact_path.read_text())
        self.assertEqual(self.runs.adopt(result.run_id, slug='target', kind='chapter', title='Target'), imported)

    def test_new_reference_import_keeps_legacy_state_and_inherits_original_inputs(self):
        result = self.service().revise_document('chapter', mode='light')
        original = self.runs.load(result.run_id)['generation_sources']
        for slug, accept, state in (('reference', False, 'draft'), ('accepted-reference', True, 'accepted')):
            with self.subTest(state=state):
                imported = self.runs.adopt(result.run_id, slug=slug, kind='world', title='Reference', accept=accept)
                version = self.project.get_version(slug, imported['version'])
                self.assertEqual(version.state, state)
                self.assertEqual(version.metadata['generation_sources'], original)
                self.assertEqual(version.metadata['generation_output_sha256'], text_sha(version.content))

    def test_legacy_run_import_clears_ambient_sources_including_first_reference(self):
        artifact = self.root / 'legacy.md'
        artifact.write_text('Legacy artifact.\n')
        handle = self.runs.create(workflow='old', command='old', request='old', provider='local', model='local')
        self.runs.finalize(handle, status='completed', artifact_path=artifact)
        manifest = self.runs.load(handle.run_id)
        manifest.pop('artifact_sha256')
        handle.manifest_path.write_text(json.dumps(manifest))
        ambient = make_receipt(mode='live', run_id='unrelated-current-run', observations=[])
        with bind_generation_sources(ambient):
            for slug, kind in (('chapter', 'chapter'), ('legacy-ref', 'world')):
                imported = self.runs.adopt(handle.run_id, slug=slug, kind=kind, title='Legacy')
                version = self.project.get_version(slug, imported['version'])
                self.assertNotIn('generation_sources', version.metadata)
                self.assertNotIn('generation_output_sha256', version.metadata)

    def test_tampered_or_unverifiable_artifact_rejected_before_first_import(self):
        result = self.service().revise_document('chapter', mode='light')
        before = self.project.list_versions('chapter')
        original = result.artifact_path.read_text()
        result.artifact_path.write_text('Tampered PRIVATE_ARTIFACT_CONTENT')
        with self.assertRaisesRegex(ValueError, 'artifact changed'):
            self.runs.adopt(result.run_id, slug='chapter', kind='chapter', title='Chapter')
        self.assertEqual(self.project.list_versions('chapter'), before)
        result.artifact_path.write_text(original)
        result.artifact_path.write_bytes(original.replace('\n', '\r\n').encode('utf-8'))
        self.assertEqual(result.artifact_path.read_text(), original)
        with self.assertRaisesRegex(ValueError, 'artifact changed'):
            self.runs.adopt(result.run_id, slug='chapter', kind='chapter', title='Chapter')
        self.assertEqual(self.project.list_versions('chapter'), before)
        result.artifact_path.write_text(original)
        handle = self.runs.handle(result.run_id)
        manifest = self.runs.load(result.run_id)
        manifest.pop('artifact_sha256')
        handle.manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'verified output hash'):
            self.runs.adopt(result.run_id, slug='chapter', kind='chapter', title='Chapter')
        self.assertEqual(self.project.list_versions('chapter'), before)

    def test_import_rejects_mismatched_run_or_saved_stage_receipt(self):
        result = self.service().revise_document('chapter', mode='light')
        handle = self.runs.handle(result.run_id)
        original = handle.manifest_path.read_text()
        before = self.project.list_versions('chapter')
        for changed_field in ('run_id', 'unknown_reasons'):
            with self.subTest(changed_field=changed_field):
                manifest = json.loads(original)
                manifest['generation_sources'][changed_field] = 'other-run' if changed_field == 'run_id' else ['Changed']
                handle.manifest_path.write_text(json.dumps(manifest))
                with self.assertRaisesRegex(ValueError, 'receipt does not match'):
                    self.runs.adopt(result.run_id, slug='chapter', kind='chapter', title='Chapter')
                self.assertEqual(self.project.list_versions('chapter'), before)
                handle.manifest_path.write_text(original)

    def test_stage_save_failure_keeps_candidate_receipt_but_does_not_claim_transactionality(self):
        with patch.object(self.runs, 'write_stage', side_effect=OSError('Stage storage failed.')):
            with self.assertRaisesRegex(OSError, 'Stage storage failed'):
                self.service().revise_document('chapter', mode='light')
        candidate = self.project.list_versions('chapter')[0]
        receipt = candidate.metadata['generation_sources']
        self.assertEqual(self.runs.load(receipt['run_id'])['generation_sources'], receipt)
        self.assertEqual(candidate.metadata['generation_output_sha256'], text_sha(candidate.content))
        self.assertEqual(self.project.get_document('chapter').content, self.document.content)

    def test_dry_run_retains_inputs_without_model_or_candidate(self):
        adapter = CapturingAdapter()
        before = self.project.list_versions('chapter')
        result = self.service(adapter).revise_document('chapter', mode='light', dry_run=True)
        self.assertEqual(result.status, 'planned')
        self.assertEqual(adapter.calls, [])
        self.assertEqual(self.project.list_versions('chapter'), before)
        receipt = self.runs.load(result.run_id)['generation_sources']
        self.assertEqual(self.runs.read_stage_metadata(result.run_id, 'revision')['generation_sources'], receipt)
        with self.assertRaisesRegex(ValueError, 'Only completed'):
            self.runs.adopt(result.run_id, slug='chapter', kind='chapter', title='Chapter')


if __name__ == '__main__':
    unittest.main()
