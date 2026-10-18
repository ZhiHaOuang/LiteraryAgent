"""The real SDK must honor LG's direct transport despite proxy environment variables."""
import json
import os
import socket
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from lg_cli.anthropic_bridge import AnthropicBridge
from tests.helpers import make_config
from tests.test_anthropic_bridge import message_events


class AnthropicProxyTests(unittest.TestCase):
    def test_real_sdk_completes_directly_without_changing_proxy_environment(self):
        proxy_requests, upstream_requests = [], []

        class Proxy(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', '0')))
                proxy_requests.append(self.path)
                body = b'{"error":{"type":"api_error","message":"Synthetic local proxy"}}'
                self.send_response(503)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                upstream_requests.append((self.path, payload))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                for event in message_events(delta={'type': 'text_delta', 'text': 'LOCAL_MESSAGES_COMPLETED'}):
                    if event['type'] == 'message_start':
                        event['message'].update(id='local-message', type='message', role='assistant',
                            content=[], model=payload['model'], stop_reason=None, stop_sequence=None)
                    self.wfile.write(('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n').encode())

        servers = [ThreadingHTTPServer(('127.0.0.1', 0), handler) for handler in (Proxy, Upstream)]
        threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
        for server, thread in zip(servers, threads):
            thread.start()
            self.addCleanup(thread.join, 2)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        config = replace(make_config(Path(temporary.name)), provider='stepfun', protocol='anthropic',
            base_url=f'http://127.0.0.1:{servers[1].server_port}', default_model='local-synthetic',
            api_key='SYNTHETIC_LOCAL_ONLY', timeout_seconds=5)
        proxy_url = f'http://127.0.0.1:{servers[0].server_port}'
        request = {'model': config.default_model, 'instructions': 'Local synthetic transport check.',
            'input': [{'role': 'user', 'content': 'Return the local fixture text.'}], 'tools': [], 'stream': True}
        proxy_keys = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy',
                      'NO_PROXY', 'no_proxy')
        connect = socket.socket.connect

        def local_connect(sock, address):
            if not isinstance(address, tuple) or address[0] not in {'127.0.0.1', '::1'}:
                raise AssertionError('Only loopback sockets are allowed in this fixture')
            return connect(sock, address)

        for lower in (False, True):
            for all_only in (False, True):
                with self.subTest(case='lower' if lower else 'upper', all_only=all_only):
                    names = ('ALL_PROXY',) if all_only else ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY')
                    environment = {(name.lower() if lower else name): proxy_url for name in names}
                    environment.update(NO_PROXY='', no_proxy='')
                    proxy_count, upstream_count = len(proxy_requests), len(upstream_requests)
                    events, error = [], None
                    with patch.dict(os.environ, environment, clear=True), \
                            patch('socket.socket.connect', new=local_connect):
                        before = {name: os.environ.get(name) for name in proxy_keys}
                        bridge = AnthropicBridge(config)
                        bridge.client = bridge.open_client()
                        try:
                            events = list(bridge.response_events(request))
                        except Exception as exc:
                            error = type(exc).__name__
                        finally:
                            bridge.client.close()
                        self.assertEqual({name: os.environ.get(name) for name in proxy_keys}, before)
                    self.assertEqual(len(proxy_requests) - proxy_count, 0,
                        'The direct SDK transport unexpectedly contacted the synthetic proxy')
                    self.assertIsNone(error, 'The real SDK did not complete: ' + str(error))
                    self.assertEqual(len(upstream_requests) - upstream_count, 1)
                    path, payload = upstream_requests[-1]
                    self.assertEqual(path, '/v1/messages')
                    self.assertEqual(payload['model'], config.default_model)
                    self.assertEqual(events[-1]['type'], 'response.completed')
                    response = events[-1]['response']
                    self.assertEqual(response['status'], 'completed')
                    text = ''.join(part.get('text', '') for item in response['output']
                        for part in item.get('content', []) if part.get('type') == 'output_text')
                    self.assertEqual(text, 'LOCAL_MESSAGES_COMPLETED')
