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

workspace = Path(sys.argv[sys.argv.index('--workspace') + 1])
config = replace(make_config(workspace), provider='stepfun', protocol='anthropic', default_model='step-3.5-flash')
if os.environ.get('LG_DESKTOP_FIXTURE_KEY'):
    from lg_cli.config import load_config
    config=replace(config,default_model=load_config(workspace).default_model)
handled = set()
original_popen = app_server.subprocess.Popen


def diagnostic_popen(command, *args, **kwargs):
    if 'app-server' in command:
        kwargs['stderr'] = (workspace/'fixture-runtime.log').open('ab')
        kwargs['env'] = dict(kwargs.get('env', os.environ), RUST_LOG='codex_mcp=debug,codex_app_server=debug')
    return original_popen(command, *args, **kwargs)


def output(payload):
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
        'studio_adopt_reference': 'Adopt the exact author-previewed literary reference candidate'}[name]
    tool = next(t['name'] for t in payload.get('tools', []) if description in t.get('description', ''))
    yield {'type': 'message_start', 'message': {'usage': {'input_tokens': 10, 'output_tokens': 0}}}
    yield {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'tool_use', 'id': 'call-' + case, 'name': tool, 'input': {}}}
    yield {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(arguments)}}
    yield {'type': 'content_block_stop', 'index': 0}
    yield {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 10}}
    yield {'type': 'message_stop'}


with patch.object(app_server.subprocess, 'Popen', side_effect=diagnostic_popen), patch.object(app_server, 'load_config', return_value=config), patch.object(app_server, 'provider_bridge',
        side_effect=lambda _: AnthropicBridge(config, stream_factory=output)):
    app_server.main()
