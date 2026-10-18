"""Saved stage input is the input consumed by either execution adapter."""
import copy
import json
import os
import tempfile
import unittest
from dataclasses import replace
from importlib import resources
from pathlib import Path
from unittest.mock import patch

from lg_cli.anthropic_bridge import AnthropicBridge
from lg_cli.book_analysis import BookAnalysisStore, ChapterAnalysisService, source_spans
from lg_cli.core_adapter import CodexStageExecutor
from lg_cli.doctor import probe_provider
from lg_cli.execution_adapter import NativeStageAdapter, NativeStageExecutor
from lg_cli.init_project import init_workspace
from lg_cli.project_store import ProjectStoreError
from lg_cli.run_store import RunStore
from lg_cli.stage_input import prepare_stage_input
from lg_cli.story_index import GRAPH_INSTRUCTIONS
from lg_cli.story_workflow import StoryWorkflowService
from lg_cli.workflow_runner import WorkflowRunner
from tests.helpers import make_config, reference_card
from tests.test_anthropic_bridge import message_events
from tests import test_execution_adapter as fixtures


class StageInputPolicyTests(unittest.TestCase):
    def test_codex_rejects_blank_instructions_before_runtime_discovery(self):
        with tempfile.TemporaryDirectory() as raw, \
                patch('lg_cli.core_adapter.discover_core_commands', side_effect=AssertionError('Runtime discovery')):
            for instructions in ('', ' \n\t'):
                result = CodexStageExecutor().run(prompt='Task', instructions=instructions,
                                                  config=make_config(Path(raw)), mode='write')
                self.assertFalse(result.ok)
                self.assertIn('must not be empty', result.error)

    def test_schema_selection_and_explicit_empty_overrides(self):
        with patch.dict(os.environ, {'LG_WORKFLOW_GUIDANCE': 'ENV_GUIDANCE'}):
            for name in ('stage-output', 'scene-draft', 'revision', 'scene-plan', 'other'):
                with self.subTest(schema=name):
                    value = prepare_stage_input('Task', output_schema=Path(name + '.schema.json'),
                                                guidance='', instructions='')
                    self.assertEqual(value.instructions, '')
                    self.assertEqual(value.prompt, 'Task' + ('\n' + GRAPH_INSTRUCTIONS
                        if name in {'stage-output', 'scene-draft', 'revision'} else ''))
            value = prepare_stage_input('Task', instructions='HOST_INSTRUCTIONS')
            self.assertEqual(value.instructions, 'HOST_INSTRUCTIONS')
            self.assertEqual(value.prompt.count('ENV_GUIDANCE'), 1)


class SavedStageInputTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.NativeStageTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.config = self.fixture.root, self.fixture.config
        self.requests = self.fixture.requests
        self.runs = RunStore(self.root)

    def assert_saved(self, run_id, stage_id, request):
        folder = self.runs.handle(run_id).directory / 'stages'
        self.assertEqual((folder / f'{stage_id}.prompt.md').read_text(), request['input'][0]['content'])
        self.assertEqual((folder / f'{stage_id}.instructions.md').read_text(), request['instructions'])

    def test_raw_and_explicit_compatibility_calls_never_add_product_input(self):
        schema = Path(str(resources.files('lg_cli.resources').joinpath('revision.schema.json')))
        with patch.dict(os.environ, {'LG_WORKFLOW_GUIDANCE': 'MUST_NOT_APPEND'}), \
                patch('lg_cli.stage_input.prepare_stage_input', side_effect=AssertionError('Implicit preparation')):
            for adapter in (NativeStageExecutor(), NativeStageAdapter()):
                for instructions in ('HOST_INSTRUCTIONS', ''):
                    result = adapter.run(prompt='EXACT_INPUT', instructions=instructions,
                        config=self.config, mode='write', output_schema=schema)
                    self.assertTrue(result.ok, result.error)
                    self.assertEqual(self.requests[-1]['instructions'], instructions)
                    self.assertEqual(self.requests[-1]['input'][0]['content'], 'EXACT_INPUT')

    def test_workflow_saves_final_input_before_late_guidance_change(self):
        original = NativeStageAdapter.run

        def change_after_save(adapter, **kwargs):
            os.environ['LG_WORKFLOW_GUIDANCE'] = 'LATE_GUIDANCE'
            return original(adapter, **kwargs)

        with patch.dict(os.environ, {'LG_WORKFLOW_GUIDANCE': 'INITIAL_GUIDANCE'}), \
                patch.object(NativeStageAdapter, 'run', change_after_save):
            result = WorkflowRunner(self.config).run('world', '只规划世界规则')
        self.assertTrue(result.ok, result.error)
        stages = self.runs.load(result.run_id)['completed_stages']
        self.assertEqual(len(stages), len(self.requests))
        for stage, request in zip(stages, self.requests):
            self.assert_saved(result.run_id, stage, request)
            self.assertLessEqual(request['input'][0]['content'].count(GRAPH_INSTRUCTIONS), 1)
        self.assertIn('INITIAL_GUIDANCE', self.requests[0]['input'][0]['content'])
        self.assertNotIn('LATE_GUIDANCE', self.requests[0]['input'][0]['content'])

    def test_revision_and_dry_run_save_same_prepared_contract(self):
        service = StoryWorkflowService(self.config)
        with patch.dict(os.environ, {'LG_WORKFLOW_GUIDANCE': 'AUTHOR_GUIDANCE'}):
            result = service.revise_document('one', mode='light', request='强化动机')
            preview = service.revise_document('one', mode='light', request='强化动机', dry_run=True)
        self.assertTrue(result.ok, result.error)
        self.assertTrue(preview.ok, preview.error)
        self.assertEqual(len(self.requests), 1)
        self.assert_saved(result.run_id, 'revision', self.requests[0])
        preview_path = self.runs.handle(preview.run_id).directory / 'stages/revision.prompt.md'
        self.assertEqual(preview_path.read_text().count('AUTHOR_GUIDANCE'), 1)
        self.assertEqual(preview_path.read_text().count(GRAPH_INSTRUCTIONS), 1)
        self.assertNotIn(GRAPH_INSTRUCTIONS, preview.text)
        self.assertNotIn('AUTHOR_GUIDANCE', preview.text)
        self.assertEqual(self.fixture.project.get_document('one').active_version_id,
                         self.fixture.original.active_version_id)

    def test_analysis_repair_saves_each_actual_input_without_duplicate_guidance(self):
        span = next(iter(source_spans(BookAnalysisStore(self.fixture.project).snapshot('one'))))
        valid = {'summary': 'Source analysis', 'entries': [{'instance_card': reference_card('Worldview'),
            'key': 'gate', 'label': 'Gate', 'analysis': 'Source supports this rule.',
            'evidence': [{'span_id': span}]}], 'findings': []}
        invalid = copy.deepcopy(valid)
        invalid['entries'][0]['evidence'][0]['span_id'] = 'invented-span'
        with patch.dict(os.environ, {'LG_WORKFLOW_GUIDANCE': 'AUTHOR_GUIDANCE'}), \
                patch.object(fixtures, 'specimen', side_effect=[invalid, valid]):
            result = ChapterAnalysisService(self.config).analyze('one', categories=['Worldview'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(self.requests), 2)
        for stage, request in zip(('Worldview', 'Worldview-repair'), self.requests):
            self.assert_saved(result['run_id'], stage, request)
            self.assertEqual(request['input'][0]['content'].count('AUTHOR_GUIDANCE'), 1)
        self.assertIn('## Evidence repair', self.requests[1]['input'][0]['content'])

    def test_analysis_budget_counts_final_guidance_before_model_call(self):
        with patch.dict(os.environ, {'LG_WORKFLOW_GUIDANCE': 'x' * self.config.max_stage_context_chars}):
            with self.assertRaisesRegex(ProjectStoreError, 'context exceeds'):
                ChapterAnalysisService(self.config).analyze('one', categories=['Worldview'])
        self.assertEqual(self.requests, [])

    def test_instruction_change_invalidates_completed_prefix(self):
        self.fixture.fail_at = 3
        runner = WorkflowRunner(self.config)
        failed = runner.run('outline', '规划第一卷')
        self.assertFalse(failed.ok)
        self.assertEqual(len(self.runs.load(failed.run_id)['completed_stages']), 2)
        self.fixture.fail_at = None

        def changed(*args, **kwargs):
            value = prepare_stage_input(*args, **kwargs)
            return replace(value, instructions=value.instructions + '\nNEW_POLICY')

        with patch('lg_cli.stage_input.prepare_stage_input', side_effect=changed):
            result = runner.resume(failed.run_id)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(self.requests), 10)
        for stage, request in zip(self.runs.load(result.run_id)['completed_stages'], self.requests[3:]):
            self.assert_saved(result.run_id, stage, request)

    def test_restored_prefix_has_current_input_artifacts(self):
        self.fixture.fail_at = 3
        runner = WorkflowRunner(self.config)
        failed = runner.run('outline', '规划第一卷')
        self.fixture.fail_at = None
        result = runner.resume(failed.run_id)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(self.requests), 8)
        stages = self.runs.load(result.run_id)['completed_stages']
        for stage, request in zip(stages, self.requests[:2] + self.requests[3:]):
            self.assert_saved(result.run_id, stage, request)

    def test_doctor_uses_only_connectivity_instructions(self):
        with patch.dict(os.environ, {'LG_WORKFLOW_GUIDANCE': 'MUST_NOT_APPEND'}), \
                patch('lg_cli.stage_input.prepare_stage_input', side_effect=AssertionError('Product instructions loaded')):
            self.assertEqual(probe_provider(self.config).status, 'PASS')
        self.assertNotIn('MUST_NOT_APPEND', json.dumps(self.requests))
        self.assertIn('connectivity check', self.requests[0]['instructions'])


