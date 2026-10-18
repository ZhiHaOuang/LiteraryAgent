"""LG-owned runtime + real HTTP/SSE + real writing MCP, with synthetic model data."""
import asyncio
import json
import os
import sys
import threading
import uuid
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from lg_cli.backend_stdio import serve
from lg_cli.init_project import init_workspace
from lg_cli.project_store import ProjectStore
from tests.helpers import make_config
from tests.intent_fixture import routed_reply
from tests.test_execution_adapter import specimen


# Explicit opt-in keeps the older protocol/approval fixtures unchanged.
MATRIX = os.environ.get('LG_TEST_TASK_MATRIX') == '1'
MATRIX_ORIGINAL = '前文保持不动。\n\n待修改片段。\n\n后文保持不动。'
matrix_steps = {}


def matrix_initialize(root):
    from lg_cli.project_store import ProjectStore
    from lg_cli.supervision import configure_supervision
    saved = ProjectStore(root)
    if not saved.list_documents(kind='chapter'):
        doc = saved.create_document(kind='chapter', slug='one', title='选区原稿',
            content=MATRIX_ORIGINAL, state='accepted', sequence=1)
        saved.create_document(kind='chapter', slug='two', title='无关章节',
            content='无关章节正文保持不动。', state='accepted', sequence=2)
        saved.set_fact(category='world', key='matrix-rule', value='闸门由机械锁控制。',
            source_document='one', source_version=doc.active_version_number)
    configure_supervision(saved)


def matrix_route(text):
    from lg_cli.intent_router import MARKER, proposal
    if not text.startswith(MARKER):
        return None
    value = json.loads(text.split('INPUT_JSON:\n', 1)[1])
    if 'MATRIX:revision' not in value['author']:
        return None
    target = next(d['target'] for d in value['context']['saved']['documents'] if d['slug'] == 'one')
    return proposal(kind='revision', target=target, passage='待修改片段。')


def matrix_values(value):
    # MCP result text can contain a JSON-encoded structuredContent envelope.
    if isinstance(value, str):
        try:
            yield from matrix_values(json.loads(value))
        except (ValueError, TypeError):
            pass
    elif isinstance(value, dict):
        yield value
        for child in value.values():
            yield from matrix_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from matrix_values(child)


def matrix_operation(root, text, inputs):
    from lg_cli.project_store import ProjectStore
    from tests.helpers import reference_card
    from lg_cli.book_analysis import CATEGORIES
    # Tool-result messages are not new author turns. Repeated adoption prompts are.
    count = sum(1 for item in inputs if item.get('role') == 'user' and
        (isinstance(item.get('content'), str) or any(part.get('type') in {'text', 'input_text'}
            for part in item.get('content', []) if isinstance(part, dict))))
    key = (text, count)
    step = matrix_steps.get(key, 0)
    matrix_steps[key] = step + 1
    saved = ProjectStore(root)
    active = saved.get_document('one').active_version_id
    if 'MATRIX:plan' in text and step == 0:
        return 'studio_save', {'stage':'plan', 'content':'MATRIX_PLAN：维修师先定位故障，再决定救援顺序。',
            'updates':{'volumes':['MATRIX_PLAN：星港机械悬疑卷']}, 'expected_profile_version':0}
    if 'MATRIX:revision' in text and step == 0:
        replacement = '第一次选区候选。' if 'revision-a' in text else '第二次选区候选。'
        return 'edit_manuscript', {'slug':'one', 'content':MATRIX_ORIGINAL.replace('待修改片段。', replacement),
            'expected_version_id':active, 'reason':'作者指定选区修订',
            'book_overview':'维修师在星港追查机械闸门故障。'}
    if '作者要求审阅 one' in text:
        if step == 0:
            return 'edit_manuscript', {'slug':'one', 'content':'审读范围内禁止的正文改写。',
                'expected_version_id':active, 'reason':'verify review-only scope',
                'book_overview':'维修师在星港追查机械闸门故障。'}
        if step == 1:
            return 'studio_review_context', {'slug':'one', 'version_number':2}
        if step == 2:
            contexts = [v for v in matrix_values(inputs) if {'snapshot','source_spans','sourceFingerprint','schemas'} <= v.keys()]
            context = contexts[-1]
            selected = saved.get_version('one', 2)
            assert context['snapshot']['documents'][0]['version_id'] == selected.id
            assert context['snapshot']['documents'][0]['content'] == selected.content
            span = next(key for key, value in context['source_spans'].items()
                if value['version_id'] == selected.id and '第一次选区候选' in value['quote'])
            (root/'matrix-review-source.json').write_text(json.dumps(context, ensure_ascii=False))
            reports = {category:{'summary':'MATRIX_REVIEW_V2_'+category,
                'entries':[{'key':'matrix-'+category, 'label':'选区候选审读', 'analysis':'仅审读选中候选的已提供证据。',
                    'instance_card':reference_card(category), 'evidence':[{'span_id':span}]}], 'findings':[]}
                for category in CATEGORIES}
            return 'studio_review', {'slug':'one', 'version_number':2,
                'source_fingerprint':context['sourceFingerprint'], 'reports':reports}
    if '作者要求采用 one 的候选 v2' in text and step == 0:
        return 'studio_accept', {'slug':'one', 'version_number':2, 'expected_version_id':active}
    return None


