"""Workflow schema snapshots bind execution, full-response checks and recovery."""
import copy
import json
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from lg_cli.execution_adapter import NativeStageAdapter
from lg_cli.events import EventType
from lg_cli.init_project import init_workspace
from lg_cli.run_store import RunStore, _atomic_private_text
from lg_cli.workflow_runner import EXIT_CONFIGURATION, WorkflowRunner, _fingerprint, _output_fingerprint
from tests.helpers import FakeAdapter, make_config
from tests import test_execution_adapter as native_fixtures


class SchemaAdapter(FakeAdapter):
    def run(self, **kwargs):
        result = super().run(**kwargs)
        if result.ok and kwargs['output_schema'] is not None:
            schema = json.loads(kwargs['output_schema'].read_text())
            result = replace(result, output_text=json.dumps(native_fixtures.specimen(schema), ensure_ascii=False))
        return result


class WorkflowSchemaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        init_workspace(self.root)
        self.config = replace(make_config(self.root), enable_reference=False)
        self.runs = RunStore(self.root)

    def install_schema(self, runner, *, command='outline', extra=False):
        stage = runner._workflow(command)[1]['stages'][0]
        agent = runner.registry.agent(stage['agent'])
        value = json.loads(agent.output_schema.read_text())
        if extra:
            value['properties']['host_extra'] = {'type': 'string', 'enum': ['  EXACT_EXTRA  ']}
            value['required'].append('host_extra')
            value['properties']['artifact_markdown']['enum'] = ['  \n原始正文\n  ']
        source = self.root / 'source' / agent.output_schema.name
        source.parent.mkdir(exist_ok=True)
        source.write_text(json.dumps(value, indent=2))
        runner.registry._agents[agent.id] = replace(agent, output_schema=source)
        return stage, source

    def assert_failed(self, result, *, calls=None):
        self.assertFalse(result.ok)
        manifest = self.runs.load(result.run_id)
        self.assertEqual(manifest['status'], 'failed')
        self.assertEqual(manifest['completed_stages'], [])
        self.assertIsNone(manifest['artifact_path'])
        self.assertNotIn('PRIVATE_', json.dumps(manifest))
        if calls is not None:
            self.assertEqual(calls, [])

    def test_frozen_schema_rejects_full_contract_violation_before_any_checkpoint_or_artifact(self):
        class InvalidAdapter(SchemaAdapter):
            def run(self, **kwargs):
                result = super().run(**kwargs)
                value = json.loads(result.output_text)
                value['handoff']['constraints'] = [123]
                value['summary'] = 'PRIVATE_INVALID_OUTPUT'
                return replace(result, output_text=json.dumps(value))

        adapter = InvalidAdapter()
        with patch('lg_cli.workflow_runner.write_workflow_output') as artifact:
            result = WorkflowRunner(self.config, adapter=adapter).run('outline', 'Exact task')
        self.assert_failed(result)
        self.assertEqual(result.error, 'Stage output does not match the frozen schema')
        artifact.assert_not_called()
        directory = self.runs.handle(result.run_id).directory / 'stages'
        self.assertFalse(list(directory.glob('*.json')))
        for schema in (None, True, False):
            with self.subTest(schema=schema):
                adapter = FakeAdapter()
                runner = WorkflowRunner(self.config, adapter=adapter)
                stage, source = self.install_schema(runner, command='chat')
                agent = runner.registry.agent(stage['agent'])
                source.write_text(json.dumps(schema))
                runner.registry._agents[agent.id] = replace(agent, output_schema=source if schema is not None else None)
                outcome = runner.run('chat', 'Exact task')
                if schema is False:
                    self.assert_failed(outcome)
                else:
                    self.assertTrue(outcome.ok, outcome.error)

    def test_schema_read_and_write_failures_are_private_configuration_failures(self):
        for operation in ('read', 'write', 'prompt'):
            with self.subTest(operation=operation):
                adapter = SchemaAdapter()
                runner = WorkflowRunner(self.config, adapter=adapter)
                target = {'read': 'lg_cli.workflow_runner.read_schema',
                          'write': 'lg_cli.run_store.RunStore.write_stage_schema',
                          'prompt': 'lg_cli.run_store.RunStore.write_stage_prompt'}[operation]
                with patch(target, side_effect=OSError('PRIVATE_SCHEMA')):
                    result = runner.run('outline', 'Exact task')
                self.assert_failed(result, calls=adapter.calls)
                self.assertEqual(result.exit_code, EXIT_CONFIGURATION)

    def test_snapshot_changes_before_and_during_model_cannot_be_checkpointed(self):
        for when in ('before', 'during'):
            with self.subTest(when=when):
                adapter = SchemaAdapter()
                runner = WorkflowRunner(self.config, adapter=adapter)
                stage, source = self.install_schema(runner)

                def mutate(path):
                    contract = json.loads(path.read_text())
                    contract['description'] = 'Changed, but accepts exactly the same output.'
                    path.write_text(json.dumps(contract))

                original = adapter.run

                def during(**kwargs):
                    result = original(**kwargs)
                    mutate(kwargs['output_schema'])
                    return result

                def event(value):
                    if when == 'before' and value.type == EventType.STAGE_STARTED:
                        snapshot = self.runs.handle(value.run_id).directory / 'stages' / (
                            stage['id'] + '.schema') / source.name
                        mutate(snapshot)

                with patch.object(adapter, 'run', side_effect=during if when == 'during' else original):
                    result = runner.run('outline', 'Exact task', on_event=event)
                self.assert_failed(result)
                self.assertEqual(len(adapter.calls), 0 if when == 'before' else 1)

    def test_completed_full_response_is_private_and_restored_without_losing_extra_fields_or_whitespace(self):
        adapter = SchemaAdapter(fail_at=2)
        runner = WorkflowRunner(self.config, adapter=adapter)
        stage, source = self.install_schema(runner, extra=True)
        failed = runner.run('outline', 'Exact task')
        metadata = self.runs.read_stage_metadata(failed.run_id, stage['id'])
        self.assertEqual(json.loads(metadata['response_text'])['host_extra'], '  EXACT_EXTRA  ')
        self.assertEqual(json.loads(metadata['response_text'])['artifact_markdown'], '  \n原始正文\n  ')
        self.assertEqual(self.runs.read_stage(failed.run_id, stage['id']), '原始正文\n')
        folder = self.runs.handle(failed.run_id).directory / 'stages'
        self.assertEqual(stat.S_IMODE((folder / (stage['id'] + '.json')).stat().st_mode), 0o600)
        snapshot = folder / (stage['id'] + '.schema') / source.name
        self.assertEqual(snapshot.read_text(), source.read_text())
        self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o600)
        adapter.fail_at = None
        events = []
        resumed = runner.resume(failed.run_id, on_event=events.append)
        self.assertTrue(resumed.ok, resumed.error)
        self.assertEqual([event.stage_id for event in events if event.data.get('restored')], [stage['id']])
        restored = self.runs.read_stage_metadata(resumed.run_id, stage['id'])
        self.assertEqual(restored['response_text'], metadata['response_text'])

    def test_resume_validates_full_schema_and_projection_even_with_recomputed_digest(self):
        for tamper in ('missing-extra', 'projection'):
            with self.subTest(tamper=tamper):
                adapter = SchemaAdapter(fail_at=2)
                runner = WorkflowRunner(self.config, adapter=adapter)
                stage, _ = self.install_schema(runner, extra=True)
                failed = runner.run('outline', 'Exact task')
                metadata = self.runs.read_stage_metadata(failed.run_id, stage['id'])
                response = json.loads(metadata['response_text'])
                if tamper == 'missing-extra':
                    response.pop('host_extra')
                else:
                    response['summary'] = 'Different from the saved summary projection'
                metadata['response_text'] = json.dumps(response)
                content = self.runs.read_stage(failed.run_id, stage['id'])
                metadata['output_fingerprint'] = _output_fingerprint(content, metadata)
                path = self.runs.handle(failed.run_id).directory / 'stages' / (stage['id'] + '.json')
                path.write_text(json.dumps(metadata))
                adapter.fail_at = None
                events = []
                resumed = runner.resume(failed.run_id, on_event=events.append)
                self.assertTrue(resumed.ok, resumed.error)
                self.assertFalse(any(event.data.get('restored') for event in events))
                self.assertEqual(len(adapter.calls), 9)

    def test_v2_inputs_are_recomputed_for_literary_tasks_but_never_replayed_for_code(self):
        def old_fingerprint(value):
            if isinstance(value, dict) and value.get('policy') == 'lg.stage-input.v3':
                value = {**value, 'policy': 'lg.stage-input.v2'}
            return _fingerprint(value)

        for command in ('outline', 'code'):
            with self.subTest(command=command):
                adapter = SchemaAdapter(fail_at=2)
                runner = WorkflowRunner(self.config, adapter=adapter)
                name, workflow = runner._workflow(command)
                workflow = copy.deepcopy(workflow)
                if command == 'code':
                    second = copy.deepcopy(workflow['stages'][0])
                    workflow['stages'][0].update(id='preflight', final=False)
                    workflow['stages'].append(second)
                with patch.object(runner, '_workflow', return_value=(name, workflow)):
                    with patch('lg_cli.workflow_runner._fingerprint', side_effect=old_fingerprint):
                        failed = runner.run(command, 'Exact task')
                    self.assertFalse(failed.ok)
                    self.assertEqual(len(self.runs.load(failed.run_id)['completed_stages']), 1)
                    adapter.fail_at = None
                    previous_calls = len(adapter.calls)
                    events = []
                    resumed = runner.resume(failed.run_id, on_event=events.append)
                self.assertFalse(any(event.data.get('restored') for event in events))
                if command == 'code':
                    self.assertFalse(resumed.ok)
                    self.assertEqual(len(adapter.calls), previous_calls)
                    self.assertIn('Code checkpoint is unverified', resumed.error)
                else:
                    self.assertTrue(resumed.ok, resumed.error)
                    self.assertEqual(len(adapter.calls), previous_calls + 7)

    def test_checkpoint_archive_failure_finalizes_without_completed_stage(self):
        adapter = SchemaAdapter()
        with patch('lg_cli.run_store._atomic_private_text', autospec=True) as write:
            # Use the real writer for the schema; fail only the response metadata.

            def fail_metadata(path, text):
                if path.parent.name == 'stages' and path.suffix == '.json':
                    raise OSError('PRIVATE_RESPONSE_WRITE')
                return _atomic_private_text(path, text)

            write.side_effect = fail_metadata
            result = WorkflowRunner(self.config, adapter=adapter).run('outline', 'Exact task')
        self.assert_failed(result)
        self.assertEqual(result.error, 'Stage output could not be saved')
        self.assertEqual(len(adapter.calls), 1)

    def test_restore_snapshot_and_archive_failures_finalize_without_model_replay(self):
        for operation in ('snapshot', 'archive'):
            with self.subTest(operation=operation):
                adapter = SchemaAdapter(fail_at=2)
                runner = WorkflowRunner(self.config, adapter=adapter)
                stage, source = self.install_schema(runner)
                failed = runner.run('outline', 'Exact task')
                adapter.fail_at = None
                original = runner.store.write_stage_prompt

                def corrupt(handle, **kwargs):
                    path = original(handle, **kwargs)
                    snapshot = handle.directory / 'stages' / (kwargs['stage_id'] + '.schema') / source.name
                    snapshot.write_text('{}')
                    return path

                target = 'write_stage_prompt' if operation == 'snapshot' else 'write_stage'
                effect = corrupt if operation == 'snapshot' else OSError('PRIVATE_RESTORE_WRITE')
                with patch.object(runner.store, target, side_effect=effect):
                    result = runner.resume(failed.run_id)
                self.assert_failed(result)
                self.assertEqual(len(adapter.calls), 2)

    def test_restored_final_write_must_still_pass_required_overview_gate(self):
        class OverviewAdapter(SchemaAdapter):
            def run(self, **kwargs):
                result = super().run(**kwargs)
                value = json.loads(result.output_text)
                value['book_overview'] = '林舟寻找失踪者，逐步揭开故乡的秘密。'
                return replace(result, output_text=json.dumps(value, ensure_ascii=False))

        adapter = OverviewAdapter()
        runner = WorkflowRunner(self.config, adapter=adapter)
        name, workflow = runner._workflow('write')
        workflow = copy.deepcopy(workflow)
        workflow['stages'] = [workflow['stages'][0]]
        stage = workflow['stages'][0]
        with patch.object(runner, '_workflow', return_value=(name, workflow)):
            _, source = self.install_schema(runner, command='write')
            contract = json.loads(source.read_text())
            contract['properties']['book_overview'] = {'type': 'string'}
            source.write_text(json.dumps(contract))
            completed = runner.run('write', 'Exact task')
            self.assertTrue(completed.ok, completed.error)
            metadata = self.runs.read_stage_metadata(completed.run_id, stage['id'])
            response = json.loads(metadata['response_text'])
            response['book_overview'] = ''
            metadata['response_text'] = json.dumps(response)
            metadata.pop('book_overview')
            content = self.runs.read_stage(completed.run_id, stage['id'])
            metadata['output_fingerprint'] = _output_fingerprint(content, metadata)
            handle = self.runs.handle(completed.run_id)
            (handle.directory / 'stages' / (stage['id'] + '.json')).write_text(json.dumps(metadata))
            manifest = self.runs.load(completed.run_id)
            manifest['status'] = 'failed'
            handle.manifest_path.write_text(json.dumps(manifest))
            events = []
            resumed = runner.resume(completed.run_id, on_event=events.append)
        self.assertTrue(resumed.ok, resumed.error)
        self.assertFalse(any(event.data.get('restored') for event in events))
        self.assertEqual(len(adapter.calls), 2)

