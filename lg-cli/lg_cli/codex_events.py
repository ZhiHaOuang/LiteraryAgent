"""Codex wire events translated at the runtime boundary."""
import json


def normalize_item(value, run_id, done):
    if not isinstance(value, dict) or not isinstance(value.get('id'), str) or not value['id']:
        return None
    kind = value.get('type')
    if kind == 'reasoning':
        return None
    state = ('failed' if value.get('status') == 'failed' else 'cancelled'
        if value.get('status') == 'declined' else 'completed' if done else 'running')
    item = {'id': value['id'], 'runId': run_id, 'kind': 'notice', 'title': str(kind or '输出'),
        'text': '', 'state': state}
    if kind == 'userMessage':
        item.update(id='user:' + run_id, kind='user', title='作者', state='completed',
            text='\n'.join(c.get('text', '') for c in value.get('content', []) if c.get('type') == 'text'))
    elif kind == 'agentMessage':
        item.update(kind='assistant', title='LG', text=value.get('text', ''))
    elif kind == 'plan':
        item.update(title='计划', text=value.get('text', ''))
    elif kind == 'dynamicToolCall' and value.get('tool') == 'delegate_review':
        failed = value.get('success') is False or value.get('status') == 'failed'
        item.update(kind='tool', title='独立专家审读', state='failed' if failed else state,
            text='专家审读未完成。' if failed else '临时意见已返回主 Agent。' if done else '正在请求独立专家审读。')
    elif kind == 'mcpToolCall':
        result = value.get('result') or {}
        if value.get('error') or value.get('status') == 'failed' or result.get('isError'):
            item.update(kind='tool', title=value.get('tool') or '工具', state='failed',
                text='工具失败，请核对已保存状态。')
            return item
        blocks = result.get('content') or []
        structured = result.get('structuredContent')
        if structured is None and len(blocks) == 1 and blocks[0].get('type') == 'text':
            try:
                structured = json.loads(blocks[0].get('text', ''))
            except (ValueError, TypeError):
                pass
        item.update(kind='tool', title=f"{value.get('server', '')} · {value.get('tool', '')}",
            text='工具失败，请核对已保存状态。' if value.get('error') else
                '\n'.join(c.get('text', '') for c in blocks if c.get('type') == 'text'),
            structured={'arguments': value.get('arguments'), 'result': structured if structured is not None else blocks})
        details = structured if isinstance(structured, dict) else {}
        if isinstance(details.get('artifact'), str):
            item['files'] = [details['artifact']]
        diff = details.get('diff') or {}
        if isinstance(diff, dict) and all(isinstance(diff.get(k), str) for k in ('path', 'before', 'after')) and type(diff.get('applied')) is bool:
            item['diff'] = {k: diff[k] for k in ('path', 'before', 'after', 'applied')}
    elif kind == 'commandExecution':
        item.update(kind='tool', title=value.get('command') or '命令', text=value.get('aggregatedOutput', ''),
            structured={'cwd': value.get('cwd'), 'exitCode': value.get('exitCode')})
    elif kind == 'fileChange':
        item.update(kind='tool', title='文件修改', text='后端已报告文件变化；界面不会重复应用。', structured=value.get('changes'))
    elif kind == 'webSearch':
        action = value.get('action')
        public_action = {k: action[k] for k in ('type','query','queries','url','pattern') if k in action} if isinstance(action,dict) else None
        item.update(kind='tool', title='搜索', text=value.get('query') or '',
                    structured={'action':public_action, 'results':value.get('results')})
    else:
        item['text'] = '此输出类型暂未提供展示内容。'
    return item


def engine_event(method, params, session, run_id):
    if method == 'item/agentMessage/delta':
        if not params.get('itemId'):
            return None
        return {'type': 'assistant.delta', 'sessionId': session, 'runId': run_id,
            'itemId': params['itemId'], 'delta': params.get('delta', '')}
    if method in {'item/started', 'item/completed'}:
        item = normalize_item(params.get('item'), run_id, method == 'item/completed')
        if item:
            return {'type': 'item.updated', 'sessionId': session, 'item': item}
    return None


