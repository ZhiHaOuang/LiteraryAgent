"""Story operations freeze their schema/profile before creating any proposals."""
import json
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from lg_cli.project_store import ProjectStore
from lg_cli.run_store import RunStore
from lg_cli.stage_input import StageInput
from lg_cli.story_workflow import StoryWorkflowService, _resource_path
from lg_cli.workflow_runner import EXIT_CONFIGURATION
from tests.helpers import make_config
from tests.test_story_workflow import StoryAdapter
from tests import test_execution_adapter as native_fixtures


class StorySchemaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = ProjectStore(self.root)
        self.project.initialize(name='Frozen Story contracts')
        self.document = self.project.create_document(kind='note', slug='source', title='Source',
            content='Original author source; the gate closes at dusk.')
        self.scene = self.project.create_scene(slug='gate', title='Gate', location='Author location')
        self.project.set_scene_plan(self.scene.document.id, 'Author approved plan.')
        self.project.approve_scene(self.scene.document.id)
        self.project.set_fact(category='world', key='gate', value='The gate closes at dusk.')
        self.config = make_config(self.root)
        self.runs = RunStore(self.root)

    def state(self):
        return (self.project.get_document('source'), self.project.list_versions('source'),
                self.project.get_scene('gate'), self.project.list_versions('gate'),
                self.project.list_proposals(), self.project.list_facts())

    def invoke(self, service, operation, **kwargs):
        if operation == 'revision':
            return service.revise_document('source', mode='light', **kwargs)
        if operation == 'scene-plan':
            return service.plan_scene('gate', **kwargs)
        if operation == 'scene-draft':
            return service.draft_scene('gate', **kwargs)
        return service.curate_document('source', **kwargs)

    def assert_failed(self, result, before):
        self.assertFalse(result.ok)
        self.assertEqual(self.state(), before)
        manifest = self.runs.load(result.run_id)
        self.assertEqual(manifest['status'], 'failed')
        self.assertEqual(manifest['completed_stages'], [])
        self.assertIsNone(manifest['artifact_path'])
        self.assertNotIn('PRIVATE_', json.dumps(manifest))

    def test_all_operations_reject_full_schema_invalid_output_before_any_domain_write(self):
        class InvalidAdapter(StoryAdapter):
            def run(self, **kwargs):
                result = super().run(**kwargs)
                payload = json.loads(result.output_text)
                payload['unexpected_private_field'] = 'PRIVATE_OUTPUT'
                return replace(result, output_text=json.dumps(payload), stdout='PRIVATE_STDOUT',
                    stderr='PRIVATE_STDERR', events=({'private': 'PRIVATE_EVENT'},))

        for operation in ('revision', 'scene-plan', 'scene-draft', 'bible-curate'):
            with self.subTest(operation=operation):
                before = self.state()
                adapter = InvalidAdapter()
                service = StoryWorkflowService(self.config, project=self.project, adapter=adapter)
                with patch('lg_cli.story_workflow.write_workflow_output') as artifact:
                    result = self.invoke(service, operation)
                self.assert_failed(result, before)
                self.assertEqual(result.error, 'Story stage output does not match the frozen schema.')
                artifact.assert_not_called()
                self.assertEqual(len(adapter.calls), 1)

    def test_all_input_failures_finalize_without_model_or_story_changes(self):
        for operation in ('revision', 'scene-plan', 'scene-draft', 'bible-curate'):
            for target in ('read_schema', 'write_stage_schema', 'write_stage_prompt'):
                with self.subTest(operation=operation, target=target):
                    before = self.state()
                    adapter = StoryAdapter()
                    service = StoryWorkflowService(self.config, project=self.project, adapter=adapter)
                    name = ('lg_cli.story_workflow.read_schema' if target == 'read_schema'
                            else 'lg_cli.run_store.RunStore.' + target)
                    with patch(name, side_effect=OSError('PRIVATE_INPUT_FAILURE')):
                        result = self.invoke(service, operation)
                    self.assert_failed(result, before)
                    self.assertEqual(result.exit_code, EXIT_CONFIGURATION)
                    self.assertEqual(adapter.calls, [])

    def test_dry_runs_keep_preview_and_author_gates_with_original_schema_basenames(self):
        adapter = StoryAdapter()
        service = StoryWorkflowService(replace(self.config, api_key=None), project=self.project, adapter=adapter)
        for operation, basename in (('revision', 'revision.schema.json'), ('scene-plan', 'scene-plan.schema.json'),
                                    ('scene-draft', 'scene-draft.schema.json'), ('bible-curate', 'memory-proposals.schema.json')):
            with self.subTest(operation=operation), patch.dict('os.environ', {'LG_WORKFLOW_GUIDANCE': 'LATEST_AUTHOR_GUIDANCE'}):
                before = self.state()
                result = self.invoke(service, operation, dry_run=True)
                self.assertTrue(result.ok, result.error)
                self.assertEqual(result.status, 'planned')
                self.assertEqual(self.state(), before)
                self.assertNotIn('LATEST_AUTHOR_GUIDANCE', result.text)
                folder = self.runs.handle(result.run_id).directory / 'stages'
                snapshot = folder / (operation + '.schema') / basename
                self.assertTrue(snapshot.exists())
                self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o600)
                self.assertIn('LATEST_AUTHOR_GUIDANCE', (folder / (operation + '.prompt.md')).read_text())
        self.assertEqual(adapter.calls, [])
        self.project.set_scene_plan('gate', 'Still a candidate plan.')
        before = self.state()
        result = service.draft_scene('gate', dry_run=True)
        self.assert_failed(result, before)
        self.assertIn('not approved', result.error)

    def test_source_rewrite_or_disappearance_and_role_change_do_not_change_prepared_contract(self):
        for action in ('rewrite', 'missing'):
            with self.subTest(action=action):
                source = self.root / 'revision.schema.json'
                contract = json.loads(_resource_path(source.name).read_text())
                contract['properties']['summary']['enum'] = ['Revised.']
                original_text = json.dumps(contract, indent=2)
                source.write_text(original_text)
                calls = []

                class CaptureAdapter(StoryAdapter):
                    def run(self, **kwargs):
                        calls.append(kwargs)
                        return super().run(**kwargs)

                service = StoryWorkflowService(self.config, project=self.project, adapter=CaptureAdapter())
                original_prepare = service._write_prompt
                resource = _resource_path

                def prepare(*args):
                    value, preview = original_prepare(*args)
                    self.assertIsInstance(value, StageInput)
                    self.assertEqual(replace(value, prompt='Changed').schema_text, value.schema_text)
                    if action == 'missing':
                        source.rename(self.root / 'retired-source.schema.json')
                    else:
                        contract['properties']['summary']['enum'] = ['B']
                        source.write_text(json.dumps(contract))
                    agent, _ = service._specialist('revision')
                    service.registry._agents[agent.id] = replace(agent, model_profile='critic', prompt='LATE_ROLE')
                    return value, preview

                with patch('lg_cli.story_workflow._resource_path',
                           side_effect=lambda name: source if name == source.name else resource(name)), \
                        patch.object(service, '_write_prompt', side_effect=prepare):
                    result = service.revise_document('source', mode='light')
                self.assertTrue(result.ok, result.error)
                self.assertEqual(calls[0]['model_profile'], 'writer')
                self.assertNotIn('LATE_ROLE', calls[0]['prompt'])
                self.assertEqual(calls[0]['output_schema'].name, source.name)
                self.assertNotEqual(calls[0]['output_schema'], source)
                self.assertEqual(calls[0]['output_schema'].read_text(), original_text)
                self.assertEqual(self.project.get_document('source').content, self.document.content)
                self.assertEqual(self.project.list_proposals(), [])

    def test_snapshot_tampering_during_preparation_before_model_or_after_output_is_rejected(self):
        for when in ('prepare', 'before', 'after'):
            with self.subTest(when=when):
                before = self.state()
                adapter = StoryAdapter()
                service = StoryWorkflowService(self.config, project=self.project, adapter=adapter)
                original_prepare = service._write_prompt
                original_write = service.runs.write_stage_prompt
                original_run = adapter.run

                def corrupt(path):
                    schema = json.loads(path.read_text())
                    schema['description'] = 'Changed but accepts the same output.'
                    path.write_text(json.dumps(schema))

                def prepare(*args):
                    value, preview = original_prepare(*args)
                    if when == 'before':
                        corrupt(value.schema_path)
                    return value, preview

                def write(handle, **kwargs):
                    path = original_write(handle, **kwargs)
                    if when == 'prepare':
                        corrupt(handle.directory / 'stages/revision.schema/revision.schema.json')
                    return path

                def run(**kwargs):
                    result = original_run(**kwargs)
                    if when == 'after':
                        corrupt(kwargs['output_schema'])
                    return result

                with patch.object(service, '_write_prompt', side_effect=prepare), \
                        patch.object(service.runs, 'write_stage_prompt', side_effect=write), \
                        patch.object(adapter, 'run', side_effect=run):
                    result = service.revise_document('source', mode='light')
                self.assert_failed(result, before)
                self.assertEqual(len(adapter.calls), 1 if when == 'after' else 0)


