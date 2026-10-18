"""Core consumes and validates one frozen schema, including across process launch."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from lg_cli.anthropic_bridge import AnthropicBridge
from lg_cli.core_adapter import CoreCommandCandidate, CodexExecAdapter, CodexStageExecutor, discover_core_commands
from tests.helpers import make_config
from tests.test_adapter import FAKE_CODEX
from tests.test_anthropic_bridge import message_events


def enum_schema(value):
    return {'type': 'object', 'additionalProperties': False,
            'properties': {'result': {'type': 'string', 'enum': [value]}}, 'required': ['result']}


class CoreOutputSchemaTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = make_config(self.root)
        self.schema = self.root / 'original.schema.json'
        self.schema.write_text(json.dumps(enum_schema('A')))
        self.argv = self.root / 'argv.json'

    def run_fake(self, output, *, output_schema=True, phase=None, change=None, adapter=None):
        fake = self.root / 'fake-codex.py'
        fake.write_text(FAKE_CODEX.replace('"adapter result"', repr(output)))
        env = {'LG_CODEX_COMMAND': f'{sys.executable} {fake}',
               'FAKE_PROMPT_PATH': str(self.root / 'prompt.txt'),
               'FAKE_ARGV_PATH': str(self.argv), 'FAKE_ENV_PATH': str(self.root / 'env.json')}

        def mutate():
            if change == 'missing':
                self.schema.rename(self.root / 'retired-source.schema.json')
            else:
                self.schema.write_text(json.dumps(enum_schema('B')))

        def discover(*args, **kwargs):
            candidates = discover_core_commands(*args, **kwargs)
            mutate()
            return candidates

        popen = subprocess.Popen

        def launch(*args, **kwargs):
            # Discovery's --version probe must still see the original source.
            if '--version' not in args[0]:
                mutate()
            return popen(*args, **kwargs)

        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, env, clear=True))
            stack.enter_context(patch('lg_cli.credentials.read_profiles', return_value={'profiles': {}}))
            stack.enter_context(patch('lg_cli.provider_transport.provider_bridge', return_value=nullcontext()))
            stack.enter_context(patch('socket.socket.connect', side_effect=AssertionError('Network forbidden')))
            if phase == 'discovery':
                stack.enter_context(patch('lg_cli.core_adapter.discover_core_commands', side_effect=discover))
            elif phase == 'launch':
                stack.enter_context(patch('lg_cli.core_adapter.subprocess.Popen', side_effect=launch))
            return (adapter or CodexStageExecutor()).run(prompt='Task', instructions='Host instructions',
                config=self.config, mode='write', output_schema=self.schema if output_schema else None)

    def snapshot(self):
        argv = json.loads(self.argv.read_text())
        return Path(argv[argv.index('--output-schema') + 1])

    def test_enum_mismatch_and_invalid_json_are_private_failures_even_after_exit_zero(self):
        for output in ('{"result":"PRIVATE_MODEL_OUTPUT"}', 'PRIVATE_INVALID_JSON'):
            with self.subTest(output=output):
                result = self.run_fake(output)
                self.assertFalse(result.ok)
                self.assertFalse(result.used_stub)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.error, 'Codex stage output does not match the supplied schema')
                self.assertEqual(result.output_text, '')
                self.assertNotIn('PRIVATE_', result.error + result.stdout + result.stderr + repr(result.events))

    def test_absent_schema_preserves_plain_text(self):
        result = self.run_fake('ordinary stage text', output_schema=False)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.output_text, 'ordinary stage text')
        self.assertNotIn('--output-schema', json.loads(self.argv.read_text()))

    def test_boolean_schemas_are_not_confused_with_an_absent_contract(self):
        for schema, output, expected in ((True, '{"anything":1}', True), (False, '{}', False)):
            with self.subTest(schema=schema):
                self.schema.write_text(json.dumps(schema))
                result = self.run_fake(output)
                self.assertEqual(result.ok, expected, result.error)
                self.assertEqual(json.loads(self.snapshot().read_text()), schema)

    def test_source_change_or_disappearance_after_freeze_never_changes_core_contract(self):
        for phase in ('discovery', 'launch'):
            for change in ('changed', 'missing'):
                with self.subTest(phase=phase, change=change):
                    raw = json.dumps(enum_schema('A'), indent=2)
                    self.schema.write_text(raw)
                    result = self.run_fake('{"result":"A"}', phase=phase, change=change)
                    self.assertTrue(result.ok, result.error)
                    snapshot = self.snapshot()
                    self.assertNotEqual(snapshot, self.schema)
                    self.assertEqual(snapshot.read_text(), raw)
                    self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o600)
                    self.assertTrue(snapshot.name.startswith('core-schema-'))

    def test_result_validation_uses_frozen_contract_after_source_changes(self):
        result = self.run_fake('{"result":"B"}', phase='discovery', change='changed')
        self.assertEqual(json.loads(self.schema.read_text()), enum_schema('B'))
        self.assertEqual(json.loads(self.snapshot().read_text()), enum_schema('A'))
        self.assertFalse(result.ok)
        self.assertEqual(result.returncode, 0)

    def test_invalid_or_external_schema_never_discovers_or_starts_provider(self):
        for raw in ('not-json PRIVATE_SCHEMA', '{"type":"PRIVATE_SCHEMA"}',
                    '{"$ref":"https://example.invalid/PRIVATE_SCHEMA"}'):
            with self.subTest(raw=raw):
                self.schema.write_text(raw)
                with patch('lg_cli.core_adapter.discover_core_commands') as discover, \
                        patch('lg_cli.provider_transport.provider_bridge') as bridge:
                    result = CodexStageExecutor().run(prompt='Task', instructions='Host instructions',
                        config=self.config, mode='write', output_schema=self.schema)
                self.assertFalse(result.ok)
                self.assertTrue(result.used_stub)
                self.assertEqual(result.error, 'Stage output schema is invalid or unreadable')
                self.assertNotIn('PRIVATE_SCHEMA', result.error)
                discover.assert_not_called()
                bridge.assert_not_called()

    def test_snapshot_write_failure_is_private_and_never_starts_core(self):
        original = self.schema.read_text()
        candidate = CoreCommandCandidate('synthetic', (sys.executable,), True, 'test fixture', 'test')
        mkdir = Path.mkdir

        def denied_directory(path, *args, **kwargs):
            if path == self.root / '.literarygiant/tmp':
                raise PermissionError('PRIVATE_SCHEMA')
            return mkdir(path, *args, **kwargs)

        for target, failure, error in (
            ('lg_cli.core_adapter.os.open', PermissionError('PRIVATE_SCHEMA'),
             'Stage output schema snapshot could not be saved'),
            ('lg_cli.core_adapter.Path.mkdir', denied_directory,
             'Stage temporary directory could not be created'),
        ):
            with self.subTest(target=target), \
                    patch('lg_cli.core_adapter.discover_core_commands', return_value=[candidate]), \
                    patch('lg_cli.provider_transport.provider_bridge', return_value=nullcontext()), \
                    patch(target, side_effect=failure, autospec=True), \
                    patch('lg_cli.core_adapter.subprocess.Popen') as launch:
                result = CodexStageExecutor().run(prompt='Task', instructions='Host instructions',
                    config=self.config, mode='write', output_schema=self.schema)
            self.assertFalse(result.ok)
            self.assertTrue(result.used_stub)
            self.assertIsNone(result.returncode)
            self.assertEqual(result.error, error)
            self.assertNotIn('PRIVATE_SCHEMA', result.error + result.stdout + result.stderr + result.output_text)
            launch.assert_not_called()
            self.assertEqual(self.schema.read_text(), original)

    def test_compatibility_preparation_keeps_original_schema_name(self):
        self.schema = self.root / 'revision.schema.json'
        self.schema.write_text(json.dumps(enum_schema('A')))
        from lg_cli.stage_input import prepare_stage_input
        original = CodexExecAdapter.run

        class LegacyAdapter(CodexExecAdapter):
            def run(self, **kwargs):
                kwargs.pop('instructions')
                return original(self, **kwargs)

        with patch('lg_cli.stage_input.prepare_stage_input', wraps=prepare_stage_input) as prepare:
            result = self.run_fake('{"result":"A"}', adapter=LegacyAdapter())
        self.assertTrue(result.ok, result.error)
        self.assertEqual(prepare.call_args.kwargs['output_schema'], self.schema)
        self.assertNotEqual(self.snapshot(), self.schema)


@unittest.skipUnless(os.environ.get('LG_TEST_RUNTIME_MANIFEST'), 'set LG_TEST_RUNTIME_MANIFEST for pinned Core')
class RealCoreSchemaTests(unittest.TestCase):
    def test_real_core_uses_private_snapshot_after_source_changes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            schema = root / 'original.schema.json'
            original_schema = json.dumps(enum_schema('A'), indent=2)
            schema.write_text(original_schema)
            config = replace(make_config(root), provider='stepfun', protocol='anthropic',
                default_model='step-3.5-flash', timeout_seconds=60,
                runtime_manifest=Path(os.environ['LG_TEST_RUNTIME_MANIFEST']))
            payloads = []

            def response(payload):
                payloads.append(payload)
                yield from message_events(block={'type': 'tool_use', 'id': 'local-final',
                    'name': 'lg_structured_output', 'input': {}},
                    delta={'type': 'input_json_delta', 'partial_json': '{"result":"A"}'}, stop='tool_use')

            def discover(*args, **kwargs):
                candidates = discover_core_commands(*args, **kwargs)
                schema.write_text(json.dumps(enum_schema('B')))
                return candidates

            with patch('lg_cli.credentials.read_profiles', return_value={'profiles': {}}), \
                    patch('lg_cli.core_adapter.discover_core_commands', side_effect=discover), \
                    patch('lg_cli.provider_transport.provider_bridge',
                          side_effect=lambda _: AnthropicBridge(config, stream_factory=response)):
                result = CodexStageExecutor().run(prompt='Return A.', instructions='Only return supplied JSON.',
                    config=config, mode='write', output_schema=schema)
            self.assertTrue(result.ok, result.error)
            self.assertEqual(json.loads(result.output_text), {'result': 'A'})
            self.assertEqual(json.loads(schema.read_text()), enum_schema('B'))
            snapshot = Path(result.command[result.command.index('--output-schema') + 1])
            self.assertNotEqual(snapshot, schema)
            self.assertEqual(snapshot.read_text(), original_schema)
            self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o600)
            self.assertTrue(payloads)
            self.assertEqual(payloads[0]['tools'][-1]['input_schema'], enum_schema('A'))
