"""Core review identities and crash settlement use the existing session metadata."""
import asyncio
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from lg_cli.bounded_reviews import MAX_RESULT_BYTES
from lg_cli.codex_reviews import CodexReviews
from lg_cli.conversation_store import ConversationStore
from lg_cli.session_runtime import SessionRuntime
from tests.helpers import make_config
from tests.test_native_runtime import answer
from tests.test_review_policy import PreparedExpertPolicy


class CodexReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root_path = Path(self.tmp.name)
        self.config = replace(make_config(self.root_path), protocol='responses',
            default_model='parent-model', critic_model='expert-model', timeout_seconds=10)
        self.key = ConversationStore(self.root_path).create()
        self.runtime = SessionRuntime(self.root_path, self.key)
        self.runtime.acquire()
        self.addCleanup(self.runtime.release)
        self.runtime.update(thread_id='root-thread', engine='codex')
        self.root, self.turn, self.enabled = 'root-thread', 'turn-one', True
        self.requests, self.events = [], []
        self.policy = PreparedExpertPolicy()

        async def generate(config, request):
            return answer('{"summary":"HOST_OBSERVATION"}')
        self.generate = generate
        self.reviews = self.make_reviews()
        self.reviews.restore()
        self.reviews.begin()

    async def asyncTearDown(self):
        self.reviews.revoke()
        await self.reviews.drain()

    def make_reviews(self):
        outer = self

        class Model:
            def __init__(self, config):
                self.config = config

            async def generate(self, request, delta):
                outer.requests.append((self.config, copy.deepcopy(request)))
                delta('PRIVATE_EXPERT_DELTA')
                return await outer.generate(self.config, request)

        return CodexReviews(self.config, self.policy, runtime=self.runtime,
            root=lambda: self.root, turn=lambda: self.turn, enabled=lambda: self.enabled,
            emit=lambda child: self.events.append(copy.deepcopy(child)), model_factory=Model)

    def params(self, call='call-one', experts=None, **changes):
        return {'threadId': self.root, 'turnId': self.turn, 'callId': call,
            'namespace': None, 'tool': 'delegate_review', 'arguments': {'experts': experts or ['critic']}, **changes}

    def value(self, response):
        self.assertEqual(set(response), {'contentItems', 'success'})
        self.assertEqual(response['contentItems'][0]['type'], 'inputText')
        return json.loads(response['contentItems'][0]['text'])

    async def test_model_input_and_terminal_are_saved_before_success_without_secrets(self):
        observed = []

        async def generate(config, request):
            saved = self.runtime.read()['codex_delegations']
            observed.append(saved)
            self.assertIn('call-one', saved['calls'])
            self.assertEqual(saved['used'], ['critic'])
            self.assertEqual(saved['seen'], ['call-one'])
            self.assertEqual(saved['calls']['call-one']['record']['children'][0]['state'], 'running')
            self.assertEqual(config.default_model, 'expert-model')
            self.assertLessEqual(config.max_output_tokens, 4096)
            self.assertEqual(request['tools'], [])
            return answer('{"summary":"HOST_OBSERVATION"}')

        self.generate = generate
        response = await self.reviews.call(self.params())
        self.assertTrue(response['success'])
        value = self.value(response)
        self.assertTrue(value['provisional'])
        self.assertTrue(value['sourceCurrent'])
        self.assertEqual(len(observed), 1)
        saved = self.runtime.read()['codex_delegations']
        self.assertTrue(saved['calls']['call-one']['settled'])
        self.assertEqual(saved['calls']['call-one']['record']['children'][0]['result'], {'summary': 'HOST_OBSERVATION'})
        raw = json.dumps(saved)
        for private in ('HOST_EVIDENCE', 'HOST_EXPERT_INSTRUCTIONS', 'PRIVATE_EXPERT_DELTA', self.config.api_key):
            self.assertNotIn(private, raw)
        self.assertNotIn('PRIVATE_EXPERT_DELTA', json.dumps(self.events))
        self.assertFalse(self.reviews.pending)

    async def test_identity_and_authority_refusals_never_call_model_or_policy_prepare(self):
        mutations = ({'threadId': 'child-thread'}, {'turnId': 'old-turn'}, {'namespace': 'foreign'},
            {'tool': 'edit_manuscript'}, {'callId': ''}, {'callId': 'x' * 129},
            {'arguments': []}, {'arguments': {'experts': ['critic'], 'model': 'unapproved'}},
            {'extra': 'forbidden'})
        for changes in mutations:
            with self.subTest(changes=changes):
                self.assertFalse((await self.reviews.call(self.params(**changes)))['success'])
        self.enabled = False
        self.assertFalse((await self.reviews.call(self.params()))['success'])
        self.assertEqual(self.requests, [])
        self.assertEqual(self.policy.calls, [])

    async def test_only_host_begin_resets_budget_and_finished_identity_cannot_replay(self):
        first = await self.reviews.call(self.params())
        self.assertTrue(first['success'])
        for params in (self.params(), self.params(experts=['continuity']), self.params('repeat-expert')):
            self.assertFalse((await self.reviews.call(params))['success'])
        with self.assertRaisesRegex(ValueError, 'already bound'):
            self.reviews.begin()
        self.assertTrue((await self.reviews.call(self.params('other', ['continuity'])))['success'])
        self.assertEqual(len(self.requests), 2)
        self.turn = 'turn-two'
        self.assertFalse((await self.reviews.call(self.params('new-turn-call')))['success'])
        self.reviews.begin()
        self.assertTrue((await self.reviews.call(self.params()))['success'])
        self.assertEqual(len(self.requests), 3)

    async def test_completed_call_survives_reopen_and_never_requests_model_twice(self):
        self.assertTrue((await self.reviews.call(self.params()))['success'])
        reopened = self.make_reviews()
        with patch.object(self.policy, 'prepare', side_effect=AssertionError('Prepared on restore')):
            reopened.restore()
            self.assertFalse((await reopened.call(self.params()))['success'])
            with self.assertRaisesRegex(ValueError, 'already bound'):
                reopened.begin()
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(self.runtime.read()['codex_delegations']['calls']['call-one']['settled'])

    async def test_serialization_expansion_is_rejected_before_success_and_checkpoint_reopens(self):
        result = {'summary': 'a' * (MAX_RESULT_BYTES - len('{"summary":""}'))}
        compact = json.dumps(result, separators=(',', ':'))
        self.assertEqual(len(compact.encode()), MAX_RESULT_BYTES)
        self.assertGreater(len(json.dumps(result, ensure_ascii=False).encode()), MAX_RESULT_BYTES)

        async def generate(*_):
            return answer(compact)

        self.generate = generate
        response = await self.reviews.call(self.params())
        self.assertFalse(response['success'])
        saved = self.runtime.read()['codex_delegations']['calls']['call-one']
        self.assertTrue(saved['settled'])
        self.assertEqual(saved['record']['children'][0]['state'], 'failed')
        self.assertIsNone(saved['record']['children'][0]['result'])
        reopened = self.make_reviews()
        reopened.restore()
        self.assertFalse((await reopened.call(self.params()))['success'])
        self.assertEqual(len(self.requests), 1)

    async def test_required_focus_call_reopens_without_persisting_focus(self):
        self.policy.tool = copy.deepcopy(self.policy.tool)
        self.policy.tool['parameters']['required'] = ['experts', 'focus']
        self.reviews = self.make_reviews()
        self.reviews.restore()
        self.turn = 'focus-turn'
        self.reviews.begin()
        params = self.params(arguments={'experts': ['critic'], 'focus': 'PRIVATE_AUTHOR_FOCUS'})
        self.assertTrue((await self.reviews.call(params))['success'])
        self.assertEqual(self.policy.calls[-1][1]['focus'], 'PRIVATE_AUTHOR_FOCUS')
        self.assertNotIn('PRIVATE_AUTHOR_FOCUS', json.dumps(self.runtime.read()))
        reopened = self.make_reviews()
        reopened.restore()
        self.assertFalse((await reopened.call(params))['success'])
        self.assertEqual(len(self.requests), 1)

    async def test_restore_settles_model_started_and_model_completed_crash_windows(self):
        for completed in (False, True):
            with self.subTest(completed=completed):
                self.turn = 'crash-' + str(completed)
                self.reviews.begin()
                entered, release = asyncio.Event(), asyncio.Event()

                async def generate(*_):
                    entered.set()
                    await release.wait()
                    return answer('{"summary":"UNDELIVERED_OBSERVATION"}')

                self.generate = generate
                task = asyncio.create_task(self.reviews.call(self.params()))
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    crash = copy.deepcopy(self.runtime.read()['codex_delegations'])
                    if completed:
                        child = crash['calls']['call-one']['record']['children'][0]
                        child.update(state='completed', result={'summary': 'UNDELIVERED_OBSERVATION'}, error=None)
                    self.reviews.revoke()
                    release.set()
                    await asyncio.wait_for(task, 2)
                    await self.reviews.drain()
                    self.runtime.update(codex_delegations=crash)
                    reopened = self.make_reviews()
                    count = len(self.requests)
                    with patch.object(self.policy, 'prepare', side_effect=AssertionError('Crash replay')):
                        reopened.restore()
                        saved = self.runtime.read()['codex_delegations']['calls']['call-one']
                        self.assertTrue(saved['settled'])
                        self.assertTrue(saved['interrupted'])
                        result = reopened.reviews.result(saved['record'], interrupted=saved['interrupted'])
                        self.assertEqual(result['status'], 'cancelled')
                        self.assertFalse(result['sourceCurrent'])
                        self.assertFalse((await reopened.call(self.params()))['success'])
                    self.assertEqual(len(self.requests), count)
                finally:
                    release.set()
                    if not task.done(): task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_pre_model_save_failure_sends_no_request_and_blocks_later_calls(self):
        with patch.object(self.runtime, 'update', side_effect=OSError('PRIVATE_FILESYSTEM_BODY')):
            response = await self.reviews.call(self.params())
        self.assertFalse(response['success'])
        self.assertNotIn('PRIVATE_', json.dumps(response))
        self.assertEqual(self.requests, [])
        self.assertFalse((await self.reviews.call(self.params('retry')))['success'])
        with self.assertRaises(ValueError):
            self.reviews.begin()

    async def test_terminal_save_failure_cannot_return_success_or_repeat_after_restore(self):
        original = self.runtime.update

        def update(**values):
            state = values.get('codex_delegations', {})
            if any(entry['settled'] for entry in state.get('calls', {}).values()):
                raise OSError('PRIVATE_TERMINAL_FAILURE')
            return original(**values)

        with patch.object(self.runtime, 'update', side_effect=update):
            response = await self.reviews.call(self.params())
        self.assertFalse(response['success'])
        self.assertNotIn('PRIVATE_', json.dumps(response))
        self.assertEqual(len(self.requests), 1)
        reopened = self.make_reviews()
        reopened.restore()
        self.assertFalse((await reopened.call(self.params()))['success'])
        self.assertEqual(len(self.requests), 1)

    async def test_revoke_precedes_cancel_and_ignores_provider_swallowing_cancellation(self):
        entered, cancelled, release, returned = (asyncio.Event() for _ in range(4))

        async def generate(*_):
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
            returned.set()
            return answer('{"summary":"PRIVATE_LATE_OUTPUT"}')

        self.generate = generate
        task = asyncio.create_task(self.reviews.call(self.params()))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.assertTrue(self.reviews.pending)
            self.reviews.revoke()
            self.assertFalse(self.reviews.reviews.live)
            await asyncio.wait_for(cancelled.wait(), 2)
            release.set()
            self.assertFalse((await asyncio.wait_for(task, 2))['success'])
            await self.reviews.drain()
            self.assertTrue(returned.is_set())
            self.assertFalse(self.reviews.pending)
            saved = self.runtime.read()['codex_delegations']['calls']['call-one']
            self.assertTrue(saved['interrupted'])
            self.assertIsNone(saved['record']['children'][0]['result'])
            self.assertNotIn('PRIVATE_LATE_OUTPUT', json.dumps(self.events))
        finally:
            release.set()
            if not task.done(): task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_repeated_revoke_preserves_settled_records_and_budget(self):
        self.assertTrue((await self.reviews.call(self.params()))['success'])
        saved = copy.deepcopy(self.runtime.read()['codex_delegations'])
        events = copy.deepcopy(self.events)
        self.reviews.revoke()
        self.reviews.revoke()
        await self.reviews.drain()
        self.assertEqual(self.runtime.read()['codex_delegations'], saved)
        self.assertEqual(self.events, events)
        self.assertFalse(self.reviews.pending)
        self.assertFalse((await self.reviews.call(self.params('late-call', ['continuity'])))['success'])
        self.assertEqual(len(self.requests), 1)

    async def test_parent_change_source_change_and_tools_never_publish_success(self):
        for change in ('parent', 'source', 'tool'):
            with self.subTest(change=change):
                self.turn = 'case-' + change
                self.reviews.begin()

                async def generate(*_):
                    if change == 'parent': self.turn = 'changed-parent'
                    if change == 'source': self.policy.unchanged = lambda *_: False
                    if change == 'tool':
                        return {'status': 'completed', 'output': [{'type': 'function_call', 'name': 'shell'}]}
                    return answer('{"summary":"UNVERIFIED_RESULT"}')

                self.generate = generate
                response = await self.reviews.call(self.params())
                self.assertFalse(response['success'])
                self.assertFalse(any(child['state'] == 'completed' for child in self.events))
                self.policy.unchanged = lambda *_: True

    async def test_switching_engine_preserves_codex_binding_and_corruption_fails_closed(self):
        self.assertTrue((await self.reviews.call(self.params()))['success'])
        self.runtime.select_engine('native')
        self.runtime.update(thread_id='native-thread')
        self.runtime.select_engine('codex')
        self.assertEqual(self.runtime.read()['thread_id'], 'root-thread')
        reopened = self.make_reviews()
        reopened.restore()
        self.assertFalse((await reopened.call(self.params()))['success'])
        saved = self.runtime.read()['codex_delegations']
        for mutate in (
            lambda value: value.update(rootThreadId='foreign-root'),
            lambda value: value.update(used=['critic', 'continuity']),
            lambda value: value['calls']['call-one']['record']['children'][0].update(result={'invalid': True}),
        ):
            broken = copy.deepcopy(saved)
            mutate(broken)
            self.runtime.update(codex_delegations=broken)
            with self.assertRaisesRegex(ValueError, 'checkpoint'):
                self.make_reviews().restore()
        self.runtime.update(codex_delegations=saved)
        self.assertEqual(len(self.requests), 1)

    async def test_unstarted_revocation_is_safe_and_tool_copy_cannot_change_policy(self):
        adapter = self.make_reviews()
        adapter.revoke()
        adapter.restore()
        adapter.tool['description'] = 'changed consumer tool'
        self.assertNotEqual(adapter.tool, self.policy.tool)
