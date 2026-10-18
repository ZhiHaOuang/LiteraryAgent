"""Expert execution works without a Native owner, transcript or MCP transport."""
import asyncio
import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from lg_cli.bounded_reviews import BoundedReviews
from tests.helpers import make_config
from tests.test_native_runtime import answer
from tests.test_review_policy import PreparedExpertPolicy


class BoundedReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = replace(make_config(Path(self.tmp.name)), protocol='responses',
            default_model='parent', critic_model='expert', timeout_seconds=10)
        self.parent = 'parent-turn-one'
        self.policy = PreparedExpertPolicy()
        self.events, self.saved, self.requests = [], [], []

        async def generate(config, request, delta):
            return answer('{"summary":"provisional"}')
        self.generate = generate

        def model(config):
            outer = self

            class Model:
                async def generate(self, request, delta):
                    outer.requests.append((config, copy.deepcopy(request)))
                    return await outer.generate(config, request, delta)
            return Model()

        self.reviews = BoundedReviews(self.policy, config=lambda: self.config,
            scope=lambda: {'host': 'selected'}, parent=lambda: self.parent, enabled=lambda: True,
            model_factory=model, save=lambda: self.saved.append(copy.deepcopy(self.reviews.records)),
            emit=lambda child: self.events.append(copy.deepcopy(child)))

    async def test_plain_result_and_frozen_requests_need_no_runtime_history(self):
        self.reviews.begin()
        self.reviews.prepare('review', {'experts': ['critic']})
        self.config = replace(self.config, critic_model='late-model')
        self.policy.instructions = 'late instructions'
        result = await self.reviews.execute('review')
        self.assertIsInstance(result, dict)
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['provisional'])
        self.assertTrue(result['sourceCurrent'])
        self.assertEqual(self.requests[0][1]['model'], 'expert')
        self.assertEqual(self.requests[0][1]['instructions'], 'HOST_EXPERT_INSTRUCTIONS')
        self.assertEqual(self.requests[0][1]['tools'], [])
        self.assertEqual(len(self.policy.calls), 1)
        self.assertEqual([e['state'] for e in self.events], ['running', 'completed'])
        self.assertEqual(self.reviews.records['review']['schema'], 'lg.expert-review.v1')
        result['experts'][0]['result']['summary'] = 'mutated consumer copy'
        self.assertEqual(self.reviews.records['review']['children'][0]['result']['summary'], 'provisional')

    async def test_parallel_prepare_reserves_budget_and_finish_does_not_reset_it(self):
        self.reviews.begin()

        async def prepare(identity):
            self.reviews.prepare(identity, {'experts': ['critic']})
            return identity

        outcomes = await asyncio.gather(prepare('one'), prepare('two'), return_exceptions=True)
        winner = next(value for value in outcomes if isinstance(value, str))
        self.assertEqual(sum(isinstance(value, ValueError) for value in outcomes), 1)
        await self.reviews.execute(winner)
        self.reviews.finish(winner)
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.reviews.prepare(winner, {'experts': ['continuity']})
        with self.assertRaisesRegex(ValueError, 'budget'):
            self.reviews.prepare('third', {'experts': ['critic']})
        with self.assertRaisesRegex(ValueError, 'already bound'):
            self.reviews.begin()
        self.reviews.prepare('other-expert', {'experts': ['continuity']})
        await self.reviews.execute('other-expert')
        self.reviews.finish('other-expert')
        self.parent = 'parent-turn-two'
        self.reviews.begin()
        self.reviews.prepare(winner, {'experts': ['critic']})
        self.assertEqual((await self.reviews.execute(winner))['status'], 'completed')

    async def test_preparation_requires_a_bound_parent_and_cannot_reset_live_work(self):
        with self.assertRaisesRegex(ValueError, 'not bound'):
            self.reviews.prepare('before-begin', {'experts': ['critic']})
        self.assertEqual(self.policy.calls, [])
        self.reviews.begin()
        self.reviews.prepare('registered', {'experts': ['critic']})
        self.parent = 'another-parent'
        with self.assertRaisesRegex(ValueError, 'must be settled'):
            self.reviews.begin()
        with self.assertRaisesRegex(ValueError, 'not bound'):
            self.reviews.prepare('late', {'experts': ['continuity']})
        result = await self.reviews.execute('registered')
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['sourceCurrent'])
        self.assertEqual(self.requests, [])

    async def test_parent_change_during_generation_discards_late_result(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def generate(*_):
            entered.set()
            await release.wait()
            return answer('{"summary":"late old-parent output"}')

        self.generate = generate
        self.reviews.begin()
        self.reviews.prepare('review', {'experts': ['critic']})
        task = asyncio.create_task(self.reviews.execute('review'))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.parent = 'new-parent'
            release.set()
            result = await task
            self.assertEqual(result['status'], 'failed')
            self.assertFalse(result['sourceCurrent'])
            self.assertIsNone(result['experts'][0]['result'])
            self.assertFalse(any(e['state'] == 'completed' for e in self.events))
        finally:
            release.set()
            if not task.done(): task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_joint_source_check_discards_both_experts_before_terminal_events(self):
        ready, release = asyncio.Event(), asyncio.Event()
        count = 0

        async def generate(*_):
            nonlocal count
            count += 1
            if count == 2:
                ready.set()
                await release.wait()
            return answer('{"summary":"checked original snapshot"}')

        self.generate = generate
        self.reviews.begin()
        self.reviews.prepare('pair', {'experts': ['critic', 'continuity']})
        task = asyncio.create_task(self.reviews.execute('pair'))
        try:
            await asyncio.wait_for(ready.wait(), 2)
            for _ in range(100):
                if self.reviews.records['pair']['children'][0]['state'] == 'completed': break
                await asyncio.sleep(.01)
            self.assertEqual(self.reviews.records['pair']['children'][0]['state'], 'completed')
            self.assertFalse(any(e['state'] == 'completed' for e in self.events))
            self.policy.unchanged = lambda *_: False
            release.set()
            result = await task
            self.assertEqual(result['status'], 'failed')
            self.assertTrue(all(c['result'] is None for c in result['experts']))
            self.assertEqual([e['state'] for e in self.events], ['running', 'running', 'failed', 'failed'])
        finally:
            release.set()
            if not task.done(): task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_expert_tool_request_fails_and_completed_call_cannot_replay(self):
        async def generate(*_):
            return {'status': 'completed', 'output': [{'type': 'function_call',
                'name': 'edit_manuscript', 'arguments': '{}', 'call_id': 'unauthorized'}]}

        self.generate = generate
        self.reviews.begin()
        self.reviews.prepare('review', {'experts': ['critic']})
        result = await self.reviews.execute('review')
        self.assertEqual(result['status'], 'failed')
        self.assertIsNone(result['experts'][0]['result'])
        with self.assertRaisesRegex(ValueError, 'replayed'):
            await self.reviews.execute('review')
        self.assertEqual(len(self.requests), 1)

    async def test_revocation_precedes_cancel_and_suppresses_late_model_output(self):
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def generate(*_):
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
                return answer('{"summary":"must not publish"}')

        self.generate = generate
        self.reviews.begin()
        self.reviews.prepare('review', {'experts': ['critic']})
        task = asyncio.create_task(self.reviews.execute('review'))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.reviews.settle_interrupted()
            self.assertFalse(self.reviews.live)
            self.assertEqual(self.reviews.records['review']['children'][0]['state'], 'cancelled')
            await asyncio.wait_for(cancelled.wait(), 2)
            self.assertFalse(task.done())
            release.set()
            result = await task
            self.assertEqual(result['status'], 'cancelled')
            self.assertFalse(result['sourceCurrent'])
            self.assertIsNone(self.reviews.records['review']['children'][0]['result'])
            self.assertFalse(any(e['state'] == 'completed' for e in self.events))
            self.assertFalse(self.reviews.tasks)
        finally:
            release.set()
            if not task.done(): task.cancel()
            await asyncio.gather(task, return_exceptions=True)
