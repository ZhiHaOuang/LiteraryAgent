"""Render LG presentation events without knowing the execution runtime."""
import json


class LiveView:
    def __init__(self, session):
        self.session = session
        self.messages = {}
        self.completed_messages = {}
        self.states = {}
        self.items = {}
        self.finished_runs = set()

    def __call__(self, method, params):
        if method != 'lg.event':
            return
        session = self.session
        event = params.get('type')
        if event == 'run.started':
            self.finished_runs.discard(params['runId'])
        elif event == 'run.finished':
            for item in list(self.items.values()):
                if item['runId'] == params['runId'] and item['state'] in {'running','waiting'}:
                    state = 'failed' if params['status'] == 'interrupted' else params['status']
                    self('lg.event', {'type':'item.updated', 'item':{**item, 'state':state}})
            self.finished_runs.add(params['runId'])
        elif event in {'assistant.delta', 'item.updated'}:
            item = params.get('item') or {}
            run = item.get('runId', params.get('runId', ''))
            if run in self.finished_runs:
                return
            key = json.dumps([run, item.get('id', params.get('itemId'))], ensure_ascii=False)
            if self.states.get(key) in {'completed', 'failed', 'cancelled'}:
                return
            if event == 'assistant.delta':
                self.messages[key] = self.messages.get(key, '') + params['delta']
                session.stream_message(key, params['delta'])
            else:
                self.states[key] = item['state']
                self.items[key] = item
                kind = item['kind']
                if kind == 'assistant':
                    self.messages[key] = item['text']
                    if item['state'] != 'running':
                        self.completed_messages[key] = item['text']
                    session.stream_message(key, item['text'], replace=True)
                elif kind == 'tool':
                    status = {'running':'进行中', 'waiting':'等待', 'completed':'完成',
                              'failed':'失败', 'cancelled':'已取消'}.get(item['state'], item['state'])
                    parts = [item['title'] + ' · ' + status, item.get('text', '')]
                    if item.get('structured') is not None:
                        parts.append(json.dumps(item['structured'], ensure_ascii=False, indent=2))
                    session.stream_message('tool:' + key, '\n'.join(parts), replace=True, role='execution')
                elif kind == 'notice':
                    data = item.get('structured')
                    task = data.get('task') if isinstance(data, dict) else None
                    if isinstance(task, dict):
                        session.tasks[key] = {'name': item['title'], 'status': item['state'],
                                              'step': item['text'], 'details': task.get('details', [])}
                    else:
                        session.stream_message('notice:' + key, item['text'], replace=True, role='progress')
        session._view_cache = None
        session.app.invalidate()
