import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from lg_cli.terminal_input import LiteraryInput
from lg_cli.workflow_control import WorkflowControl, control_request
from lg_cli.core_adapter import CodexExecAdapter, CoreCommandCandidate
from tests.helpers import make_config
from lg_cli.config import load_config
from lg_cli.main import interactive_loop


class WorkflowControlTests(unittest.TestCase):
    def test_busy_command_accepts_main_agent_direction_until_reply_finishes(self):
        with tempfile.TemporaryDirectory() as raw, create_pipe_input() as pipe:
            root = Path(raw)
            session = LiteraryInput(root / 'history', input=pipe, output=DummyOutput(),
                usage_path=root / 'usage.json')
            received = []

            class Agent:
                thread_id = 'main'

                def __init__(self, config, on_event, **kwargs):
                    self.endpoint = kwargs['control_socket']

                async def start(self, **kwargs):
                    pass

                async def run(self, text):
                    received.append(text)
                    status = await asyncio.to_thread(control_request, self.endpoint, 'status')
                    await asyncio.to_thread(control_request, self.endpoint, 'stop', job_id=status['id'])
                    main_stopped.set()
                    await finish_reply.wait()

                async def close(self):
                    pass

            def run(handler, *args, **kwargs):
                async def exercise():
                    nonlocal main_stopped, finish_reply
                    main_stopped, finish_reply = asyncio.Event(), asyncio.Event()
                    session._handler = handler
                    original = session.run_command

                    async def fake_command(argv, **options):
                        return await original([sys.executable, '-u', '-c',
                            "import time; print('READY',flush=True); time.sleep(30)"])

                    session.run_command = fake_command
                    command = asyncio.create_task(session._dispatch('/outline original'))
                    try:
                        for _ in range(200):
                            if 'READY' in session.transcript:
                                break
                            await asyncio.sleep(.01)
                        self.assertTrue(session.busy)
                        steering = asyncio.create_task(session._send_steering('停止这个任务'))
                        await asyncio.wait_for(main_stopped.wait(), 10)
                        await command
                        self.assertTrue(session.busy)
                        self.assertIn('停止这个任务', received[0])
                        self.assertIn('/outline original', received[0])
                        finish_reply.set()
                        await steering
                        self.assertFalse(session.busy)
                    finally:
                        finish_reply.set()
                        await session.shutdown_handler()
                    return 0

                return asyncio.run(exercise())

            main_stopped = finish_reply = None
            with patch('lg_cli.main.LiteraryInput', return_value=session), \
                 patch('lg_cli.live_agent.LiveAgent', Agent), patch.object(session, 'run', run):
                self.assertEqual(interactive_loop(load_config(root / 'book', environment='ui-test')), 0)

    def test_real_process_stop_retry_and_stale_control(self):
        async def exercise(root, pipe):
            session=LiteraryInput(root/'history',input=pipe,output=DummyOutput(),usage_path=root/'usage.json')
            control=WorkflowControl(session)
            target=root/'guidance.txt'
            code="import os,time,pathlib; print('READY',flush=True); g=os.environ.get('LG_WORKFLOW_GUIDANCE'); pathlib.Path(os.environ['RESULT']).write_text(g) if g else time.sleep(30)"
            env=dict(os.environ,RESULT=str(target))
            task=asyncio.create_task(control.run([sys.executable,'-u','-c',code],env=env,label='/outline original'))
            try:
                for _ in range(200):
                    if 'READY' in session.transcript: break
                    await asyncio.sleep(.01)
                endpoint=await control.endpoint()
                self.assertEqual(Path(endpoint).stat().st_mode & 0o777,0o600)
                status=await asyncio.to_thread(control_request,endpoint,'status')
                self.assertEqual(status['status'],'running')
                with self.assertRaises(ValueError):
                    await asyncio.to_thread(control_request,endpoint,'stop',job_id='stale')
                stopped=await asyncio.to_thread(control_request,endpoint,'stop',job_id=status['id'])
                self.assertEqual(stopped['status'],'stopped')
                self.assertNotEqual(await task,0)
                restarted=await asyncio.to_thread(control_request,endpoint,'retry',job_id=status['id'],guidance='改成第一人称')
                self.assertEqual(restarted['attempt'],2)
                for _ in range(200):
                    if not control.active: break
                    await asyncio.sleep(.01)
                self.assertEqual(control.status()['status'],'completed')
                self.assertEqual(target.read_text(),'改成第一人称')
                self.assertFalse(session.busy)
                with self.assertRaises(ValueError):
                    await control.retry(status['id'])
                control.reset()
                self.assertEqual(control.status(),{'status':'idle'})
            finally:
                await control.close()
        with tempfile.TemporaryDirectory() as raw,create_pipe_input() as pipe:
            asyncio.run(exercise(Path(raw),pipe))

    def test_retry_guidance_reaches_adapter_prompt(self):
        with tempfile.TemporaryDirectory() as raw:
            config=make_config(Path(raw))
            adapter=CodexExecAdapter()
            candidate=CoreCommandCandidate('fake',('fake',),True,'test','test')
            with patch.dict(os.environ,{'LG_WORKFLOW_GUIDANCE':'author revision'}), \
                 patch('lg_cli.core_adapter.discover_core_commands',return_value=[candidate]), \
                 patch.object(adapter,'_run_available',return_value='done') as run:
                self.assertEqual(adapter.run(prompt='original task',config=config,mode='outline'),'done')
                self.assertIn('author revision',run.call_args.args[1])
                self.assertIn('original task',run.call_args.args[1])
