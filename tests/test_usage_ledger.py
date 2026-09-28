import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from lg_cli.anthropic_bridge import AnthropicBridge
from lg_cli.provider_transport import ResponsesBridge
from lg_cli.usage_ledger import UsageLedger
from tests.helpers import make_config
from tests.test_anthropic_bridge import message_events


class UsageLedgerTests(unittest.TestCase):
    def test_subscription_events_track_child_models_and_deduplicate_responses(self):
        from lg_cli.live_agent import LiveAgent
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            agent = LiveAgent(replace(make_config(root), auth_mode='chatgpt'), lambda *args: None)
            agent._record_usage('thread/started', {'thread': {'id':'child','model':'review-model'}})
            event = {'threadId':'child', 'tokenUsage':{'total':{'inputTokens':131,'outputTokens':27},
                'last':{'inputTokens':31,'outputTokens':7}}}
            agent._record_usage('thread/tokenUsage/updated', event)
            agent._record_usage('thread/tokenUsage/updated', event)
            data = UsageLedger(root).summary()
            self.assertEqual(data['requests'],1)
            self.assertEqual(data['models'][0]['model'],'review-model')
            self.assertEqual(data['input'],31)

    def test_historical_read_is_repeatable_read_only_and_cutoff_avoids_duplicates(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);run=root/'.literarygiant/runs/old'
            run.mkdir(parents=True)
            (run/'run.json').write_text(json.dumps({'provider':'provider'}))
            def event(sequence,timestamp):
                return {'sequence':sequence,'timestamp':timestamp,'data':{'engine_event':{
                    'type':'turn.completed','usage':{'input_tokens':20,'output_tokens':5}}}}
            old=event(1,'2020-01-01T00:00:00+00:00')
            future=event(2,'2099-01-01T00:00:00+00:00')
            path=run/'events.jsonl'
            path.write_text(json.dumps(old)+'\n'+json.dumps(old)+'\n')
            ledger=UsageLedger(root)
            first=ledger.summary()
            self.assertEqual(first['input'],20)
            self.assertEqual(first['historical'],1)
            self.assertFalse(ledger.path.exists())
            self.assertEqual(ledger.summary(),first)
            ledger.start_capture()
            ledger.record('new','provider','model',{'input_tokens':7,'output_tokens':2})
            path.write_text(path.read_text()+json.dumps(future)+'\n')
            combined=ledger.summary()
            self.assertEqual((combined['input'],combined['output']), (27,7))
            self.assertEqual(combined['historical'],1)

    def test_anthropic_counts_cache_once_and_preserves_interrupted_usage(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);config=replace(make_config(root),provider='stepfun',protocol='anthropic',default_model='model')
            def factory(payload):
                events=list(message_events())
                events[0]['message']['usage'].update(cache_read_input_tokens=4,cache_creation_input_tokens=2)
                yield from events
            bridge=AnthropicBridge(config,stream_factory=factory)
            list(bridge.response_events({'model':'model','input':'hello'}))
            ledger=UsageLedger(root)
            self.assertEqual((ledger.summary()['input'],ledger.summary()['output']),(16,3))
            stream=bridge.events({'model':'model'})
            next(stream);stream.close()
            usage=ledger.summary()
            self.assertEqual(usage['partial'],1)
            self.assertEqual(usage['requests'],2)

    def test_responses_failure_retains_reported_usage(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);config=replace(make_config(root),provider='openai',protocol='responses')
            bridge=ResponsesBridge(config)
            with self.assertRaises(ValueError):
                list(bridge.validated_events([{'type':'response.incomplete','response':{
                    'usage':{'input_tokens':11,'output_tokens':4}}}], {'model':'test'}))
            usage=UsageLedger(root).summary()
            self.assertEqual((usage['input'],usage['output'],usage['partial']),(11,4,1))
