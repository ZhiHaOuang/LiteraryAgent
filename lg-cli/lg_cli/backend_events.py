"""LG presentation events; Codex wire shapes stay on the service side."""
from __future__ import annotations

import json


def service_event(method, params, session, run_id):
    base = {'sessionId': session}
    if method == 'task/updated':
        return {**base, 'type': 'item.updated', 'item': {'id': params['taskId'], 'runId': run_id,
            'kind': 'notice', 'title': params.get('name', '工作流'), 'text': params.get('step', ''),
            'state': params.get('status', 'running'), 'structured': {'task': {
                'name': params.get('name', '工作流'), 'step': params.get('step', ''),
                'status': params.get('status', 'running'), 'details': params.get('details', [])}}}}
    if method == 'workflow/output':
        return {**base, 'type': 'item.updated', 'item': {'id': 'workflow-output:' + run_id,
            'runId': run_id, 'kind': 'notice', 'title': '工作流输出', 'text': params['text'], 'state': 'running'}}
    if method == 'run/started':
        return {**base, 'type': 'run.started', 'runId': params['runId'], 'text': params.get('text', ''),
            **({'source':'retry'} if params.get('source')=='retry' else {})}
    if method == 'run/completed':
        event = {**base, 'type': 'run.finished', 'runId': params['runId'], 'status': params['status']}
        if params['status'] == 'failed':
            event['error'] = '本次任务失败；已保留历史和完成的产物，请核对后再继续。'
        return event
    if method == 'approval/requested':
        details, key = params['details'], params['approvalId']
        if params['kind'] == 'item/tool/requestUserInput':
            return {**base, 'type': 'userInput.requested', 'request': {'id': key, 'runId': run_id,
                'questions': [{'id': q['id'], 'label': q['question'],
                    'options': [o['label'] for o in q.get('options') or []]} for q in details['questions']]}}
        titles = {'creative': '创作采用待审批', 'operation': '写作操作待审批',
            'item/commandExecution/requestApproval': '命令执行待审批',
            'item/fileChange/requestApproval': '文件修改待审批', 'permissions': '权限请求待审批'}
        return {**base, 'type': 'approval.requested', 'approval': {'id': key, 'runId': run_id,
            'title': titles.get(params['kind'], '操作待审批'), 'details': json.dumps(details, ensure_ascii=False, indent=2),
            'choices': ['accept', 'decline', 'cancel'], 'state': 'pending'}}
    if method in {'approval/resolved', 'approval/cancelled'} and params.get('approvalId'):
        question = params.get('kind') == 'item/tool/requestUserInput'
        return {**base, 'type': 'userInput.resolved' if question else
            'approval.resolved' if method == 'approval/resolved' else 'approval.expired', 'requestId': params['approvalId']}
    return None


def active_runs(events):
    active = {}
    for record in events:
        if record['method']!='client/event':
            continue
        event = record['params']
        if event.get('type')=='run.started':
            active[event['runId']] = {'id':event['runId'],'state':'running'}
        elif event.get('type')=='run.finished':
            active.pop(event['runId'],None)
    return list(active.values())


def snapshot_items(messages, events,*,prior_items=()):
    items = {}
    for record in events:
        if record['method'] != 'client/event':
            continue
        event = record['params']
        kind = event['type']
        if kind == 'run.started':
            run = event['runId']
            retry = event.get('source')=='retry'
            item = {'id': ('retry:' if retry else 'user:') + run, 'runId': run,
                'kind': 'notice' if retry else 'user', 'title': '重试' if retry else '作者',
                'text': event['text'], 'state': 'completed'}
            items[(run, item['id'])] = item
        elif kind == 'item.updated':
            item = event['item'].copy()
            key = (item['runId'], item['id'])
            previous = items.get(key)
            if not previous or previous['state'] in {'running', 'waiting'} or item['state'] not in {'running', 'waiting'}:
                items[key] = item
        elif kind == 'assistant.delta':
            key = (event['runId'], event['itemId'])
            item = items.setdefault(key, {'id': key[1], 'runId': key[0], 'kind': 'assistant',
                'title': 'LG', 'text': '', 'state': 'running'})
            if item['state'] == 'running':
                item['text'] += event['delta']
        elif kind == 'run.finished':
            for item in items.values():
                if item['runId'] == event['runId'] and item['state'] in {'running', 'waiting'}:
                    item['state'] = 'failed' if event['status'] == 'interrupted' else event['status']
    grouped = {}
    for (run, _), item in items.items():
        grouped.setdefault(run, []).append(item)
    ordered, seen_runs = [], set()
    prior_keys={(item['runId'],item['id']) for item in prior_items}
    for index, message in enumerate(messages):
        key = (message.get('run_id'), message.get('item_id'))
        if key in prior_keys:continue
        if key[0] in grouped and key[0] not in seen_runs:
            ordered.extend(grouped[key[0]])
            seen_runs.add(key[0])
        if key in items:
            continue
        run = message.get('run_id') or f'history:{index}'
        ordered.append({'id': message.get('item_id') or f'history:{index}', 'runId': run,
            'kind': message['role'] if message['role'] != 'error' else 'notice',
            'title': '作者' if message['role'] == 'user' else 'LG', 'text': message['text'], 'state': 'completed'})
    return [*prior_items,*ordered, *(item for k, item in items.items() if k[0] not in seen_runs)]
