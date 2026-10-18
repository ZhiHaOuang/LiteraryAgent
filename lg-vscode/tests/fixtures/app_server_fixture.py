"""Real pinned core + real LG launcher/MCP; model output is deliberately synthetic."""
import json
import re
import sys
import time
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from lg_cli import app_server
from lg_cli.anthropic_bridge import AnthropicBridge
from tests.helpers import make_config
from tests.test_anthropic_bridge import message_events
from tests.intent_fixture import current_user,routed_reply

workspace = Path(sys.argv[sys.argv.index('--workspace') + 1])
config = replace(make_config(workspace), provider='stepfun', protocol='anthropic', default_model='step-3.5-flash')
if os.environ.get('LG_DESKTOP_FIXTURE_KEY'):
    from lg_cli.config import load_config
    config=replace(config,default_model=load_config(workspace).default_model)
handled = set()
original_popen = app_server.subprocess.Popen


def diagnostic_popen(command, *args, **kwargs):
    if 'app-server' in command and '--literary-backend' not in sys.argv:
        kwargs['stderr'] = (workspace/'fixture-runtime.log').open('ab')
        kwargs['env'] = dict(kwargs.get('env', os.environ), RUST_LOG='codex_mcp=debug,codex_app_server=debug')
    return original_popen(command, *args, **kwargs)


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


def output(payload):
    if MATRIX:
        text = current_user(payload.get('messages', []))
        routed = matrix_route(text)
        if routed is not None:
            yield from message_events(delta={'type':'text_delta','text':json.dumps(routed, ensure_ascii=False)})
            return
        (workspace/'matrix-provider-request.json').write_text(json.dumps({'text':text,'tools':[t['name'] for t in payload.get('tools', [])]},ensure_ascii=False))
        operation = matrix_operation(workspace, text, payload.get('messages', []))
        if operation is None:
            yield from message_events(delta={'type':'text_delta','text':'MATRIX 工具执行已核对。'})
            return
        name, arguments = operation
        descriptions = {'studio_save':'Save a provisional planning/review artifact',
            'edit_manuscript':'Save an Agent-generated prose revision as a candidate',
            'studio_review_context':'Read the exact source snapshot and existing review schemas',
            'studio_review':'Save source-validated reviews produced from studio_review_context',
            'studio_accept':'Explicitly adopt a reviewed candidate once'}
        tool = next(t['name'] for t in payload.get('tools', []) if descriptions[name] in t.get('description',''))
        yield {'type':'message_start','message':{'usage':{'input_tokens':10,'output_tokens':0}}}
        yield {'type':'content_block_start','index':0,'content_block':{'type':'tool_use','id':'matrix-'+str(sum(matrix_steps.values())),'name':tool,'input':{}}}
        yield {'type':'content_block_delta','index':0,'delta':{'type':'input_json_delta','partial_json':json.dumps(arguments,ensure_ascii=False)}}
        yield {'type':'content_block_stop','index':0}
        yield {'type':'message_delta','delta':{'stop_reason':'tool_use'},'usage':{'output_tokens':10}}
        yield {'type':'message_stop'}
        return

    routed=routed_reply(current_user(payload.get('messages',[])))
    if routed is not None:
        yield from message_events(delta={'type':'text_delta','text':json.dumps(routed,ensure_ascii=False)})
        return
    (workspace/'fixture-tool-names.json').write_text(json.dumps([t.get('name') for t in payload.get('tools', [])]))
    transcript=json.dumps(payload.get('messages', []),ensure_ascii=False)
    markers=list(re.finditer(r'CASE:([a-z]+)|作者要求生成更新 characters|作者要求采用 lin 的候选 v(\d+)',transcript))
    latest=markers[-1] if markers else None
    case=(latest.group(1) or ('libadopt' if latest.group(2) else 'libupdate')) if latest else 'text'
    handled_key=case+str(len(markers)) if case.startswith('lib') else case
    if handled_key in handled:
        yield from message_events(delta={'type': 'text_delta', 'text': 'LG synthetic provider finished.'})
        return
    handled.add(handled_key)
    operations = {
        'init': ('studio_initialize', {}),
        'plan': ('studio_save', {'stage': 'position', 'content': '原创机械悬疑长篇，目标百万字。',
            'updates': {'positioning': '原创机械悬疑长篇', 'targetWords': 1000000}, 'expected_profile_version': 0}),
        'denied': ('update_book_overview', {'text': '这句话被拒绝后不得保存。'}),
        'accepted': ('update_book_overview', {'text': '修理师在停摆的浮城中追查供能失衡的真相。'}),
        'refdeny': ('studio_reference', {'kind':'character','slug':'denied-person','title':'不得保存的人物','content':'不得保存。','expected_version_id':0,'reason':'approval denial test'}),
        'reference': ('studio_reference', {'kind':'character','slug':'lin','title':'林砚','content':'人物总结：维修学徒先隔离电源，再救出闸门后的工友。','expected_version_id':0,'reason':'original reference candidate fixture'}),
    }
    if case=='adoptref':
        from lg_cli.project_store import ProjectStore
        operations[case]=('studio_adopt_reference',{'slug':'lin','version_number':1,'expected_version_id':ProjectStore(workspace).get_document('lin').active_version_id})
    if case in {'edit', 'restore','scopeedit'}:
        from lg_cli.project_store import ProjectStore
        from lg_cli.project_store import ProjectStoreError
        saved = ProjectStore(workspace)
        try:
            document = saved.get_document('original')
        except ProjectStoreError:
            document = saved.create_document(kind='chapter', slug='original', title='原稿',
                content='作者已采纳的原稿。', state='accepted', sequence=1)
        operations[case] = ('edit_manuscript', {'slug': 'original', 'content': 'Agent 提出的改稿候选。',
            'expected_version_id': document.active_version_id, 'reason': 'author requested candidate',
            'book_overview': '维修师在停摆的浮城中寻找供能失衡的真相。'}) if case in {'edit','scopeedit'} else (
            'restore_manuscript', {'slug': 'original', 'version_number': 1,
                'expected_version_id': document.active_version_id})
    if case in {'libupdate','libadopt'}:
        from lg_cli.project_store import ProjectStore
        document=ProjectStore(workspace).get_document('lin')
        operations[case]=('studio_reference',{'kind':'character','slug':'lin','title':'林砚','content':'更新候选：林砚在救援前隔离电源，愿意承担错过返乡船的代价。','expected_version_id':document.active_version_id,'reason':'author-requested UI update fixture'}) if case=='libupdate' else ('studio_adopt_reference',{'slug':'lin','version_number':int(latest.group(2)),'expected_version_id':document.active_version_id})
    if case == 'slow':
        time.sleep(5)
    if case not in operations:
        yield from message_events(delta={'type': 'text_delta', 'text': 'LG streaming fixture: 星港的灯亮了。'})
        return
    name, arguments = operations[case]
    description = {'studio_initialize': 'Initialize only the current workspace',
        'studio_save': 'Save a provisional planning/review artifact',
        'update_book_overview': 'Save one concise Chinese sentence',
        'studio_reference': 'Save author-requested literary reference material',
        'studio_adopt_reference': 'Adopt the exact author-previewed literary reference candidate',
        'edit_manuscript': 'Save an Agent-generated prose revision as a candidate',
        'restore_manuscript': 'Propose restoration of a saved prose version as a candidate'}[name]
    tool = next(t['name'] for t in payload.get('tools', []) if description in t.get('description', ''))
    yield {'type': 'message_start', 'message': {'usage': {'input_tokens': 10, 'output_tokens': 0}}}
    yield {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'tool_use', 'id': 'call-' + case, 'name': tool, 'input': {}}}
    yield {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(arguments)}}
    yield {'type': 'content_block_stop', 'index': 0}
    yield {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 10}}
    yield {'type': 'message_stop'}


