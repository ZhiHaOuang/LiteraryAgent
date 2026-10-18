"""Actual async HTTP transports against synthetic local SSE providers."""
import asyncio
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from lg_cli.native_model import NativeModel
from lg_cli.anthropic_bridge import ProtocolError
from tests.helpers import make_config
from tests.test_anthropic_bridge import message_events


class NativeModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_responses_and_anthropic_use_real_http_streams(self):
        for protocol in ('responses', 'anthropic'):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as raw:
                seen, deltas = [], []
                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *_):
                        pass
                    def do_POST(self):
                        expected = '/v1/messages' if protocol == 'anthropic' else '/responses'
                        if self.path != expected:
                            self.send_response(404);self.end_headers();return
                        seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/event-stream')
                        self.end_headers()
                        events = list(message_events(delta={'type':'text_delta','text':'真实 HTTP 本地回复。'})) if protocol == 'anthropic' else [
                            {'type':'response.output_text.delta','delta':'真实 HTTP 本地回复。'},
                            {'type':'response.completed','response':{'status':'completed','output':[
                                {'type':'message','role':'assistant','content':[{'type':'output_text','text':'真实 HTTP 本地回复。'}]}],
                                'usage':{'input_tokens':10,'output_tokens':5}}}]
                        for event in events:
                            self.wfile.write(('event: '+event['type']+'\ndata: '+json.dumps(event,ensure_ascii=False)+'\n\n').encode())
                        self.wfile.flush()
                server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    config = replace(make_config(Path(raw)), protocol=protocol, provider='stepfun' if protocol == 'anthropic' else 'openai',
                        default_model='synthetic', base_url=f'http://127.0.0.1:{server.server_port}', max_output_tokens=2048)
                    try:
                        result = await NativeModel(config).generate({'model':'synthetic','input':[{'role':'user','content':'只讨论'}],
                            'instructions':'只返回一句话','tools':[],'stream':True}, deltas.append)
                    except ProtocolError as exc:
                        context = exc.__context__
                        self.fail(f'HTTP stream failed: {type(context).__name__}, status={getattr(context,"status_code",None)}, local_requests={len(seen)}')
                    self.assertEqual(result['status'], 'completed')
                    self.assertEqual(''.join(deltas), '真实 HTTP 本地回复。')
                    self.assertEqual(len(seen), 1)
                finally:
                    await asyncio.to_thread(server.shutdown)
                    server.server_close()
                    worker.join(timeout=2)

    async def test_http_failure_does_not_expose_upstream_body(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                self.send_response(401);self.end_headers()
                self.wfile.write(b'SENSITIVE_TEST_BODY')
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True);worker.start()
        try:
            with tempfile.TemporaryDirectory() as raw:
                config = replace(make_config(Path(raw)), protocol='responses', base_url=f'http://127.0.0.1:{server.server_port}')
                with self.assertRaises(ProtocolError) as error:
                    await NativeModel(config).generate({'model':'synthetic','input':[],'stream':True}, lambda _:None)
                self.assertNotIn('SENSITIVE_TEST_BODY', str(error.exception))
        finally:
            await asyncio.to_thread(server.shutdown);server.server_close();worker.join(timeout=2)
