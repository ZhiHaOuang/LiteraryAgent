"""Map Native execution results to the existing product presentation contract."""
import json
from typing import Any, Callable


class NativeEvents:
    def __init__(self, emit: Callable[[str, dict], None]):
        self.emit = emit

    def assistant_delta(self, identity: str, text: str) -> None:
        self.emit('assistant.delta', {'itemId': identity, 'delta': text})

    def assistant_completed(self, identity: str, text: str) -> None:
        self.emit('item.updated', {'item': {'id': identity, 'kind': 'assistant',
            'title': 'LG', 'text': text, 'state': 'completed'}})

    def tool_started(self, identity: str, name: str, arguments: dict) -> None:
        self.emit('item.updated', {'item': {'id': identity, 'kind': 'tool',
            'title': 'lg_writing · ' + name, 'text': '', 'state': 'running',
            'structured': {'arguments': arguments}}})

    def tool_completed(self, identity: str, name: str, arguments: dict, data: dict[str, Any]) -> None:
        structured = data.get('structuredContent')
        blocks = data.get('content') or []
        if structured is None and len(blocks) == 1 and blocks[0].get('type') == 'text':
            try:
                structured = json.loads(blocks[0].get('text', ''))
            except ValueError:
                pass
        details = structured if isinstance(structured, dict) else {}
        item = {'id': identity, 'kind': 'tool', 'title': 'lg_writing · ' + name,
            'state': 'failed' if data.get('isError') else 'completed',
            'text': '\n'.join(c.get('text', '') for c in blocks if c.get('type') == 'text'),
            'structured': {'arguments': arguments, 'result': structured if structured is not None else blocks}}
        if isinstance(details.get('artifact'), str):
            item['files'] = [details['artifact']]
        diff = details.get('diff')
        if (isinstance(diff, dict) and all(isinstance(diff.get(k), str) for k in ('path', 'before', 'after'))
                and type(diff.get('applied')) is bool):
            item['diff'] = {k: diff[k] for k in ('path', 'before', 'after', 'applied')}
        self.emit('item.updated', {'item': item})

    def review_task(self, child: dict[str, Any]) -> None:
        labels = {'waiting': '等待专家分析', 'running': '独立审读中', 'completed': '临时审读意见已返回',
                  'failed': '专家分析未完成', 'cancelled': '专家分析已取消'}
        step = labels[child['state']]
        self.emit('item.updated', {'item': {'id': child['id'], 'kind': 'notice', 'title': child['expert'],
            'state': child['state'], 'text': step, 'structured': {'task': {'name': child['expert'],
                'status': child['state'], 'step': step, 'details': []}}}})