class CodexEvents:
    """Only registered children and explicit public fields cross the runtime boundary."""
    def __init__(self, emit, thread_id, *, expected_turn=None):
        self.emit, self.thread_id = emit, thread_id
        self.expected_turn = expected_turn
        self.retired_turns = set()
        self.retired_child_turns = set()
        self.begin()

    def begin(self):
        if getattr(self, 'turn_id', None):
            self.retired_turns.add(self.turn_id)
        self.children, self.summaries, self.outputs = {}, {}, {}
        self.work_events = set()
        self.child_turns = {}
        self.pending, self.pending_bytes = [], 0
        self.turn_id = None
        self.active = True

    def finish(self):
        for key, task in self.children.items():
            if task['status'] in {'running','waiting'}:
                self.task(key, state='cancelled', step='主任务已结束，子任务终态未确认。')
        if self.turn_id:
            self.retired_turns.add(self.turn_id)
        self.retired_child_turns.update(self.child_turns.values())
        self.pending, self.pending_bytes = [], 0
        self.active = False

    def bind(self, turn):
        if not turn or turn in self.retired_turns:
            return
        self.turn_id = turn
        pending, self.pending = self.pending, []
        self.pending_bytes = 0
        for method, params in pending:
            self(method, params)

    def item(self, key, title, text, *, state='running', kind='notice', structured=None):
        value = {'id': key, 'runId': 'runtime', 'kind': kind, 'title': title,
                 'text': text, 'state': state}
        if structured is not None:
            value['structured'] = structured
        self.emit('item.updated', {'item': value})

    def task(self, key, *, name=None, state=None, step=None, detail=None, new_work=False):
        task = self.children.setdefault(key, {'name': name or '协作 Agent',
            'status': 'running', 'step': '准备工作', 'details': [], 'generation':0})
        if new_work and task['status'] in {'completed','failed','cancelled'}:
            task.update(status='running', step='准备工作', details=[], generation=task['generation'] + 1)
            if key in self.child_turns:
                self.retired_child_turns.add(self.child_turns.pop(key))
        if name:
            task['name'] = name
        if state:
            task['status'] = {'inProgress':'running', 'pendingInit':'waiting', 'errored':'failed',
                'notFound':'failed', 'interrupted':'cancelled', 'shutdown':'cancelled'}.get(state, state)
            if task['status'] not in {'waiting','running','completed','cancelled','failed'}:
                task['status'] = 'running'
        if step:
            task['step'] = step
        if detail and (not task['details'] or task['details'][-1] != detail):
            task['details'] = [*task['details'][-19:], detail[:8000]]
        self.item('task:' + key + ':' + str(task['generation']), task['name'], task['step'], state=task['status'],
                  structured={'task': {**task, 'details': list(task['details'])}})

    def __call__(self, method, params):
        main = self.thread_id()
        if not self.active or not main:
            return
        if method == 'lg.turnBound':
            if params.get('threadId') == main:
                self.bind(params.get('turnId'))
            return
        if self.expected_turn and self.turn_id is None:
            expected = self.expected_turn()
            if expected:
                self.bind(expected)
            if self.turn_id is None:
                if method in {'item/started','item/completed','turn/completed','thread/started',
                              'item/agentMessage/delta','item/reasoning/summaryTextDelta',
                              'item/commandExecution/outputDelta'}:
                    self.pending_bytes += len(json.dumps(params,ensure_ascii=False).encode())
                    if len(self.pending) >= 1024 or self.pending_bytes > 4 * 1024 * 1024:
                        raise ValueError('Runtime sent too much output before confirming its turn')
                    self.pending.append((method, params))
                return
        thread = params.get('threadId')
        child = thread is not None and thread != main
        if method == 'thread/started':
            info = params.get('thread') or {}
            if info.get('id') in self.children:
                self.task(info['id'], name=info.get('agentNickname') or info.get('agentRole'))
            return
        if child:
            if thread not in self.children:
                return
            child_turn = params.get('turnId') or (params.get('turn') or {}).get('id')
            if child_turn:
                if child_turn in self.retired_child_turns:
                    return
                previous = self.child_turns.get(thread)
                if previous and previous != child_turn:
                    if method != 'turn/started':
                        return
                    self.retired_child_turns.add(previous)
                if previous != child_turn and self.children[thread]['status'] in {'completed','failed','cancelled'}:
                    self.task(thread, new_work=True, state='running', step='工作中')
                self.child_turns[thread] = child_turn
            if method == 'turn/completed':
                turn = params.get('turn') or {}
                status = turn.get('status', 'failed')
                self.task(thread, state=status, step='已完成' if status == 'completed' else
                          '已中断' if status == 'interrupted' else '任务失败，请核对已保存状态。')
                if child_turn:
                    self.retired_child_turns.add(child_turn)
            elif method in {'item/started','item/completed'}:
                value = params.get('item') or {}
                if value.get('type') == 'agentMessage' and method == 'item/completed':
                    text = value.get('text') or ''
                    self.task(thread, step=text.split('\n')[0][:80], detail=text)
                elif value.get('type') == 'mcpToolCall':
                    self.task(thread, step=value.get('tool') or '读取资料')
            return
        turn = params.get('turnId')
        if turn:
            if turn in self.retired_turns:
                return
            if self.turn_id and turn != self.turn_id:
                return
            self.turn_id = turn
        if method == 'lg.requestUnsupported':
            self.item('unsupported', '交互请求', '模型请求了当前界面尚不支持的交互。', state='failed')
            return
        if method == 'item/reasoning/summaryTextDelta':
            key, index = params.get('itemId'), params.get('summaryIndex', 0)
            if not key or type(index) is not int or not 0 <= index < 100:
                return
            parts = self.summaries.setdefault(key, {})
            parts[index] = (parts.get(index, '') + params.get('delta', ''))[-24000:]
            self.item('summary:' + key, '进度', '\n'.join(parts[i] for i in sorted(parts))[-24000:],
                      structured={'presentation': 'progress'})
            return
        if method == 'item/commandExecution/outputDelta':
            key = params.get('itemId')
            if key:
                self.outputs[key] = (self.outputs.get(key, '') + params.get('delta', ''))[-250000:]
                self.item(key, '命令执行中', self.outputs[key], kind='tool')
            return
        if method in {'item/started','item/completed'}:
            value = params.get('item') or {}
            kind = value.get('type')
            if kind == 'subAgentActivity':
                key = value.get('agentThreadId')
                if key:
                    activity = value.get('kind')
                    if activity == 'interacted':
                        # Queue-only messages and new work share this wire event.
                        self.task(key, state=None if key in self.children else 'completed', step='收到主 Agent 消息')
                        return
                    fresh = activity == 'started' and value.get('id') not in self.work_events
                    self.work_events.add(value.get('id'))
                    self.task(key, name=str(value.get('agentPath') or '协作 Agent').split('/')[-1],
                        new_work=fresh,
                        state={'completed':'completed', 'interrupted':'cancelled', 'started':'running'}.get(activity),
                        step={'completed':'已完成','interrupted':'已中断','started':'工作中'}.get(activity,'收到主 Agent 指令'))
                return
            if kind == 'collabAgentToolCall':
                fresh = value.get('tool') in {'followupTask','resumeAgent','sendInput'} and value.get('id') not in self.work_events
                self.work_events.add(value.get('id'))
                for key in value.get('receiverThreadIds') or []:
                    state = (value.get('agentsStates') or {}).get(key) or {}
                    status = state.get('status')
                    message = state.get('message') if status not in {'errored','notFound'} else '任务失败，请核对已保存状态。'
                    self.task(key, state=status, step=(message or '').split('\n')[0][:80], detail=message, new_work=fresh)
                return
            if kind == 'reasoning':
                summary = '\n'.join(s for s in value.get('summary') or [] if isinstance(s,str))
                if summary and method == 'item/completed':
                    self.item('summary:' + value['id'], '进度', summary[-24000:], state='completed',
                              structured={'presentation':'progress'})
                return
            if kind == 'commandExecution' and not value.get('aggregatedOutput'):
                params = {**params, 'item': {**value, 'aggregatedOutput': self.outputs.get(value.get('id'), '')}}
        event = engine_event(method, params, 'runtime', 'runtime')
        if event and event.get('item',{}).get('kind') != 'user':
            event.pop('sessionId', None)
            event.pop('runId', None)
            kind = event.pop('type')
            self.emit(kind, event)


