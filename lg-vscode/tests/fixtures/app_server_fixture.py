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
handled = set()
original_popen = app_server.subprocess.Popen


def diagnostic_popen(command, *args, **kwargs):
    if 'app-server' in command:
        kwargs['stderr'] = (workspace/'fixture-runtime.log').open('ab')
        kwargs['env'] = dict(kwargs.get('env', os.environ), RUST_LOG='codex_mcp=debug,codex_app_server=debug')
    return original_popen(command, *args, **kwargs)


def output(payload):
    (workspace/'fixture-tool-names.json').write_text(json.dumps([t.get('name') for t in payload.get('tools', [])]))
    markers = re.findall(r'CASE:([a-z]+)', json.dumps(payload.get('messages', [])))
    case = markers[-1] if markers else 'text'
    if case in handled:
        yield from message_events(delta={'type': 'text_delta', 'text': 'LG synthetic provider finished.'})
        return
    handled.add(case)
    operations = {
        'init': ('studio_initialize', {}),
        'plan': ('studio_save', {'stage': 'position', 'content': '原创机械悬疑长篇，目标百万字。',
            'updates': {'positioning': '原创机械悬疑长篇', 'targetWords': 1000000}, 'expected_profile_version': 0}),
        'denied': ('update_book_overview', {'text': '这句话被拒绝后不得保存。'}),
        'accepted': ('update_book_overview', {'text': '修理师在停摆的浮城中追查供能失衡的真相。'}),
    }
    if case == 'slow':
        time.sleep(5)
    if case not in operations:
        yield from message_events(delta={'type': 'text_delta', 'text': 'LG streaming fixture: 星港的灯亮了。'})
        return
    name, arguments = operations[case]
    description = {'studio_initialize': 'Initialize only the current workspace',
        'studio_save': 'Save a provisional planning/review artifact',
        'update_book_overview': 'Save one concise Chinese sentence'}[name]
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
