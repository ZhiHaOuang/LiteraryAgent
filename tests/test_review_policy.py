"""Injected expert inputs remain separate from Native scheduling and transport."""
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lg_cli.agent_runtime import ExpertInput, ReviewInput, runtime_factory
from lg_cli.native_delegation import NativeDelegations
from lg_cli.native_events import NativeEvents
from lg_cli.review_policy import LiteraryReviewPolicy, TOOL
from tests.helpers import make_config
from tests.test_native_runtime import answer


RESULT_SCHEMA = {'type': 'object', 'additionalProperties': False,
                 'properties': {'summary': {'type': 'string'}}, 'required': ['summary']}


class PreparedExpertPolicy:
    tool = TOOL

    def __init__(self):
        self.instructions = 'HOST_EXPERT_INSTRUCTIONS'
        self.calls = []

    def available(self, scope):
        return True

    def prepare(self, scope, arguments, *, context_limit):
        self.calls.append((scope, arguments, context_limit))
        return ReviewInput('host-task', 'host-task-hash', 'host-source-hash', tuple(
            ExpertInput(name, 'critic', self.instructions, 'HOST_EVIDENCE', json.dumps(RESULT_SCHEMA))
            for name in arguments['experts']))

    def unchanged(self, scope, record):
        return True

    def output_schema(self, name):
        return RESULT_SCHEMA


class Context:
    def instructions(self, **kwargs):
        return 'HOST_PARENT_INSTRUCTIONS'

    def history(self, text):
        return text


class NoTools:
    def __init__(self, *args, **kwargs):
        self.tools = {}

    async def start(self):
        pass

    async def close(self):
        pass


class ReviewPolicyBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = replace(make_config(Path(self.tmp.name)), runtime_engine='native', protocol='responses',
                              default_model='parent-model', critic_model='expert-model', timeout_seconds=15)

    async def test_injected_policy_cannot_expand_executor_budget_or_forge_recovery(self):
        policy = PreparedExpertPolicy()
        policy.tool = copy.deepcopy(TOOL)
        policy.tool['parameters']['properties']['experts'] = {'type': 'array', 'items': {'type': 'string'}}
        owner = SimpleNamespace(config=self.config, runtime=None, review_only=False, messages=[], pending=[],
            turn_id='host-turn', save=lambda: None, presentation=NativeEvents(lambda *_: None))
        delegated = NativeDelegations(owner, policy)
        delegated.begin()
        for names in ([], ['one', 'one'], ['one', 'two', 'three']):
            with self.subTest(names=names), self.assertRaisesRegex(ValueError, 'bounded expert'):
                delegated.prepare('invalid', {'experts': names})
        self.assertEqual(policy.calls, [])
        delegated.prepare('first', {'experts': ['one', 'two']})
        with self.assertRaisesRegex(ValueError, 'budget'):
            delegated.prepare('second', {'experts': ['three']})
        self.assertEqual(len(policy.calls), 1)
        records = copy.deepcopy(delegated.records)
        records['second'] = copy.deepcopy(records['first'])
        records['second']['children'] = [{**records['first']['children'][0], 'id': 'third-child', 'expert': 'three'}]
        owner.pending = ['first', 'second']
        owner.messages = [{'type': 'function_call', 'call_id': call_id, 'name': policy.tool['name'],
            'arguments': json.dumps({'experts': names})} for call_id, names in
            [('first', ['one', 'two']), ('second', ['three'])]]
        with self.assertRaisesRegex(ValueError, 'children'):
            NativeDelegations(owner, policy).restore(records)
        self.assertEqual(owner.pending, ['first', 'second'])
        self.assertFalse(any(item.get('type') == 'function_call_output' for item in owner.messages))

    async def test_injected_policy_executes_without_product_source_or_definition_reads(self):
        policy = PreparedExpertPolicy()
        requests = []

        class Model:
            def __init__(self, config):
                self.config = config

            async def generate(self, request, delta):
                requests.append(request)
                if request['model'] == 'expert-model':
                    return answer('{"summary":"provisional host response"}')
                if any(item.get('type') == 'function_call_output' for item in request['input']):
                    return answer('Main synthesis')
                return {'status': 'completed', 'output': [{'type': 'function_call', 'call_id': 'host-review',
                    'name': policy.tool['name'], 'arguments': '{"experts":["critic"]}'}]}

        with patch('lg_cli.review_policy.ProjectStore', side_effect=AssertionError('Product source read')), \
                patch('lg_cli.review_policy.DefinitionRegistry', side_effect=AssertionError('Product definition read')), \
                patch.object(LiteraryReviewPolicy, 'prepare', side_effect=AssertionError('Default policy used')):
            runtime = runtime_factory(self.config, lambda *_: None, context_provider=Context(),
                expert_policy=policy, tools_factory=NoTools, model_factory=Model)
            try:
                await runtime.start()
                result = await runtime.run('Review host evidence')
                self.assertEqual(result.status, 'completed')
            finally:
                await runtime.close()
        self.assertEqual(len(policy.calls), 1)
        self.assertEqual(len(requests), 3)
        self.assertEqual(requests[1]['instructions'], 'HOST_EXPERT_INSTRUCTIONS')
        self.assertEqual(requests[1]['input'], [{'role': 'user', 'content': 'HOST_EVIDENCE'}])
        self.assertEqual(requests[1]['tools'], [])
        self.assertEqual(requests[1]['text']['format']['schema'], RESULT_SCHEMA)
        output = next(item for item in runtime.messages if item.get('type') == 'function_call_output')
        self.assertTrue(json.loads(output['output'])['structuredContent']['provisional'])

    async def test_prepared_request_and_model_are_not_rebuilt_before_execution(self):
        policy = PreparedExpertPolicy()
        seen = []

        class Model:
            def __init__(self, config):
                self.config = config

            async def generate(self, request, delta):
                seen.append((self.config, request))
                return answer('{"summary":"frozen request"}')

        owner = SimpleNamespace(config=self.config, runtime=None, review_only=False, messages=[], pending=[],
            turn_id='host-turn', model_factory=Model, save=lambda: None, presentation=NativeEvents(lambda *_: None))
        delegated = NativeDelegations(owner, policy)
        delegated.begin()
        delegated.prepare('host-review', {'experts': ['critic']})
        policy.instructions = 'LATE_INSTRUCTIONS'
        owner.config = replace(self.config, critic_model='late-model', max_output_tokens=99999)
        with patch.object(policy, 'prepare', side_effect=AssertionError('Input rebuilt')):
            result = await delegated.execute('host-review')
        self.assertFalse(result.isError)
        self.assertEqual(seen[0][1]['instructions'], 'HOST_EXPERT_INSTRUCTIONS')
        self.assertEqual(seen[0][1]['model'], 'expert-model')
        self.assertLessEqual(seen[0][0].max_output_tokens, 4096)
        self.assertEqual(len(policy.calls), 1)

    async def test_review_only_runtime_cannot_enable_nested_experts(self):
        policy = PreparedExpertPolicy()
        seen = []

        class Model:
            def __init__(self, config):
                pass

            async def generate(self, request, delta):
                seen.append(request)
                return answer('Review only')

        runtime = runtime_factory(self.config, lambda *_: None, context_provider=Context(),
            expert_policy=policy, tools_factory=NoTools, model_factory=Model, review_only=True)
        try:
            await runtime.start()
            await runtime.run('Read only')
        finally:
            await runtime.close()
        self.assertEqual(seen[0]['tools'], [])
        self.assertEqual(policy.calls, [])

    async def test_default_policy_is_lazy_and_both_runtimes_consume_injection(self):
        with patch('lg_cli.review_policy.ProjectStore', side_effect=AssertionError('Eager source read')), \
                patch('lg_cli.review_policy.DefinitionRegistry', side_effect=AssertionError('Eager definition read')):
            runtime = runtime_factory(self.config, lambda *_: None, context_provider=Context())
            self.assertIsInstance(runtime.delegations.policy, LiteraryReviewPolicy)
        policy = PreparedExpertPolicy()
        runtime = runtime_factory(replace(self.config, runtime_engine='codex'), lambda *_: None,
                                  context_provider=Context(), expert_policy=policy)
        self.assertIs(runtime.delegations.policy, policy)
        self.assertEqual(runtime.delegations.tool, policy.tool)
        self.assertEqual(policy.calls, [])