class NativeStorySchemaTests(unittest.TestCase):
    def test_actual_native_request_keeps_prepared_schema_and_specialist_profile(self):
        fixture = native_fixtures.NativeStageTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        service = StoryWorkflowService(fixture.config)
        source = fixture.root / 'revision.schema.json'
        contract = json.loads(_resource_path(source.name).read_text())
        contract['properties']['summary']['enum'] = ['A']
        source.write_text(json.dumps(contract))
        prepare = service._write_prompt
        resource = _resource_path

        def change(*args):
            prepared, preview = prepare(*args)
            contract['properties']['summary']['enum'] = ['B']
            source.write_text(json.dumps(contract))
            agent, _ = service._specialist('revision')
            service.registry._agents[agent.id] = replace(agent, model_profile='critic')
            return prepared, preview

        with patch('lg_cli.story_workflow._resource_path',
                   side_effect=lambda name: source if name == source.name else resource(name)), \
                patch.object(service, '_write_prompt', side_effect=change):
            result = service.revise_document('one', mode='light')
        self.assertTrue(result.ok, result.error)
        request = fixture.requests[0]
        self.assertEqual(request['text']['format']['schema']['properties']['summary']['enum'], ['A'])
        self.assertEqual(request['model'], 'native-writer')
        self.assertEqual(fixture.project.get_document('one').content, fixture.original.content)