root = Path(sys.argv[1])
init_workspace(root)
store = ProjectStore(root)
if MATRIX:
    matrix_initialize(root)
elif not store.list_documents(kind='chapter'):
    store.create_document(kind='chapter', slug='one', title='原稿', content='作者已采纳的原稿。', state='accepted',sequence=1)

if '--init-only' in sys.argv:
    from lg_cli.config import project_config_path
    with project_config_path(root).open('a') as stream:
        stream.write('\n[runtime]\nengine = "native"\n')
    raise SystemExit(0)

if os.environ.get('LG_TEST_DELEGATION') == '1':
    store.create_version('one',content='待审读候选：林砚隔离电源后救援。',
        expected_version_id=store.get_document('one').active_version_id)


class Model(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        inputs = request.get('input') or []
        latest = next((i.get('content', '') for i in reversed(inputs) if i.get('role') == 'user'), '')
        routed=matrix_route(latest) if MATRIX else routed_reply(latest)
        operation=matrix_operation(root,latest,inputs) if MATRIX and routed is None else None
        if 'Expert role: ' in request.get('instructions',''):
            assert request['tools'] == []
            output=[{'type':'message','role':'assistant','content':[{'type':'output_text',
                'text':json.dumps(specimen(request['text']['format']['schema']),ensure_ascii=False)}]}]
        elif routed is not None:
            output=[{'type':'message','role':'assistant','content':[{'type':'output_text','text':json.dumps(routed,ensure_ascii=False)}]}]
        elif MATRIX:
            output = ([{'type':'function_call','call_id':uuid.uuid4().hex,'name':operation[0],
                'arguments':json.dumps(operation[1],ensure_ascii=False)}] if operation else
                [{'type':'message','role':'assistant','content':[{'type':'output_text','text':'MATRIX 工具执行已核对。'}]}])
        elif inputs and inputs[-1].get('type') == 'function_call_output':
            output = [{'type':'message','role':'assistant','content':[{'type':'output_text','text':'Native 工具结果已核对。'}]}]
        elif 'CASE:delegate' in latest:
            output=[{'type':'function_call','call_id':uuid.uuid4().hex,'name':'delegate_review',
                'arguments':json.dumps({'experts':['continuity','critic']})}]
        elif 'CASE:edit' in latest or 'CASE:ambiguous' in latest:
            output = [{'type':'function_call','call_id':uuid.uuid4().hex,'name':'edit_manuscript',
                'arguments':json.dumps({'slug':'one','content':'Native 提出的候选正文。',
                    'expected_version_id':store.get_document('one').active_version_id,'reason':'作者要求候选',
                    'book_overview':'维修师在星港寻找闸门故障的原因。'})}]
        elif 'CASE:deny' in latest:
            output = [{'type':'function_call','call_id':uuid.uuid4().hex,'name':'update_book_overview',
                'arguments':json.dumps({'text':'拒绝后不得保存的作品概况。'})}]
        else:
            output = [{'type':'message','role':'assistant','content':[{'type':'output_text','text':'Native 流式回复。'}]}]
        events = []
        if output[0]['type'] == 'message':
            events.append({'type':'response.output_text.delta','delta':output[0]['content'][0]['text']})
        events.append({'type':'response.completed','response':{'status':'completed','output':output,
            'usage':{'input_tokens':10,'output_tokens':5}}})
        self.send_response(200)
        self.send_header('Content-Type','text/event-stream')
        self.end_headers()
        for event in events:
            self.wfile.write(('event: '+event['type']+'\ndata: '+json.dumps(event,ensure_ascii=False)+'\n\n').encode())
        self.wfile.flush()


server = ThreadingHTTPServer(('127.0.0.1',0), Model)
worker = threading.Thread(target=server.serve_forever, daemon=True)
worker.start()
config = replace(make_config(root), runtime_engine='native', protocol='responses', default_model='synthetic-native',
    base_url=f'http://127.0.0.1:{server.server_port}')
try:
    with patch('lg_cli.live_agent.LiveAgent', side_effect=AssertionError('Native must not invoke Codex')):
        asyncio.run(serve(config))
finally:
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)
