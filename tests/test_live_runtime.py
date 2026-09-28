"""Opt-in integration against the pinned binary and an in-process fake provider."""
import asyncio
import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from lg_cli.anthropic_bridge import AnthropicBridge
from lg_cli.init_project import init_workspace
from lg_cli.live_agent import LiveAgent
from tests.helpers import make_config
from tests.test_anthropic_bridge import message_events


@unittest.skipUnless(os.environ.get('LG_TEST_RUNTIME_MANIFEST'), 'set LG_TEST_RUNTIME_MANIFEST for pinned-runtime integration')
class LiveRuntimeTests(unittest.TestCase):
    def test_creative_reviewer_has_read_tools_only(self):
        from lg_cli.creative_review import review_creative
        async def exercise(root):
            init_workspace(root)
            config = replace(make_config(root), provider='stepfun', protocol='anthropic',
                default_model='step-3.5-flash', runtime_manifest=Path(os.environ['LG_TEST_RUNTIME_MANIFEST']), timeout_seconds=60)
            payloads=[]
            def factory(payload):
                payloads.append(payload)
                yield from message_events(delta={'type':'text_delta','text':'{"decision":"decline","reason":"Insufficient evidence"}'})
            with patch('lg_cli.live_agent.provider_bridge', side_effect=lambda _: AnthropicBridge(config,stream_factory=factory)):
                result = await review_creative(config, {'fact':'test'})
            self.assertEqual(result['decision'],'decline')
            tools = json.dumps([tool['name'] for tool in payloads[0].get('tools',[])])
            self.assertNotIn('edit_manuscript',tools)
            self.assertNotIn('create_chapter',tools)
            self.assertNotIn('spawn_agent',tools)
        with tempfile.TemporaryDirectory() as raw:
            asyncio.run(exercise(Path(raw)))

    def test_persistent_thread_resumes_in_new_app_server_process(self):
        from lg_cli.conversation_store import ConversationStore
        async def exercise(root):
            init_workspace(root)
            config = replace(make_config(root), provider='stepfun', protocol='anthropic',
                default_model='step-3.5-flash', runtime_manifest=Path(os.environ['LG_TEST_RUNTIME_MANIFEST']), timeout_seconds=60)
            key = ConversationStore(root).create()
            payloads=[]
            def factory(payload):
                payloads.append(payload)
                yield from message_events(delta={'type':'text_delta','text':'Persisted answer.'})
            with patch('lg_cli.live_agent.provider_bridge', side_effect=lambda _: AnthropicBridge(config,stream_factory=factory)):
                first=LiveAgent(config,lambda *_:None,conversation=key)
                try:
                    await first.start()
                    thread=first.thread_id
                    await first.run('Remember SESSION_PERSISTENCE_MARKER; return a short acknowledgement without tools.')
                finally:
                    await first.close()
                second=LiveAgent(config,lambda *_:None,conversation=key)
                try:
                    await second.start()
                    self.assertEqual(second.thread_id,thread)
                    await second.run('Continue; return a short answer without tools.')
                    self.assertIn('SESSION_PERSISTENCE_MARKER',json.dumps(payloads[-1]))
                finally:
                    await second.close()
        with tempfile.TemporaryDirectory() as raw:
            asyncio.run(exercise(Path(raw)))

    def test_real_core_streaming_and_steering(self):
        async def exercise(root):
            init_workspace(root)
            config = replace(make_config(root), provider='stepfun', protocol='anthropic',
                default_model='step-3.5-flash', runtime_manifest=Path(os.environ['LG_TEST_RUNTIME_MANIFEST']), timeout_seconds=60)
            payloads=[]
            events=[]
            def factory(payload):
                payloads.append(payload)
                if len(payloads)==1:
                    time.sleep(1)
                yield from message_events(delta={'type':'text_delta','text':'LG live ready'})
            with patch('lg_cli.live_agent.provider_bridge', side_effect=lambda _: AnthropicBridge(config,stream_factory=factory)):
                agent=LiveAgent(config,lambda m,p:events.append((m,p)))
                try:
                    await agent.start()
                    turn=asyncio.create_task(agent.run('Connectivity check: return LG live ready without tools.'))
                    await asyncio.wait_for(agent._ready.wait(), 10)
                    await agent.steer('Also acknowledge STEER_MARKER.')
                    result=await turn
                    self.assertEqual(result['status'],'completed')
                    self.assertTrue(any(m=='item/agentMessage/delta' for m,p in events))
                    self.assertTrue(any('STEER_MARKER' in json.dumps(p) for p in payloads))
                finally:
                    await agent.close()
        with tempfile.TemporaryDirectory() as raw:
            asyncio.run(exercise(Path(raw)))

    def test_real_parallel_reviewers_report_to_main_agent(self):
        from lg_cli.live_view import LiveView
        from lg_cli.terminal_input import LiteraryInput
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        import threading

        def tools_event(calls):
            yield {'type': 'message_start', 'message': {'usage': {'input_tokens': 10, 'output_tokens': 0}}}
            for index, (name, args) in enumerate(calls):
                yield {'type': 'content_block_start', 'index': index,
                    'content_block': {'type': 'tool_use', 'id': 'call-' + str(index) + '-' + str(time.time_ns()), 'name': name, 'input': {}}}
                yield {'type': 'content_block_delta', 'index': index,
                    'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(args)}}
                yield {'type': 'content_block_stop', 'index': index}
            yield {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 10}}
            yield {'type': 'message_stop'}

        async def exercise(root, pipe):
            init_workspace(root)
            config = replace(make_config(root), provider='stepfun', protocol='anthropic',
                default_model='step-3.5-flash', runtime_manifest=Path(os.environ['LG_TEST_RUNTIME_MANIFEST']), timeout_seconds=60)
            lock=threading.Lock()
            main_calls=0
            def factory(payload):
                nonlocal main_calls
                texts=[]
                for message in payload['messages']:
                    if message['role']=='user':
                        content=message['content']
                        texts.extend([content] if isinstance(content,str) else [b.get('text','') for b in content if b.get('type')=='text'])
                if any(text.strip().endswith(('REVIEW_ALPHA','REVIEW_BETA')) for text in texts):
                    time.sleep(.6)
                    yield from message_events(delta={'type':'text_delta','text':'Review complete.'})
                    return
                with lock:
                    main_calls += 1
                    number=main_calls
                if number==1:
                    tool=next(t['name'] for t in payload['tools'] if 'task_name' in t.get('input_schema',{}).get('properties',{}))
                    yield from tools_event([(tool,{'task_name':'continuity','message':'Connectivity check: return Review complete without tools. REVIEW_ALPHA','fork_turns':'none'}),
                        (tool,{'task_name':'style','message':'Connectivity check: return Review complete without tools. REVIEW_BETA','fork_turns':'none'})])
                elif number==2:
                    tool=next(t['name'] for t in payload['tools'] if 'timeout_ms' in t.get('input_schema',{}).get('properties',{}))
                    yield from tools_event([(tool,{'timeout_ms':10000})])
                else:
                    yield from message_events(delta={'type':'text_delta','text':'Both reviews have been summarized.'})
            session=LiteraryInput(root/'history',input=pipe,output=DummyOutput(),usage_path=root/'usage.json')
            view=LiveView(session)
            events=[]
            def event(method,params):
                events.append((method,params))
                view(method,params)
            with patch('lg_cli.live_agent.provider_bridge', side_effect=lambda _: AnthropicBridge(config,stream_factory=factory)):
                agent=LiveAgent(config,event)
                try:
                    await agent.start()
                    view.thread_id=agent.thread_id
                    await agent.run('Connectivity test: coordinate two independent reviews, then summarize them.')
                    await asyncio.sleep(.3)
                    self.assertGreaterEqual(len(session.tasks),2,[(m,p.get('item',{}).get('type')) for m,p in events])
                    self.assertTrue(all(t['status']=='completed' for t in session.tasks.values()), (session.tasks, [p.get('turn', {}).get('error') for m,p in events if m=='turn/completed']))
                    self.assertIn('Both reviews', '\n'.join(v for _,v in session._blocks))
                    from lg_cli.usage_ledger import UsageLedger
                    usage = UsageLedger(root).summary()
                    self.assertEqual(usage['requests'], main_calls + 2)
                    self.assertEqual(usage['input'], (main_calls + 2) * 10)
                    self.assertEqual(usage['missing'], 0)
                    self.assertTrue(any(m == 'thread/tokenUsage/updated' for m, p in events), sorted(set(m for m,p in events)))
                    meter_root = root / 'subscription-meter'
                    meter = LiveAgent(replace(config, workspace=meter_root, auth_mode='chatgpt'), lambda *args: None)
                    meter.request = agent.request
                    for method, params in events:
                        meter._record_usage(method, params)
                    for thread in meter._usage_totals:
                        await meter._load_thread_model(thread)
                    metered = UsageLedger(meter_root).summary()
                    self.assertEqual((metered['input'], metered['output']), (usage['input'], usage['output']))
                    self.assertEqual([row['model'] for row in metered['models']], [config.default_model])
                finally:
                    await agent.close()
        with tempfile.TemporaryDirectory() as raw, create_pipe_input() as pipe:
            asyncio.run(exercise(Path(raw),pipe))

    def test_main_agent_controls_explicit_workflow_over_real_mcp(self):
        from lg_cli.workflow_control import WorkflowControl
        from lg_cli.terminal_input import LiteraryInput
        from lg_cli.live_view import LiveView
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        import sys

        async def exercise(root,pipe):
            init_workspace(root)
            config=replace(make_config(root),provider='stepfun',protocol='anthropic',default_model='step-3.5-flash',
                runtime_manifest=Path(os.environ['LG_TEST_RUNTIME_MANIFEST']),timeout_seconds=60)
            session=LiteraryInput(root/'history',input=pipe,output=DummyOutput(),usage_path=root/'usage.json')
            control=WorkflowControl(session)
            output=root/'new-direction.txt'
            code="import os,time,pathlib; print('READY',flush=True); g=os.environ.get('LG_WORKFLOW_GUIDANCE'); pathlib.Path(os.environ['RESULT']).write_text(g) if g else time.sleep(30)"
            worker=asyncio.create_task(control.run([sys.executable,'-u','-c',code],env=dict(os.environ,RESULT=str(output)),label='/outline old direction'))
            for _ in range(200):
                if 'READY' in session.transcript: break
                await asyncio.sleep(.01)
            job_id=control.status()['id']
            calls=[]
            def factory(payload):
                calls.append(payload)
                if len(calls)==1:
                    tool=next(t['name'] for t in payload['tools'] if set(t.get('input_schema',{}).get('properties',{}))=={'job_id'})
                    yield from message_events(block={'type':'tool_use','id':'stop-call','name':tool,'input':{}},
                        delta={'type':'input_json_delta','partial_json':json.dumps({'job_id':job_id})},stop='tool_use')
                elif len(calls)==2:
                    tool=next(t['name'] for t in payload['tools'] if 'guidance' in t.get('input_schema',{}).get('properties',{}))
                    yield from message_events(block={'type':'tool_use','id':'retry-call','name':tool,'input':{}},
                        delta={'type':'input_json_delta','partial_json':json.dumps({'job_id':job_id,'guidance':'STEER_FROM_MAIN'})},stop='tool_use')
                else:
                    yield from message_events(delta={'type':'text_delta','text':'已停止旧任务，并带新指令启动重试。'})
            view=LiveView(session)
            with patch('lg_cli.live_agent.provider_bridge',side_effect=lambda _:AnthropicBridge(config,stream_factory=factory)):
                agent=LiveAgent(config,view,control_socket=await control.endpoint())
                try:
                    await agent.start()
                    view.thread_id=agent.thread_id
                    await agent.run('Stop the active explicit command and retry it with the new direction.')
                    self.assertNotEqual(await worker,0)
                    for _ in range(200):
                        if not control.active: break
                        await asyncio.sleep(.01)
                    self.assertEqual(control.status()['status'],'completed')
                    self.assertEqual(output.read_text(),'STEER_FROM_MAIN')
                    self.assertEqual(control.status()['attempt'],2)
                    self.assertTrue(any('stopped' in json.dumps(p) for p in calls[1:]))
                finally:
                    await control.close()
                    await agent.close()
        with tempfile.TemporaryDirectory() as raw,create_pipe_input() as pipe:
            asyncio.run(exercise(Path(raw),pipe))
