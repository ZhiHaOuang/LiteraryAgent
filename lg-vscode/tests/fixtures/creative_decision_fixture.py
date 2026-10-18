"""Real stdio/SDK/MCP journey; only the local model responses are synthetic."""
import asyncio
import ipaddress
import json
import socket
import sys
import threading
import uuid
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from lg_cli.backend_stdio import serve
from lg_cli.conversation_store import ConversationStore
from lg_cli.init_project import init_workspace
from lg_cli.project_store import ProjectStore
from lg_cli.session_runtime import SessionRuntime
from tests.helpers import make_config
from tests.intent_fixture import current_user
from tests.test_anthropic_bridge import message_events


root, engine = Path(sys.argv[1]), sys.argv[2]
init_workspace(root)
saved = ProjectStore(root)
chapter = saved.create_document(kind='chapter', slug='one', title='原稿',
    content='前文保持不动。\n\n作者已采纳的正文。\n\n后文保持不动。', state='accepted', sequence=1)
saved.create_document(kind='chapter', slug='two', title='无关章节',
    content='无关章节保持不动。', state='accepted', sequence=2)
saved.set_fact(category='world', key='river', value='河流向北。',
    source_document='one', source_version=chapter.active_version_number)
options = [{'id': key, 'title': title, 'content': content} for key, title, content in (
    ('A', '逃离', '第一组：渡船人协助主角离开。'),
    ('B', '留下', '第一组：主角留下揭穿阴谋。'),
    ('C', '谈判', '第一组：主角与对手家族谈判。'))]
first = saved.save_decision_options(slug='ending-choice', title='结局方向',
    topic={'scope':'chapter', 'document_id':chapter.id, 'key':'ending'}, options=options,
    sources=[{'document_id':chapter.id, 'version_id':chapter.active_version_id}],
    request='提供三个真正不同的结局；未经作者选择不生效。', expected_version_id=0)
saved.save_decision_options(slug='ending-choice', title='结局方向', topic=first['topic'],
    options=[{**item, 'content':item['content'].replace('第一组', '第二组')} for item in options],
    sources=first['sources'], request=first['request'], expected_version_id=first['version_id'])
session = ConversationStore(root).create()
runtime = SessionRuntime(root, session)
runtime.acquire()
try:
    runtime.update(mode='full_access', creative_mode='ask')
finally:
    runtime.release()

# A fixture configuration error must fail locally rather than reaching an external model.
original_connect = socket.socket.connect
def local_connect(connection, address):
    if connection.family in {socket.AF_INET, socket.AF_INET6}:
        if not ipaddress.ip_address(address[0]).is_loopback:
            raise AssertionError('Creative-decision fixture only permits loopback sockets')
    return original_connect(connection, address)
socket.socket.connect = local_connect
seen = set()
errors = []


class Model(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        try:
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            text = current_user(payload.get('messages', []))
            marker = text.index('Host-enforced author task;')
            task, _ = json.JSONDecoder().raw_decode(text[text.index('{', marker):])
            assert task['kind'] == 'decision'
            if task['taskId'] not in seen:
                selecting = task['decisionAction'] == 'select'
                description = ('Select exactly one saved option' if selecting else
                    'Withdraw the current official creative choice')
                tool = next(item['name'] for item in payload['tools']
                    if description in item.get('description', ''))
                seen.add(task['taskId'])
                arguments = {'slug':task['document'], 'expected_version_id':task['sourceVersion']}
                if selecting:
                    arguments.update(options_version_number=task['versionNumber'], option_id=task['optionId'])
                with (root/'model-calls.jsonl').open('a') as stream:
                    stream.write(json.dumps({'task':task, 'tool':tool, 'arguments':arguments}, ensure_ascii=False)+'\n')
                events = message_events(block={'type':'tool_use', 'id':uuid.uuid4().hex, 'name':tool, 'input':{}},
                    delta={'type':'input_json_delta', 'partial_json':json.dumps(arguments, ensure_ascii=False)}, stop='tool_use')
            else:
                events = message_events(delta={'type':'text_delta', 'text':'创作方向操作结果已核对。'})
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            for event in events:
                if event['type'] == 'message_start':
                    event['message'].update(id='msg_'+uuid.uuid4().hex, type='message', role='assistant',
                        model=payload['model'], content=[], stop_reason=None, stop_sequence=None)
                self.wfile.write(('event: '+event['type']+'\ndata: '+json.dumps(event, ensure_ascii=False)+'\n\n').encode())
            self.wfile.flush()
        except Exception as exc:
            errors.append(type(exc).__name__+': '+str(exc))
            (root/'provider-errors.json').write_text(json.dumps(errors, ensure_ascii=False))
            self.send_error(500, 'Synthetic response contract failed')


server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
worker = threading.Thread(target=server.serve_forever, daemon=True)
worker.start()
config = replace(make_config(root), runtime_engine=engine, provider='stepfun', protocol='anthropic',
    default_model='creative-'+engine, api_key='synthetic-local-only',
    base_url=f'http://127.0.0.1:{server.server_port}', enable_reference=False, timeout_seconds=60,
    runtime_manifest=Path(sys.argv[3]) if engine == 'codex' else None)
try:
    asyncio.run(serve(config))
finally:
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)