with patch.object(app_server.subprocess, 'Popen', side_effect=diagnostic_popen), patch.object(app_server, 'load_config', return_value=config), patch.object(app_server, 'provider_bridge',
        side_effect=lambda _: AnthropicBridge(config, stream_factory=output)):
    if '--literary-backend' in sys.argv:
        import asyncio
        from lg_cli.backend_stdio import serve
        from lg_cli.init_project import init_workspace
        init_workspace(workspace)
        from lg_cli.project_store import ProjectStore
        saved=ProjectStore(workspace)
        if MATRIX:
            matrix_initialize(workspace)
        elif not saved.list_documents(kind='chapter'):
            saved.create_document(kind='chapter',slug='original',title='原稿',content='作者已采纳的原稿。',state='accepted',sequence=1)
        config = replace(config, runtime_manifest=Path(sys.argv[sys.argv.index('--runtime-manifest') + 1]))
        from lg_cli.backend import Backend
        dispatch=Backend.dispatch
        async def diagnostic_dispatch(self,method,params):
            try:return await dispatch(self,method,params)
            except Exception as exc:
                (workspace/'fixture-dispatch-error.json').write_text(json.dumps({'method':method,'type':type(exc).__name__,'error':str(exc)}))
                raise
        with patch('lg_cli.live_agent.provider_bridge', side_effect=lambda _: AnthropicBridge(config, stream_factory=output)), patch.object(Backend,'dispatch',diagnostic_dispatch):
            asyncio.run(serve(config))
    else:
        app_server.main()
