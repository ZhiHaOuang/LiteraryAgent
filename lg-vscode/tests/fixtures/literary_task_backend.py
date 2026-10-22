"""Real LG stdio Backend + Native HTTP; only the model provider is synthetic."""
import asyncio
import json
import sys
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from lg_cli.backend_stdio import serve
from lg_cli.project_store import ProjectStore
from tests.helpers import make_config
from tests.test_execution_adapter import specimen

root = Path(sys.argv[1])
store = ProjectStore(root)
store.initialize(name='Frontend delivery fixture')
store.create_document(kind='chapter', slug='one', title='One', content='Prefix. SELECTED. Suffix.',
    state='accepted', sequence=1)


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def do_POST(self):
        if self.path != '/responses':
            self.send_response(404); self.end_headers(); return
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        with (root / 'http-requests.jsonl').open('a') as output:
            output.write(json.dumps(request) + '\n')
        schema = request['text']['format']['schema']
        payload = specimen(schema)
        payload.update(revision_markdown='REVISED.', change_log=['Polished the selection.'], memory_proposals=[],
            risks=[], book_overview='维修师在浮城寻找失灵闸门背后的真相。')
        text = json.dumps(payload)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        for event in [{'type': 'response.output_text.delta', 'delta': text},
            {'type': 'response.completed', 'response': {'status': 'completed', 'output': [
                {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text}]}]}}]:
            self.wfile.write(('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n').encode())
        self.wfile.flush()


server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
config = replace(make_config(root), runtime_engine='native', provider='openai', protocol='responses',
    default_model='synthetic-frontend', writer_model='', critic_model='',
    base_url=f'http://127.0.0.1:{server.server_port}', timeout_seconds=8, max_stage_context_chars=200000)
try:
    asyncio.run(serve(config))
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
