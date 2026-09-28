"""Translate public agent events into the conversation and compact task strip."""
import json


class LiveView:
    def __init__(self, session):
        self.session = session
        self.thread_id = None
        self.messages = {}
        self.completed_messages = {}
        self.executions = {}

    def __call__(self, method, params):
        session = self.session
        thread = params.get('threadId')
        child = bool(self.thread_id and thread and thread != self.thread_id)
        if method == 'lg.requestUnsupported':
            session.append('模型请求了当前界面尚不支持的交互：' + params['method'] + '\n')
        elif method == 'item/agentMessage/delta' and not child:
            key = params['itemId']
            delta = params.get('delta', '')
            self.messages[key] = self.messages.get(key, '') + delta
            session.stream_message(key, delta)
        elif method == 'item/reasoning/summaryTextDelta' and not child:
            session.stream_message('summary:' + params['itemId'], params.get('delta', ''), role='progress')
        elif method == 'item/commandExecution/outputDelta' and not child:
            key = params['itemId']
            self.executions[key] = (self.executions.get(key, '') + params.get('delta', ''))[-250000:]
            session.stream_message('tool:' + key, '命令执行中\n' + self.executions[key], replace=True, role='execution')
        elif method in {'item/started', 'item/completed'}:
            item = params.get('item', {})
            kind = item.get('type')
            if kind == 'subAgentActivity':
                key = item['agentThreadId']
                task = session.tasks.setdefault(key, {'name': item['agentPath'].split('/')[-1],
                    'status': 'running', 'step': '准备工作', 'details': []})
                if item.get('kind') == 'interrupted':
                    task['status'], task['step'] = 'cancelled', '已中断'
                else:
                    task['step'] = '工作中' if item.get('kind') == 'started' else '收到主 Agent 指令'
                task['details'].append(task['step'])
            elif kind == 'collabAgentToolCall':
                for key in item.get('receiverThreadIds', []):
                    state = item.get('agentsStates', {}).get(key, {})
                    task = session.tasks.setdefault(key, {'name': '审查 Agent ' + key[:6],
                        'status': 'running', 'step': '准备工作', 'details': []})
                    if item.get('prompt'):
                        prompt = item['prompt']
                        task['step'] = prompt.splitlines()[0][:80]
                        if prompt not in task['details']:
                            task['details'].append(prompt)
                    status = state.get('status', 'running')
                    task['status'] = {'errored': 'failed', 'interrupted': 'cancelled', 'shutdown': 'cancelled'}.get(status, status)
                    if state.get('message'):
                        task['details'].append(state['message'])
                        task['step'] = state['message'].splitlines()[0][:80]
            elif child:
                task = session.tasks.setdefault(thread, {'name': '审查 Agent ' + thread[:6],
                    'status': 'running', 'step': '工作中', 'details': []})
                if kind == 'agentMessage' and method == 'item/completed':
                    task['step'] = item.get('text', '').split('\n')[0][:80]
                    task['details'].append(item.get('text', ''))
                elif kind == 'mcpToolCall':
                    task['step'] = item.get('tool', '读取资料')
            elif kind == 'agentMessage' and method == 'item/completed':
                key, text = item['id'], item.get('text', '')
                self.messages[key] = text
                self.completed_messages[key] = text
                session.stream_message(key, text, replace=True)
            elif kind == 'reasoning' and method == 'item/completed':
                summary = '\n'.join(item.get('summary') or [])
                if summary:
                    session.stream_message('summary:' + item['id'], summary, replace=True, role='progress')
            elif kind == 'mcpToolCall':
                label = item.get('tool', '执行操作')
                status = '完成' if method == 'item/completed' else '进行中'
                detail = json.dumps({'参数': item.get('arguments') or {},
                    '结果': item.get('result') or item.get('error') or {}}, ensure_ascii=False, indent=2)
                session.stream_message('tool:' + item['id'], f'{label} · {status}\n{detail}', replace=True, role='execution')
            elif kind == 'commandExecution':
                status = '完成' if method == 'item/completed' else '进行中'
                output = item.get('aggregatedOutput') or self.executions.get(item['id'], '')
                detail = f"命令 · {status}\n{item.get('command', '')}\n{output}"
                if item.get('exitCode') is not None:
                    detail += f"\n退出码：{item['exitCode']}"
                session.stream_message('tool:' + item['id'], detail, replace=True, role='execution')
            elif kind in {'fileChange', 'webSearch'}:
                label = '文件修改' if kind == 'fileChange' else '搜索'
                status = '完成' if method == 'item/completed' else '进行中'
                session.stream_message('tool:' + item['id'], f'{label} · {status}\n'
                    + json.dumps(item, ensure_ascii=False, indent=2), replace=True, role='execution')
        elif method == 'turn/completed' and child and thread in session.tasks:
            turn = params.get('turn', {})
            task = session.tasks[thread]
            task['status'] = {'interrupted': 'cancelled'}.get(turn.get('status'), turn.get('status', 'completed'))
            if turn.get('error'):
                task['step'] = str(turn['error'].get('message', '任务失败'))
                task['details'].append(task['step'])
            elif task['status'] == 'cancelled':
                task['step'] = '已中断'
            elif task['step'] in {'准备工作', '工作中'}:
                task['step'] = '已完成'
        elif method == 'thread/started':
            info = params.get('thread', {})
            key = info.get('id')
            name = info.get('agentNickname') or info.get('agentRole')
            if key and name:
                task = session.tasks.setdefault(key, {'status': 'running', 'step': '准备工作', 'details': []})
                task['name'] = name
        session._view_cache = None
        session.app.invalidate()