@unittest.skipUnless(os.environ.get('LG_TEST_RUNTIME_MANIFEST'), 'set LG_TEST_RUNTIME_MANIFEST for pinned Core')
class CodexStageInputTests(unittest.TestCase):
    def test_real_core_receives_custom_instructions_from_private_file(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            init_workspace(root)
            config = replace(make_config(root), provider='stepfun', protocol='anthropic',
                default_model='step-3.5-flash', timeout_seconds=60,
                runtime_manifest=Path(os.environ['LG_TEST_RUNTIME_MANIFEST']))
            payloads = []

            def response(payload):
                payloads.append(payload)
                yield from message_events(delta={'type': 'text_delta', 'text': 'LG_READY'})

            with patch.dict(os.environ, {'LG_WORKFLOW_GUIDANCE': 'MUST_NOT_APPEND'}), \
                    patch('lg_cli.provider_transport.provider_bridge',
                          side_effect=lambda _: AnthropicBridge(config, stream_factory=response)), \
                    patch('lg_cli.stage_input.prepare_stage_input', side_effect=AssertionError('Implicit preparation')):
                result = CodexStageExecutor().run(prompt='EXACT_INPUT', instructions='HOST_STAGE_INSTRUCTIONS',
                                                  config=config, mode='write')
            self.assertTrue(result.ok, result.error)
            sent = json.dumps(payloads)
            self.assertIn('HOST_STAGE_INSTRUCTIONS', sent)
            self.assertIn('EXACT_INPUT', sent)
            self.assertNotIn('MUST_NOT_APPEND', sent)
            self.assertNotIn('You are LiteraryGiant (LG)', sent)
            self.assertNotIn('HOST_STAGE_INSTRUCTIONS', ' '.join(result.command))
            files = list((root / '.literarygiant/tmp').glob('core-instructions-*.txt'))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_text(), 'HOST_STAGE_INSTRUCTIONS')
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
