"""Story preparation/preview failures settle runs before best-effort diagnostics."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lg_cli.events import EventType
from lg_cli.project_store import ProjectStore
from lg_cli.run_store import RunStore
from lg_cli.story_workflow import StoryWorkflowService
from lg_cli.workflow_runner import EXIT_CONFIGURATION
from tests.helpers import make_config
from tests.test_story_workflow import StoryAdapter


OPERATIONS = ('revision', 'scene-plan', 'scene-draft', 'bible-curate')


class StoryFailureStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = ProjectStore(self.root)
        self.project.initialize(name='Story failure boundaries')
        self.document = self.project.create_document(kind='note', slug='source', title='Source', content='Author source.')
        self.scene = self.project.create_scene(slug='scene', title='Scene', location='Author location')
        self.project.set_scene_plan('scene', 'Approved author plan.')
        self.project.approve_scene('scene')
        self.project.set_fact(category='world', key='gate', value='The gate closes at dusk.')
        self.adapter = StoryAdapter()
        self.runs = RunStore(self.root)
        self.service = StoryWorkflowService(make_config(self.root), project=self.project,
            adapter=self.adapter, runs=self.runs)

    def state(self):
        return (self.project.get_document('source'), self.project.list_versions('source'),
                self.project.get_scene('scene'), self.project.list_versions('scene'),
                self.project.list_proposals(), self.project.list_facts())

    def invoke(self, operation, **kwargs):
        if operation == 'revision':
            return self.service.revise_document('source', mode='light', **kwargs)
        if operation == 'scene-plan':
            return self.service.plan_scene('scene', **kwargs)
        if operation == 'scene-draft':
            return self.service.draft_scene('scene', **kwargs)
        return self.service.curate_document('source', **kwargs)

    def assert_failed_before_model(self, result, before, *, diagnostic_preview=False):
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, EXIT_CONFIGURATION)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.state(), before)
        manifest = self.runs.load(result.run_id)
        self.assertEqual(manifest['status'], 'failed')
        if diagnostic_preview:
            self.assertIn(manifest['completed_stages'], [[], [result.operation]])
        else:
            self.assertEqual(manifest['completed_stages'], [])
        self.assertIsNone(manifest['artifact_path'])
        self.assertNotIn('PRIVATE_', json.dumps(manifest))
        self.assertNotIn('PRIVATE_', result.error)

    def test_all_context_snapshot_failures_finalize_without_model_or_domain_writes(self):
        for operation in OPERATIONS:
            with self.subTest(operation=operation):
                before = self.state()
                with patch.object(self.project, 'context_snapshot', side_effect=OSError('PRIVATE_CONTEXT')):
                    result = self.invoke(operation)
                self.assert_failed_before_model(result, before)

    def test_context_prompt_render_failures_use_the_same_safe_boundary(self):
        targets = {'revision': '_revision_prompt', 'scene-plan': '_scene_plan_prompt',
                   'scene-draft': '_scene_draft_prompt', 'bible-curate': 'render_story_context_with_sources'}
        for operation, target in targets.items():
            with self.subTest(operation=operation):
                before = self.state()
                with patch('lg_cli.story_workflow.' + target, side_effect=RuntimeError('PRIVATE_RENDER')):
                    result = self.invoke(operation)
                self.assert_failed_before_model(result, before)

    def test_all_preview_archive_failures_finalize_without_model_or_domain_writes(self):
        for operation in OPERATIONS:
            for target in ('lg_cli.run_store.RunStore.write_stage', 'lg_cli.story_workflow.write_workflow_output'):
                with self.subTest(operation=operation, target=target):
                    before = self.state()
                    with patch(target, side_effect=OSError('PRIVATE_PREVIEW')):
                        result = self.invoke(operation, dry_run=True)
                    self.assert_failed_before_model(result, before, diagnostic_preview=True)
                    self.assertEqual(result.error, 'Story preview could not be fully saved.')

    def test_preview_event_failure_retains_diagnostics_but_cannot_be_adopted(self):
        before = self.state()
        original = self.runs.append_event

        def append(handle, event):
            if event.type == EventType.RUN_PLANNED:
                raise OSError('PRIVATE_PREVIEW_EVENT')
            return original(handle, event)

        with patch.object(self.runs, 'append_event', side_effect=append):
            result = self.invoke('revision', dry_run=True)
        self.assert_failed_before_model(result, before, diagnostic_preview=True)
        self.assertEqual(self.runs.load(result.run_id)['completed_stages'], ['revision'])
        with self.assertRaisesRegex(ValueError, 'completed'):
            self.runs.adopt(result.run_id, slug='unapproved', kind='note', title='Unapproved')

    def test_failure_manifest_precedes_independent_best_effort_events_and_log(self):
        before = self.state()
        handle, emit = self.service._new_run('test', 'revision', '', None)
        attempted = []

        def fail_event(_handle, event):
            self.assertEqual(self.runs.load(handle.run_id)['status'], 'failed')
            attempted.append(event.type)
            raise OSError('PRIVATE_EVENT')

        with patch.object(self.runs, 'append_event', side_effect=fail_event), \
                patch('lg_cli.story_workflow.append_agent_log', side_effect=OSError('PRIVATE_LOG')) as log:
            result = self.service._fail(handle, emit, 'Fixed failure.', EXIT_CONFIGURATION,
                document=self.document, stage_id='revision')
        self.assert_failed_before_model(result, before)
        self.assertEqual(attempted, [EventType.STAGE_FAILED, EventType.RUN_FAILED])
        log.assert_called_once()

    def test_startup_event_failures_persist_failed_then_raise_fixed_value_error(self):
        for operation in OPERATIONS:
            with self.subTest(operation=operation):
                before = self.state()
                original = self.runs.create
                handles = []

                def create(**kwargs):
                    handle = original(**kwargs)
                    handles.append(handle)
                    return handle

                with patch.object(self.runs, 'create', side_effect=create), \
                        patch.object(self.runs, 'append_event', side_effect=OSError('PRIVATE_START')):
                    with self.assertRaisesRegex(ValueError, '^Story workflow could not start\.$') as raised:
                        self.invoke(operation)
                self.assertTrue(raised.exception.__suppress_context__)
                self.assertEqual(self.runs.load(handles[0].run_id)['status'], 'failed')
                self.assertNotIn('PRIVATE_', json.dumps(self.runs.load(handles[0].run_id)))
                self.assertEqual(self.state(), before)
                self.assertEqual(self.adapter.calls, [])

    def test_startup_failure_does_not_hide_failed_finalize_exception(self):
        failure = OSError('PRIVATE_FINALIZE')
        with patch.object(self.runs, 'append_event', side_effect=OSError('PRIVATE_START')), \
                patch.object(self.runs, 'finalize', side_effect=failure):
            with self.assertRaises(OSError) as raised:
                self.invoke('revision')
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.adapter.calls, [])

    def test_failure_finalize_exception_remains_transparent_and_diagnostics_do_not_run(self):
        handle, emit = self.service._new_run('test', 'revision', '', None)
        failure = OSError('PRIVATE_FINALIZE')
        with patch.object(self.runs, 'finalize', side_effect=failure), \
                patch.object(emit, 'emit') as event, patch('lg_cli.story_workflow.append_agent_log') as log:
            with self.assertRaises(OSError) as raised:
                self.service._fail(handle, emit, 'Fixed failure.', EXIT_CONFIGURATION,
                    document=self.document, stage_id='revision')
        self.assertIs(raised.exception, failure)
        event.assert_not_called()
        log.assert_not_called()

    def test_planned_finalize_exception_is_not_reclassified_as_failed(self):
        before = self.state()
        failure = OSError('PRIVATE_FINALIZE')
        with patch.object(self.runs, 'finalize', side_effect=failure) as finalize:
            with self.assertRaises(OSError) as raised:
                self.invoke('revision', dry_run=True)
        self.assertIs(raised.exception, failure)
        self.assertEqual([call.kwargs['status'] for call in finalize.call_args_list], ['planned'])
        self.assertEqual(self.state(), before)
        self.assertEqual(self.adapter.calls, [])
