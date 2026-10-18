"""Real LG stdio and selected runtime; local model and historical source fixtures."""
import json
import sys
import threading
from contextlib import ExitStack
from dataclasses import replace
from functools import partial
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from lg_cli.backend import Backend
from lg_cli.backend_stdio import main
from lg_cli.init_project import init_workspace
from lg_cli.project_store import ProjectStore
from tests.helpers import make_config
from tests.test_legacy_sessions import FakeReader
from tests.intent_fixture import current_user, routed_reply

root=Path(sys.argv[sys.argv.index('--workspace')+1])
engine=sys.argv[sys.argv.index('--runtime-engine')+1] if '--runtime-engine' in sys.argv else 'native'
init_workspace(root)
store=ProjectStore(root)
if not store.list_documents(kind='chapter'):
    store.create_document(kind='chapter',slug='one',title='原稿',content='作者的正式正文。',state='accepted')
home=root/'.literarygiant'/'codex-home';home.mkdir(exist_ok=True)
(home/'state_5.sqlite').touch()
thread='01900000-2222-7777-aaaa-bbbbbbbbbbbb'
FakeReader.threads={thread:({'id':thread,'cwd':str(root),'name':'旧 Extension 规划','updatedAt':123},[
    {'id':'old-turn','status':'inProgress','items':[
        {'id':'author','type':'userMessage','content':[{'type':'text','text':'LEGACY_AUTHOR：只规划。'}]},
        {'id':'private','type':'reasoning','text':'PRIVATE_REASONING'},
        {'id':'tool','type':'mcpToolCall','result':'PRIVATE_TOOL_OUTPUT'},
        {'id':'assistant','type':'agentMessage','text':'旧助手规划。'+'长历史中文🖋'*140000+'旧助手规划结束。'}]}])}

class Model(BaseHTTPRequestHandler):
    def log_message(self,*_):pass
    def do_POST(self):
        request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        routed = routed_reply(current_user(request.get('input') or []))
        if routed is not None:
            text = json.dumps(routed,ensure_ascii=False)
        else:
            text='Native 接续回复。'
            if '旧助手规划结束。' not in json.dumps(request,ensure_ascii=False):text='Missing imported context'
        response={'status':'completed','output':[{'type':'message','role':'assistant',
            'content':[{'type':'output_text','text':text}]}]}
        self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
        self.wfile.write(('data: '+json.dumps({'type':'response.completed','response':response})+'\n\n').encode())

server=ThreadingHTTPServer(('127.0.0.1',0),Model)
worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
config=replace(make_config(root),runtime_engine='native',protocol='responses',default_model='synthetic-native',
    base_url=f'http://127.0.0.1:{server.server_port}',runtime_manifest=None,environment='legacy-fixture-no-profile')
try:
    with ExitStack() as stack:
        stack.enter_context(patch('lg_cli.backend_stdio.Backend',partial(Backend,legacy_reader_factory=FakeReader)))
        if engine=='codex':
            from lg_cli.anthropic_bridge import AnthropicBridge
            from tests.test_anthropic_bridge import message_events
            config=replace(config,provider='stepfun',protocol='anthropic',default_model='synthetic-codex')
            def output(payload):
                routed=routed_reply(current_user(payload.get('messages') or []))
                text=json.dumps(routed,ensure_ascii=False) if routed is not None else (
                    'Codex 接续回复。' if '旧助手规划结束。' in json.dumps(payload,ensure_ascii=False) else 'Missing imported context')
                yield from message_events(delta={'type':'text_delta','text':text})
            stack.enter_context(patch('lg_cli.live_agent.provider_bridge',
                side_effect=lambda cfg:AnthropicBridge(cfg,stream_factory=output)))
        else:
            stack.enter_context(patch('lg_cli.live_agent.LiveAgent',side_effect=AssertionError('Native must not invoke Codex')))
        stack.enter_context(patch('lg_cli.backend_stdio.load_config',return_value=config))
        main()
finally:
    server.shutdown();server.server_close();worker.join(timeout=2)
