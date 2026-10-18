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
    def test_host_rejections_reply_once_and_leave_the_wire_usable(self):
        server = r'''
import json, sys
def send(value):
 print(json.dumps(value), flush=True)
methods = ['item/commandExecution/requestApproval', 'item/fileChange/requestApproval',
           'item/permissions/requestApproval', 'item/tool/requestUserInput',
           'mcpServer/elicitation/request', 'item/commandExecution/requestApproval']
responses = []
for line in sys.stdin:
 r = json.loads(line)
 if 'method' not in r:
  responses.append(r)
  if len(responses) == len(methods):
   send({'method':'turn/completed', 'params':{'threadId':'thread-main', 'turn':{
    'id':'turn-1', 'status':'completed', 'items':[{'type':'agentMessage',
    'text':json.dumps(responses)}]}}})
  continue
 if 'id' not in r: continue
 method = r['method']
 result = {'thread':{'id':'thread-main'}} if method == 'thread/start' else {}
 if method == 'turn/start': result = {'turn':{'id':'turn-1'}}
 send({'id':r['id'], 'result':result})
 if method == 'turn/start':
  for i, method in enumerate(methods):
   send({'id':'approval-'+str(i), 'method':method, 'params':{'reject':i < 5}})
'''
        async def exercise(config):
            async def approve(method, params):
                if params['reject']:
                    raise ValueError('PRIVATE_POLICY_DETAIL')
                return {'decision': 'decline'}
            agent = LiveAgent(config, lambda *_: None, on_request=approve,
                              command=[sys.executable, '-u', '-c', server])
            try:
                await agent.start()
                turn = await asyncio.wait_for(agent.run('check host refusal'), 3)
                replies = json.loads(turn['items'][0]['text'])
                self.assertEqual(len(replies), 6)
                self.assertEqual({r['id'] for r in replies}, {'approval-'+str(i) for i in range(6)})
                for reply in replies:
                    if reply['id'] == 'approval-5':
                        self.assertEqual(reply['result'], {'decision': 'decline'})
                    else:
                        self.assertNotIn('result', reply)
                        self.assertEqual(reply['error'], {'code': -32000, 'message': 'Request rejected by host policy'})
                self.assertNotIn('PRIVATE_POLICY_DETAIL', json.dumps(replies))
                await agent.request('thread/read', {'threadId': 'thread-main'})
            finally:
                await agent.close()
        with tempfile.TemporaryDirectory() as raw:
            asyncio.run(exercise(make_config(Path(raw))))

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
