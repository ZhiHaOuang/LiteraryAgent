import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from lg_cli.live_agent import LiveAgent, LiveAgentError
from tests.helpers import make_config

SERVER = r'''
import json, sys

def send(value):
 print(json.dumps(value), flush=True)
for line in sys.stdin:
 r=json.loads(line)
 m=r['method']
 if 'id' not in r: continue
 p=r['params']
 if m=='initialize': result={}
 elif m=='thread/start': result={'thread':{'id':'thread-main'}}
 elif m=='turn/start': result={'turn':{'id':'turn-1'}}
 elif m=='turn/steer':
  assert p['threadId']=='thread-main' and p['expectedTurnId']=='turn-1'
  text=p['input'][0]['text']
  if text=='reject':
   send({'id':r['id'],'error':{'message':'turn mismatch'}})
   continue
  result={'turnId':'turn-1'}
  send({'method':'item/agentMessage/delta','params':{'threadId':'thread-main','itemId':'a1','delta':text}})
 elif m=='turn/interrupt': result={}
 else: result={}
 send({'id':r['id'],'result':result})
 if m in ('turn/steer','turn/interrupt'):
  send({'method':'turn/completed','params':{'threadId':'thread-main','turn':{'id':'turn-1','status':'completed' if m=='turn/steer' else 'interrupted'}}})
'''


class LiveAgentTests(unittest.TestCase):
    def test_wire_protocol_steers_active_turn_and_closes(self):
        async def exercise(config):
            events=[]
            agent=LiveAgent(config, lambda m,p: events.append((m,p)), command=[sys.executable,'-u','-c',SERVER])
            try:
                await agent.start()
                turn=asyncio.create_task(agent.run('original request'))
                await asyncio.wait_for(agent._ready.wait(), 2)
                await agent.steer('new direction')
                self.assertEqual((await turn)['status'],'completed')
                self.assertIn(('item/agentMessage/delta',{'threadId':'thread-main','itemId':'a1','delta':'new direction'}),events)
                with self.assertRaises(LiveAgentError):
                    await agent.steer('too late')
            finally:
                process=agent.process
                await agent.close()
                self.assertIsNotNone(process.returncode)
        with tempfile.TemporaryDirectory() as raw:
            asyncio.run(exercise(make_config(Path(raw))))

    def test_failed_steer_is_not_reported_as_delivered(self):
        async def exercise(config):
            agent=LiveAgent(config, lambda *_: None, command=[sys.executable,'-u','-c',SERVER])
            try:
                await agent.start()
                task=asyncio.create_task(agent.run('work'))
                await asyncio.wait_for(agent._ready.wait(),2)
                with self.assertRaisesRegex(LiveAgentError,'turn mismatch'):
                    await agent.steer('reject')
                await agent.interrupt()
                self.assertEqual((await task)['status'],'interrupted')
            finally:
                await agent.close()
        with tempfile.TemporaryDirectory() as raw:
            asyncio.run(exercise(make_config(Path(raw))))