class WorkflowSchemaRaceTests(unittest.TestCase):
    def test_a_b_a_source_race_uses_saved_a_and_resume_restores_only_verified_stage(self):
        fixture = native_fixtures.NativeStageTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        runner = WorkflowRunner(fixture.config)
        stage = runner._workflow('outline')[1]['stages'][0]
        agent = runner.registry.agent(stage['agent'])
        value = json.loads(agent.output_schema.read_text())
        value['properties']['summary']['enum'] = ['A']
        source = fixture.root / 'changing.schema.json'
        original = json.dumps(value)
        source.write_text(original)
        runner.registry._agents[agent.id] = replace(agent, output_schema=source)
        native = NativeStageAdapter.run
        switched = False

        def mutate(adapter, **kwargs):
            nonlocal switched
            if not switched:
                switched = True
                changed = copy.deepcopy(value)
                changed['properties']['summary']['enum'] = ['B']
                source.write_text(json.dumps(changed))
                try:
                    return native(adapter, **kwargs)
                finally:
                    source.write_text(original)
            return native(adapter, **kwargs)

        fixture.fail_at = 2
        with patch.object(NativeStageAdapter, 'run', mutate):
            failed = runner.run('outline', 'Exact schema race task')
        self.assertFalse(failed.ok)
        self.assertEqual(fixture.requests[0]['text']['format']['schema']['properties']['summary']['enum'], ['A'])
        runs = RunStore(fixture.root)
        metadata = runs.read_stage_metadata(failed.run_id, stage['id'])
        self.assertEqual(metadata['summary'], 'A')
        self.assertEqual(source.read_text(), original)
        fixture.fail_at = None
        events = []
        resumed = runner.resume(failed.run_id, on_event=events.append)
        self.assertTrue(resumed.ok, resumed.error)
        self.assertEqual([event.stage_id for event in events if event.data.get('restored')], [stage['id']])
        self.assertEqual(len(fixture.requests), 8)
