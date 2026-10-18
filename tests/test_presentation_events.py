"""Public progress survives runtime adaptation; private fields never cross it."""
import json
import unittest
from types import SimpleNamespace

from lg_cli.codex_events import CodexEvents
from lg_cli.live_view import LiveView
from lg_cli.terminal_backend import TerminalBackendClient


class Session:
    def __init__(self):
        self.tasks, self.blocks = {}, {}
        self.app = SimpleNamespace(invalidate=lambda: None)

    def stream_message(self, key, text, *, replace=False, role='assistant'):
        self.blocks[key] = (role, text if replace else self.blocks.get(key, (role,''))[1] + text)


class PresentationTests(unittest.TestCase):
    def setUp(self):
        self.records = []
        self.codec = CodexEvents(lambda m,p:self.records.append({'type':m, 'runId':'runtime', **p}), lambda:'main')

    def send(self, method, **params):
        self.codec(method, {'threadId':'main', 'turnId':'turn', **params})

    def test_dynamic_review_displays_status_without_publishing_raw_expert_payload(self):
        for method, status, success, expected in [
            ('item/started', 'inProgress', None, 'running'),
            ('item/completed', 'completed', True, 'completed'),
            ('item/completed', 'completed', False, 'failed')]:
            self.send(method, item={'id': 'review', 'type': 'dynamicToolCall', 'tool': 'delegate_review',
                'status': status, 'success': success, 'arguments': {'focus': 'PRIVATE_FOCUS'},
                'contentItems': [{'type': 'inputText', 'text': 'PRIVATE_EXPERT_RESULT'}]})
            self.assertEqual(self.records[-1]['item']['state'], expected)
            self.assertEqual(self.records[-1]['item']['title'], '独立专家审读')
        self.assertNotIn('PRIVATE_', json.dumps(self.records))

    def test_public_summaries_commands_and_failures_keep_only_public_fields(self):
        self.send('item/reasoning/textDelta', itemId='r', delta='PRIVATE_REASONING')
        self.send('item/reasoning/summaryTextDelta', itemId='r', summaryIndex=1, delta='第二项')
        self.send('item/reasoning/summaryTextDelta', itemId='r', summaryIndex=0, delta='第一项')
        self.assertEqual(self.records[-1]['item']['text'], '第一项\n第二项')
        self.send('item/commandExecution/outputDelta', itemId='cmd', delta='partial output')
        self.send('item/completed', item={'id':'cmd','type':'commandExecution','command':'example',
                                        'status':'declined', 'exitCode':None})
        self.assertEqual(self.records[-1]['item']['state'], 'cancelled')
        self.assertEqual(self.records[-1]['item']['text'], 'partial output')
        self.send('item/completed', item={'id':'tool','type':'mcpToolCall','status':'failed',
            'error':{'message':'PRIVATE_ERROR'}, 'result':{'content':[{'type':'text','text':'PRIVATE_ERROR'}]}})
        self.assertEqual(self.records[-1]['item']['state'], 'failed')
        self.assertNotIn('PRIVATE_', json.dumps(self.records))

    def test_only_registered_children_have_tasks_and_errors_are_redacted(self):
        self.send('item/completed', threadId='unknown', item={'id':'hidden','type':'agentMessage','text':'UNKNOWN_CHILD'})
        self.assertEqual(self.records, [])
        self.send('item/started', item={'id':'spawn','type':'subAgentActivity','kind':'started',
                                      'agentThreadId':'child','agentPath':'/main/critic'})
        self.send('item/completed', threadId='child', item={'id':'answer','type':'agentMessage','text':'审读结论'})
        self.assertEqual(self.records[-1]['item']['structured']['task']['details'], ['审读结论'])
        self.send('turn/completed', threadId='child', turn={'status':'failed','error':{'message':'PRIVATE_ERROR'}})
        self.assertEqual(self.records[-1]['item']['state'], 'failed')
        self.assertNotIn('PRIVATE_ERROR', json.dumps(self.records))
        self.codec.begin()
        self.send('item/completed', threadId='child', item={'id':'late','type':'agentMessage','text':'LATE_CHILD'})
        self.assertNotIn('LATE_CHILD', json.dumps(self.records))

    def test_subagent_completion_and_missing_status_have_product_terminal_states(self):
        self.send('item/completed', item={'id':'complete','type':'subAgentActivity','kind':'completed',
                                         'agentThreadId':'child','agentPath':'/main/critic'})
        self.assertEqual(self.records[-1]['item']['state'], 'completed')
        for wire, expected in [('pendingInit','waiting'), ('notFound','failed')]:
            self.send('item/completed', item={'id':'collab','type':'collabAgentToolCall',
                'receiverThreadIds':['other'], 'agentsStates':{'other':{'status':wire}}})
            self.assertEqual(self.records[-1]['item']['state'], expected)

    def test_unconfirmed_children_do_not_become_success_when_main_ends(self):
        self.send('item/started', item={'id':'spawn','type':'subAgentActivity','kind':'started',
                                      'agentThreadId':'child','agentPath':'/main/critic'})
        self.codec.finish()
        self.assertEqual(self.records[-1]['item']['state'], 'cancelled')
        count = len(self.records)
        self.send('item/agentMessage/delta', itemId='late', delta='late')
        self.assertEqual(len(self.records), count)

    def test_requested_turn_binding_rejects_old_output_arriving_first(self):
        records = []
        codec = CodexEvents(lambda m,p:records.append((m,p)), lambda:'main', expected_turn=lambda:None)
        for turn, text in [('old','OLD_ANSWER'), ('new','NEW_ANSWER')]:
            codec('item/agentMessage/delta', {'threadId':'main','turnId':turn,'itemId':'a','delta':text})
        self.assertEqual(records, [])
        codec('lg.turnBound', {'threadId':'main','turnId':'new'})
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0][1]['delta'], 'NEW_ANSWER')

    def test_reused_child_gets_new_task_generation_and_rejects_old_turn(self):
        session = Session()
        view = LiveView(session)
        self.send('item/started', item={'id':'spawn','type':'subAgentActivity','kind':'started',
                                      'agentThreadId':'child','agentPath':'/main/critic'})
        self.send('turn/completed', threadId='child', turnId='child-one', turn={'status':'completed'})
        self.send('item/completed', item={'id':'again','type':'collabAgentToolCall','tool':'followupTask',
                  'receiverThreadIds':['child'], 'agentsStates':{'child':{'status':'running'}}})
        self.send('turn/completed', threadId='child', turnId='child-one', turn={'status':'completed'})
        self.assertEqual(self.records[-1]['item']['state'], 'running')
        self.send('item/completed', threadId='child', turnId='child-two',
                  item={'id':'answer','type':'agentMessage','text':'第二次审读'})
        self.send('turn/completed', threadId='child', turnId='child-two', turn={'status':'failed'})
        for record in self.records:
            view('lg.event', record)
        self.assertEqual([t['status'] for t in session.tasks.values()], ['completed','failed'])
        self.assertEqual(list(session.tasks.values())[-1]['details'], ['第二次审读'])

    def test_queue_only_message_does_not_invent_another_child_run(self):
        self.send('item/completed', item={'id':'done','type':'subAgentActivity','kind':'completed',
                                        'agentThreadId':'child','agentPath':'/main/critic'})
        key = self.records[-1]['item']['id']
        self.send('item/completed', item={'id':'message','type':'subAgentActivity','kind':'interacted',
                                        'agentThreadId':'child','agentPath':'/main/critic'})
        self.assertEqual(self.records[-1]['item']['id'], key)
        self.assertEqual(self.records[-1]['item']['state'], 'completed')
        self.codec.finish()
        self.assertEqual(self.records[-1]['item']['state'], 'completed')

    def test_unseen_old_child_completion_cannot_replace_the_active_turn(self):
        self.send('item/started', item={'id':'spawn','type':'subAgentActivity','kind':'started',
                                      'agentThreadId':'child','agentPath':'/main/critic'})
        self.send('item/completed', threadId='child', turnId='current',
                  item={'id':'answer','type':'agentMessage','text':'当前结果'})
        count = len(self.records)
        self.send('turn/completed', threadId='child', turnId='unseen-old', turn={'status':'completed'})
        self.assertEqual(len(self.records), count)
        self.send('turn/completed', threadId='child', turnId='current', turn={'status':'failed'})
        self.assertEqual(self.records[-1]['item']['state'], 'failed')

    def test_search_details_and_cancelled_tool_match_public_snapshot(self):
        self.send('item/completed', item={'id':'search','type':'webSearch','query':'public query',
                  'action':{'type':'search','query':'public query','private':'PRIVATE_FIELD'}})
        self.assertEqual(self.records[-1]['item']['kind'], 'tool')
        self.assertEqual(self.records[-1]['item']['structured']['action']['query'], 'public query')
        self.assertNotIn('PRIVATE_FIELD', json.dumps(self.records))
        session = Session()
        view = LiveView(session)
        view('lg.event', {'type':'item.updated','item':{'runId':'run','id':'tool','kind':'tool',
            'title':'操作','text':'partial','state':'running'}})
        view('lg.event', {'type':'run.finished','runId':'run','status':'cancelled'})
        self.assertIn('已取消', next(iter(session.blocks.values()))[1])
        self.assertNotIn('进行中', next(iter(session.blocks.values()))[1])

    def test_generic_view_namespaces_ids_and_ignores_late_output(self):
        session = Session()
        view = LiveView(session)
        for run, text in [('one','第一轮'), ('two','第二轮')]:
            view('lg.event', {'type':'assistant.delta','runId':run,'itemId':'answer','delta':text})
            view('lg.event', {'type':'item.updated','item':{'id':'answer','runId':run,
                'kind':'assistant','title':'LG','text':text,'state':'completed'}})
            view('lg.event', {'type':'assistant.delta','runId':run,'itemId':'answer','delta':'LATE'})
        self.assertEqual(sorted(text for _,text in session.blocks.values()), ['第一轮','第二轮'])
        view('lg.event', {'type':'run.finished','runId':'two','status':'completed'})
        view('lg.event', {'type':'assistant.delta','runId':'two','itemId':'new','delta':'LATE_NEW'})
        view('item/agentMessage/delta', {'itemId':'raw','delta':'RAW'})
        self.assertEqual(len(session.blocks), 2)

    def test_terminal_forwards_product_events_without_an_engine_attribute(self):
        client = object.__new__(TerminalBackendClient)
        received = []
        client.on_event = lambda _:None
        client.on_notice = lambda _:None
        client.view = lambda m,p:received.append((m,p))
        event = {'type':'assistant.delta','runId':'future-run','itemId':'a','delta':'hello'}
        client._event({'method':'client/event','params':event})
        client._event({'method':'engine/event','params':{'method':'private','params':{'raw':'PRIVATE'}}})
        self.assertEqual(received, [('lg.event',event)])
