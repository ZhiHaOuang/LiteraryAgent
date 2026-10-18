"""One host-prepared launch plan governs Core startup and effective config."""
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lg_cli.agent_runtime import RuntimePolicyError, runtime_factory
from lg_cli.anthropic_bridge import AnthropicBridge, translate_request
from lg_cli.codex_policy import READ_TOOLS, WritingLaunchPolicy
from lg_cli.config import load_config
from lg_cli.conversation_store import ConversationStore
from lg_cli.credentials import environment_dir
from lg_cli.init_project import init_workspace
from lg_cli.live_agent import CodexSession, LiveAgent
from lg_cli.native import writing_settings
from lg_cli.session_runtime import SessionRuntime
from tests.helpers import make_config
from tests.test_anthropic_bridge import message_events
from tests.test_live_agent import SERVER


def snapshot(plan):
    config = {}
    for key, raw in plan.overrides:
        cursor = config
        parts = key.split('.')
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = json.loads(raw)
    config['mcp_servers']['lg_writing']['environment_id'] = 'local'
    return {'config': config, 'layers': []}


class LaunchPolicyTests(unittest.TestCase):
    def test_bounded_parent_disables_core_agents_without_shrinking_literary_tools(self):
        with tempfile.TemporaryDirectory() as raw:
            config = make_config(Path(raw))
            ordinary = WritingLaunchPolicy().prepare(config)
            bounded = WritingLaunchPolicy().prepare(config, allow_core_delegation=False)
            normal = snapshot(ordinary)['config']
            reply = snapshot(bounded)
            bounded.validate(reply)
            effective = reply['config']
            self.assertFalse(effective['agents']['enabled'])
            self.assertFalse(effective['features']['multi_agent_v2'])
            self.assertEqual(effective['mcp_servers']['lg_writing']['enabled_tools'],
                             normal['mcp_servers']['lg_writing']['enabled_tools'])
            self.assertIn('edit_manuscript', effective['mcp_servers']['lg_writing']['enabled_tools'])

    def test_review_requires_explicit_agents_disable_in_effective_config(self):
        with tempfile.TemporaryDirectory() as raw:
            plan = WritingLaunchPolicy().prepare(make_config(Path(raw)), review_only=True)
            for agents in (None, {}, {'enabled': True}, {'enabled': 'false'}):
                with self.subTest(agents=agents):
                    reply = snapshot(plan)
                    if agents is None:
                        reply['config'].pop('agents', None)
                    else:
                        reply['config']['agents'] = agents
                    with self.assertRaises(RuntimePolicyError):
                        plan.validate(reply)

    def test_plan_freezes_nested_settings_and_validation_does_not_prepare_again(self):
        with tempfile.TemporaryDirectory() as raw:
            config = make_config(Path(raw))
            settings = writing_settings(config)
            with patch('lg_cli.codex_policy.writing_settings', return_value=settings):
                plan = WritingLaunchPolicy().prepare(config)
            before = plan.overrides
            settings['mcp_servers.lg_writing.args'].append('PRIVATE_MUTATION')
            settings['features.multi_agent'] = False
            with patch('lg_cli.codex_policy.writing_settings', side_effect=AssertionError('Prepared twice')):
                plan.validate(snapshot(plan))
            self.assertEqual(plan.overrides, before)
            self.assertNotIn('PRIVATE_MUTATION', repr(plan.overrides))

    def test_effective_environment_and_multi_agent_must_match_frozen_plan(self):
        with tempfile.TemporaryDirectory() as raw:
            config = replace(make_config(Path(raw)), environment='isolated')
            plan = WritingLaunchPolicy().prepare(config)
            for mutation in ('environment', 'multi_agent'):
                with self.subTest(mutation=mutation):
                    reply = snapshot(plan)
                    if mutation == 'environment':
                        reply['config']['mcp_servers']['lg_writing']['args'][-1] = 'PRIVATE_OTHER_ENV'
                    else:
                        reply['config']['features']['multi_agent'] = False
                    with self.assertRaises(RuntimePolicyError) as caught:
                        plan.validate(reply)
                    self.assertNotIn('PRIVATE_OTHER_ENV', str(caught.exception))

    def test_named_environment_reaches_real_mcp_parser_and_config_loader(self):
        from lg_cli import writing_mcp
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home, book = root / 'home', root / 'book'
            book.mkdir()
            with patch.object(Path, 'home', return_value=home), patch.dict(os.environ, {}, clear=True):
                user = home / '.literarygiant/config.toml'
                user.parent.mkdir(parents=True)
                user.write_text('[model]\nprovider="stepfun"\ndefault="default-user-model"\n')
                named = environment_dir('audit-isolated') / 'config.toml'
                named.parent.mkdir(parents=True)
                named.write_text('[model]\nprovider="stepfun"\ndefault="isolated-parent-model"\n')
                parent = load_config(book, environment='audit-isolated')
                plan = WritingLaunchPolicy().prepare(parent)
                args = json.loads(dict(plan.overrides)['mcp_servers.lg_writing.args'])[2:]
                received = []

                def server(config, **kwargs):
                    received.append(config)
                    return SimpleNamespace(run=lambda **kwargs: None)

                with patch.object(writing_mcp, 'create_server', side_effect=server):
                    with patch.object(sys, 'argv', ['writing_mcp', *args]):
                        writing_mcp.main()
                    with patch.object(sys, 'argv', ['writing_mcp', *args[:-2]]):
                        writing_mcp.main()
                self.assertEqual((received[0].environment, received[0].default_model),
                                 ('audit-isolated', 'isolated-parent-model'))
                self.assertEqual((received[1].environment, received[1].default_model),
                                 (None, 'default-user-model'))

    def test_native_rejects_core_specific_policy(self):
        with tempfile.TemporaryDirectory() as raw:
            config = replace(make_config(Path(raw)), runtime_engine='native')
            with self.assertRaisesRegex(ValueError, 'only by Codex'):
                runtime_factory(config, lambda *_: None, launch_policy=WritingLaunchPolicy())


class DevelopmentLaunchTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_development_command_preserves_policy_bypass(self):
        with tempfile.TemporaryDirectory() as raw:
            policy = SimpleNamespace(prepare=lambda *a, **k: self.fail('Development command used production policy'))
            agent = LiveAgent(make_config(Path(raw)), lambda *_: None,
                              command=[sys.executable, '-u', '-c', SERVER], launch_policy=policy)
            try:
                await agent.start()
                self.assertEqual(agent.thread_id, 'thread-main')
            finally:
                await agent.close()


@unittest.skipUnless(os.environ.get('LG_TEST_RUNTIME_MANIFEST'), 'set LG_TEST_RUNTIME_MANIFEST for pinned Core')
class CoreLaunchPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        init_workspace(self.root)
        self.config = replace(make_config(self.root), provider='stepfun', protocol='anthropic',
            default_model='step-3.5-flash', timeout_seconds=60,
            runtime_manifest=Path(os.environ['LG_TEST_RUNTIME_MANIFEST']))

    async def test_review_resume_disables_inherited_v2_and_normal_resume_restores_it(self):
        key = ConversationStore(self.root).create()
        names, payloads = [], []
        collaboration = {'spawn_agent', 'send_message', 'followup_task', 'wait_agent',
                         'interrupt_agent', 'list_agents'}

        def capture(request, max_tokens):
            payload, mapping = translate_request(request, max_tokens)
            names.append({spec['name'] for spec in mapping.values()})
            return payload, mapping

        def response(payload):
            payloads.append(payload)
            yield from message_events(delta={'type': 'text_delta', 'text': 'Synthetic acknowledgement.'})

        root = None
        with patch('lg_cli.live_agent.provider_bridge', side_effect=lambda _: AnthropicBridge(self.config, stream_factory=response)), \
                patch('lg_cli.anthropic_bridge.translate_request', side_effect=capture):
            for index, review in enumerate((False, True, False)):
                agent = LiveAgent(self.config, lambda *_: None, conversation=key, review_only=review)
                try:
                    await agent.start()
                    root = root or agent.thread_id
                    self.assertEqual(agent.thread_id, root)
                    await agent.run('Remember REVIEW_ROOT_HISTORY. Reply without tools.' if index == 0
                                    else 'Continue the earlier discussion without tools.')
                    if review:
                        self.assertFalse(names[-1] & collaboration, names[-1])
                        self.assertTrue(set(READ_TOOLS) <= names[-1])
                        self.assertNotIn('edit_manuscript', names[-1])
                        self.assertIn('REVIEW_ROOT_HISTORY', json.dumps(payloads[-1]))
                    else:
                        self.assertTrue(collaboration <= names[-1], names[-1])
                finally:
                    await agent.close()

    async def test_injected_policy_is_late_bound_prepared_once_and_review_keeps_four_tools(self):
        sequence, prepared, payloads, mappings = [], [], [], []
        base = WritingLaunchPolicy()

        def prepare(config, **kwargs):
            prepared.append(kwargs)
            plan = base.prepare(config, **kwargs)

            def validate(reply):
                sequence.append('validate')
                plan.validate(reply)
            return SimpleNamespace(overrides=plan.overrides, validate=validate)

        def response(payload):
            payloads.append(payload)
            yield from message_events(delta={'type': 'text_delta', 'text': 'Review complete.'})

        original = CodexSession.request

        def translate(request, max_tokens):
            payload, mapping = translate_request(request, max_tokens)
            mappings.append(mapping)
            return payload, mapping

        async def record(session, method, params):
            sequence.append(method)
            return await original(session, method, params)

        runtime = runtime_factory(self.config, lambda *_: None, review_only=True,
                                  launch_policy=SimpleNamespace(prepare=prepare))
        self.assertEqual(prepared, [])
        socket = str(self.root / 'bound-later.sock')
        runtime.control_socket = socket
        with patch('lg_cli.live_agent.provider_bridge', side_effect=lambda _: AnthropicBridge(self.config, stream_factory=response)), \
                patch.object(CodexSession, 'request', record), \
                patch('lg_cli.anthropic_bridge.translate_request', side_effect=translate), \
                patch('lg_cli.codex_policy.writing_settings', wraps=writing_settings) as settings:
            try:
                await runtime.start()
                await runtime.start()
                await runtime.run('Only reply, do not call tools.')
            finally:
                await runtime.close()
            self.assertEqual(settings.call_count, 1)
        self.assertEqual(prepared, [{'control_socket': socket, 'review_only': True}])
        self.assertLess(sequence.index('config/read'), sequence.index('validate'))
        self.assertLess(sequence.index('validate'), sequence.index('thread/start'))
        names = {value['name'] for value in mappings[0].values()}
        self.assertEqual(names, set(READ_TOOLS) | {'list_mcp_resources', 'list_mcp_resource_templates',
                                                 'read_mcp_resource', 'request_user_input'})

    async def test_rejected_plan_closes_core_before_start_or_resume_without_private_errors(self):
        key = ConversationStore(self.root).create()
        original = CodexSession.request
        sequence, processes, payloads = [], [], []

        async def record(session, method, params):
            sequence.append(method)
            if method == 'config/read':
                processes.append(session.process)
            return await original(session, method, params)

        def prepare(config, **kwargs):
            plan = WritingLaunchPolicy().prepare(config, **kwargs)

            def reject(reply):
                raise TypeError('SYNTHETIC_PRIVATE_CONFIG_VALUE')
            return SimpleNamespace(overrides=plan.overrides, validate=reject)

        def response(payload):
            payloads.append(payload)
            return iter(())

        with patch('lg_cli.live_agent.provider_bridge', side_effect=lambda _: AnthropicBridge(self.config, stream_factory=response)):
            for resume in (False, True):
                if resume:
                    first = LiveAgent(self.config, lambda *_: None, conversation=key)
                    try:
                        await first.start()
                    finally:
                        await first.close()
                saved = SessionRuntime(self.root, key).read()
                sequence.clear()
                agent = LiveAgent(self.config, lambda *_: None, conversation=key,
                                  launch_policy=SimpleNamespace(prepare=prepare))
                with patch.object(CodexSession, 'request', record):
                    try:
                        with self.assertRaises(RuntimePolicyError) as caught:
                            await agent.start()
                        self.assertNotIn('SYNTHETIC_PRIVATE', str(caught.exception))
                        self.assertNotIn('thread/start', sequence)
                        self.assertNotIn('thread/resume', sequence)
                        self.assertIsNone(agent.process)
                        self.assertIsNotNone(processes[-1].returncode)
                        self.assertEqual(SessionRuntime(self.root, key).read(), saved)
                    finally:
                        await agent.close()
        self.assertEqual(payloads, [])

    async def test_prepare_and_override_errors_do_not_start_core_or_expose_private_values(self):
        class BrokenPlan:
            @property
            def overrides(self):
                raise KeyError('SYNTHETIC_PRIVATE_OVERRIDE')

        def broken_prepare(*args, **kwargs):
            raise RuntimeError('SYNTHETIC_PRIVATE_PREPARE')

        with patch('lg_cli.live_agent.provider_bridge', side_effect=lambda _: AnthropicBridge(self.config, stream_factory=lambda _: iter(()))):
            for prepare in (broken_prepare, lambda *a, **k: BrokenPlan()):
                agent = LiveAgent(self.config, lambda *_: None,
                                  launch_policy=SimpleNamespace(prepare=prepare))
                with patch('lg_cli.live_agent.asyncio.create_subprocess_exec', side_effect=AssertionError('Core started')) as spawn:
                    try:
                        with self.assertRaises(RuntimePolicyError) as caught:
                            await agent.start()
                        self.assertNotIn('SYNTHETIC_PRIVATE', str(caught.exception))
                        self.assertIsNone(agent.process)
                        spawn.assert_not_called()
                    finally:
                        await agent.close()
