"""Versioned, terminal-independent LiteraryGiant service for local clients."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import sqlite3
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

import jsonschema

from .approvals import ApprovalBroker
from .backend_events import active_runs, service_event, snapshot_items
from .backend_history import SnapshotPages, prepare_snapshot
from .conversation_store import ConversationStore
from .agent_runtime import runtime_factory
from .project_store import ProjectStore
from .session_runtime import operation_policy, permissions
from .workflow_control import WorkflowControl


class EventJournal:
    @staticmethod
    def public_record(record):
        if record.get('method') == 'engine/event':
            return {**record, 'method': 'runtime/legacyEventOmitted', 'params': {}}
        return record

    def __init__(self, workspace):
        path = workspace / '.literarygiant' / 'client-events.sqlite3'
        path.parent.mkdir(parents=True,exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        path.chmod(0o600)
        self.db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, session TEXT, event TEXT)')
        self.db.commit()

    def append(self, session, method, params):
        record = self.public_record({'method': method, 'params': params, 'sessionId': session})
        with self.db:
            cursor = self.db.execute('INSERT INTO events(session,event) VALUES (?,?)', (session, json.dumps(record, ensure_ascii=False)))
        return {'cursor': cursor.lastrowid, **record}

    def read(self, session, after=0, limit=200):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Invalid event cursor or limit.')
        return [{'cursor': key, **self.public_record(json.loads(event))} for key, event in self.db.execute(
            'SELECT id,event FROM events WHERE session=? AND id>? ORDER BY id LIMIT ?', (session, after, limit))]


class ProcessHost:
    """WorkflowControl adapter with no terminal dependency."""
    def __init__(self, emit):
        self.emit = emit
        self.process = None

    def set_task(self, key, **values):
        self.emit('task/updated', {'taskId': key, **values})

    def begin_work(self):
        pass

    def end_work(self):
        pass

    def append(self, text):
        self.emit('workflow/output', {'text': text})

    async def _animate_progress(self):
        await asyncio.Future()

    async def run_command(self, argv, **kwargs):
        from .workflow_io import WorkflowStream, feed_request
        callback = kwargs.get('on_record')
        stream = WorkflowStream(callback) if callback else None
        input_text = kwargs.get('input_text')
        self.process = await asyncio.create_subprocess_exec(*argv, cwd=kwargs.get('cwd'), env=kwargs.get('env'),
            stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        feed = asyncio.create_task(feed_request(self.process,input_text)) if input_text is not None else None
        try:
            while chunk := await self.process.stdout.read(8192):
                if stream:
                    stream.feed(chunk)
                else:
                    self.append(chunk.decode('utf-8', errors='replace'))
            if stream:
                stream.end()
            if feed:
                await feed
            return await self.process.wait()
        finally:
            if feed:
                feed.cancel()
                await asyncio.gather(feed,return_exceptions=True)
            if self.process and self.process.returncode is None:
                await self.stop_workflow_process()
            self.process = None

    async def stop_workflow_process(self):
        process = self.process
        if process:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                await asyncio.wait_for(process.wait(), 5)
            except ProcessLookupError:
                pass
            except asyncio.TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


class Backend:
    def __init__(self, config, notify=lambda record: None, *, agent_factory=None, control_factory=None,legacy_reader_factory=None,task_router=None):
        from .bookshelf import Bookshelf
        if Bookshelf(config.workspace).marker.is_file():
            raise ValueError('Choose an individual book workspace.')
        self.config, self.notify, self.agent_factory = config, notify, agent_factory
        self.control_factory = control_factory
        from .intent_router import interpret
        self.task_router=task_router or interpret
        self.store = ConversationStore(config.workspace)
        self.journal = EventJournal(config.workspace)
        self.sessions, self.tasks, self.task_kinds = {}, {}, {}
        self.approvals = ApprovalBroker(self._approval_event)
        self.history = SnapshotPages()
        from .legacy_sessions import LegacySessions
        self.legacy=LegacySessions(config,**({'reader_factory':legacy_reader_factory} if legacy_reader_factory else {}))

    def emit(self, session, method, params, *, run_id=None):
        record = self.journal.append(session, method, params)
        self.notify(record)
        if method != 'client/event':
            state = self.sessions.get(session, {})
            run_id = run_id or state.get('run_id') or state.get('workflow_run_id') or 'read-only'
            presentation_params = params
            if method == 'workflow/output':
                output = state.get('workflow_output', '') + params['text']
                state['workflow_output'] = output[-12000:]
                presentation_params = {'text': ('[较早输出已省略]\n' if len(output) > 12000 else '') + state['workflow_output']}
            event = service_event(method, presentation_params, session, run_id)
            if event:
                self.emit(session, 'client/event', event)

    def _approval_event(self, method, params):
        self.emit(params['owner'], method, params,run_id=params.get('runId'))

    def session(self, key):
        if key not in self.sessions:
            raise ValueError('Session is not open in this client.')
        return self.sessions[key]

    async def open(self, key):
        if key in self.sessions:
            return {'sessionId': key, **self.sessions[key]['agent'].runtime.read()}
        self.store.read(key)
        messages = {}
        state = {'run_id': None, 'workflow_run_id':None}
        def event(method, params):
            if method == 'compat.event':
                return
            if not state['run_id']:
                return
            if method == 'assistant.delta':
                item = params['itemId']
                messages[item] = messages.get(item, '') + params['delta']
                self.emit(key, 'client/event', {'type': method, 'sessionId': key, 'runId': state['run_id'], **params})
            elif method == 'item.updated':
                item = params['item']
                if item['kind'] == 'assistant':
                    messages[item['id']] = item['text']
                self.emit(key, 'client/event', {'type': method, 'sessionId': key,
                    'item': {**item, 'runId': state['run_id']}})
        async def request(method, params):
            kinds = {'permissions': 'permissions', 'user_input': 'item/tool/requestUserInput',
                'command': 'item/commandExecution/requestApproval', 'file_change': 'item/fileChange/requestApproval'}
            if method == 'user_input':
                questions = params.get('questions')
                if not isinstance(questions, list) or not questions or any(
                    not isinstance(q, dict) or q.get('isSecret') or not q.get('id') or not q.get('question') for q in questions):
                    raise ValueError('Unsupported or secret user input request.')
            elif method not in kinds:
                raise ValueError('Unsupported runtime request; no authorization granted.')
            if method in {'command','file_change','permissions'} and state['agent'].runtime.read().get('task'):
                raise ValueError('Scoped literary tasks use LG tools; runtime file and command access is not authorized')
            return await self.approvals.request(kinds[method], params, owner=key,run_id=state['run_id'])
        agent = runtime_factory(self.config, event, agent_factory=self.agent_factory, on_request=request, conversation=key)
        state['agent'] = agent
        agent.runtime.acquire()
        host = ProcessHost(lambda method, params: self.emit(key, method, params,run_id=state.get('workflow_run_id')))
        control = self.control_factory(host.emit) if self.control_factory else WorkflowControl(host)
        def source_receipt():
            from .generation_sources import make_receipt
            return make_receipt(mode='live', run_id=state['source_run_id'],
                observations=state['source_observations'], unknown_reasons=state['source_unknown_reasons'])

        def source_run():
            run = state.get('agent_run_id')
            if not run or run != state['run_id'] or run != state.get('source_run_id'):
                raise ValueError('Agent run is no longer active; no source observation recorded.')
            return run

        async def source_request(action, request):
            run = source_run()
            if action == 'source_begin':
                from .mcp_approval import READ_TOOLS
                tool = request.get('tool')
                if not isinstance(tool, str) or tool not in READ_TOOLS or len(state['source_leases']) >= 100:
                    raise ValueError('Generation source read is unsupported or exceeds the active read limit.')
                token = uuid.uuid4().hex
                lease = {'run_id': run, 'epoch': state['source_epoch'], 'token': token}
                state['source_leases'][token] = {'lease': lease, 'tool': tool}
                return dict(lease)
            lease = request.get('lease')
            if not isinstance(lease, dict) or not isinstance(lease.get('token'), str):
                raise ValueError('Generation source read lease expired.')
            saved = state['source_leases'].pop(lease.get('token'), None)
            if (not saved or saved['lease'] != lease or lease.get('run_id') != run
                    or lease.get('epoch') != state['source_epoch']):
                raise ValueError('Generation source read lease expired.')
            if action == 'source_aborted':
                return {'aborted': True}
            observation = request.get('observation')
            if (not isinstance(observation, dict) or set(observation) != {'tool', 'payload_sha256', 'refs', 'coverage'}
                    or observation.get('tool') != saved['tool'] or observation.get('coverage') not in {'partial', 'unknown'}
                    or not isinstance(observation.get('refs'), list)
                    or not isinstance(observation.get('payload_sha256'), str)
                    or len(observation['payload_sha256']) != 64
                    or any(char not in '0123456789abcdef' for char in observation['payload_sha256'])):
                raise ValueError('Invalid generation source observation.')
            try:
                # Store compact host observations, never unbounded tool payloads.
                copied = json.loads(json.dumps(observation, ensure_ascii=False, allow_nan=False))
                observations = [*state['source_observations'], copied]
                reasons = list(state['source_unknown_reasons'])
                omitted = len(observations) > 100 or len(json.dumps(observations, ensure_ascii=False).encode('utf-8')) > 1024 * 1024
                if omitted:
                    observations = state['source_observations']
                    reason = 'additional_read_observations_omitted_by_capture_limit'
                    if reason not in reasons:
                        reasons.append(reason)
                from .generation_sources import make_receipt
                receipt = make_receipt(mode='live', run_id=run, observations=observations, unknown_reasons=reasons)
                state['agent'].runtime.update(generation_sources=receipt)
            except Exception:
                raise ValueError('Generation source observation could not be saved; no read result was delivered.') from None
            state['source_observations'] = observations
            state['source_unknown_reasons'] = reasons
            return {'recorded': not omitted, 'truncated': omitted, 'run_id': run}

        control.source_handler = source_request
        async def approve(details, *,run_id=None,author_action=False):
            run_id=run_id or state['run_id']
            from .task_scope import check_operation
            scoped=state['agent'].runtime.read().get('task') if details.get('tool') and not author_action else None
            if scoped:check_operation(self.config.workspace,scoped,details)
            from .operation_receipts import SUPPORTED
            operation_id = uuid.uuid4().hex if details.get('tool') in SUPPORTED else None
            shown = {**details, **({'operationId': operation_id} if operation_id else {})}
            if operation_id:
                self.announce_operation(key, run_id, operation_id)
            kind, mode = operation_policy(state['agent'].runtime.read(), details)
            if mode == 'auto_review':
                from .creative_review import review_creative
                result = await review_creative(self.config, {'operation': shown},
                    on_event=lambda method, params: self.emit(key, 'review/event', {'method':method, 'params':params},run_id=run_id))
                self.emit(key, 'approval/resolved', {'owner':key,'kind':kind,'automatic':True,**result},run_id=run_id)
            else:
                result=await self.approvals.request(kind, shown, owner=key, mode=mode,run_id=run_id)
            if scoped and result.get('decision')=='accept':
                current=state['agent'].runtime.read().get('task')
                if not current or current['id']!=scoped['id']:raise ValueError('Author task changed during approval')
                grant=check_operation(self.config.workspace,current,details)
                current[grant]=current.get(grant,0)+1
                state['agent'].runtime.update(task=current)
            if operation_id and result.get('decision') == 'accept':
                state.setdefault('operation_ids', []).append((run_id, operation_id))
                result = {**result, 'operationId': operation_id}
            return result
        agent_requests = set()
        async def bound_approval(details, *, run_id=None, author_action=False):
            if run_id is not None or author_action:
                return await approve(details, run_id=run_id, author_action=author_action)
            run_id = state.get('agent_run_id')
            if not run_id or run_id != state['run_id']:
                raise ValueError('Agent run is no longer active; no approval granted.')
            receipt = source_receipt()
            caller = asyncio.current_task()
            agent_requests.add(caller)
            try:
                result = await approve(details, run_id=run_id)
                if state.get('agent_run_id') != run_id or state['run_id'] != run_id:
                    raise ValueError('Agent run changed during approval; no approval granted.')
                return {**result, 'generation_sources': receipt} if result.get('decision') == 'accept' else result
            finally:
                agent_requests.discard(caller)
        control.approval_handler = bound_approval
        def control_guard(action):
            if action in {'stop','retry'} and state['agent'].runtime.read().get('task'):
                raise ValueError('Workflow control is outside this literary task; use an explicit author control action')
        control.action_guard=control_guard
        unfinished = {}
        for row in self.journal.db.execute('SELECT event FROM events WHERE session=? ORDER BY id', (key,)):
            prior = json.loads(row[0])
            if prior['method'] == 'run/started':
                unfinished[prior['params']['runId']] = prior
            elif prior['method'] == 'run/completed':
                unfinished.pop(prior['params']['runId'], None)
        for run in unfinished:
            self.emit(key, 'run/completed', {'runId':run, 'status':'interrupted', 'reason':'Previous client disconnected; completed artifacts retained.'})
        state.update(control=control, messages=messages, request=request, event=event,
                     agent_run_id=None, agent_requests=agent_requests, source_run_id=None,
                     source_epoch=None, source_leases={}, source_observations=[], source_unknown_reasons=[])
        self.sessions[key] = state
        return {'sessionId': key, **agent.runtime.read()}

    def _active(self, key, *, include_workflows=True,include_agents=True):
        return any(owner == key and not task.done() and
            (include_workflows or self.task_kinds.get(run)!='workflow') and
            (include_agents or self.task_kinds.get(run)!='agent') for run,(owner,task) in self.tasks.items())

    def schedule(self, key, execute, *, text='', kind='operation',controlled_retry=False):
        state = self.session(key)
        if self._active(key,include_workflows=kind!='agent',include_agents=not controlled_retry) or (kind=='workflow' and state['control'].active and not controlled_retry):
            raise ValueError('A run is already active; steer or cancel it first.')
        run = uuid.uuid4().hex
        field = 'workflow_run_id' if kind=='workflow' else 'run_id'
        state[field] = run
        if kind=='workflow':
            state['workflow_output'] = ''
        async def work():
            self.emit(key, 'run/started', {'runId': run, 'text': text,**({'source':'retry'} if controlled_retry else {})})
            try:
                result = await execute()
                self.emit(key, 'run/completed', {'runId': run, 'status': 'completed', 'result': result})
            except asyncio.CancelledError:
                self.emit(key, 'run/completed', {'runId': run, 'status': 'cancelled'})
            except Exception as exc:
                from .workflow_io import WorkflowFailure
                extra={'exitCode':exc.exit_code,'result':{'workflowResult':exc.result}} if isinstance(exc,WorkflowFailure) else {}
                self.emit(key, 'run/completed', {'runId': run, 'status': 'failed', 'error': str(exc),**extra})
            finally:
                try:
                    self.publish_operation_receipts(key, run)
                finally:
                    if state[field] == run:
                        state[field] = None
        task = asyncio.create_task(work())
        self.tasks[run] = (key, task)
        self.task_kinds[run] = kind
        return {'runId': run, 'sessionId': key}

    def announce_operation(self, key, run, operation_id):
        # Save the lookup identity before granting execution, including full_access.
        # No claim of a transaction or a model/tool completion is made here.
        self.emit(key, 'client/event', {'type':'item.updated','sessionId':key,
            'item':{'id':'operation-receipt:'+operation_id,'runId':run,'kind':'notice',
                'title':'正式提交状态','text':'操作尚未确认提交；结果可通过回执核对。','state':'completed',
                'structured':{'operationReceipt':{'schema':'lg.operation-receipt.v1','operationId':operation_id,
                    'status':'unknown','effects':[],'toolOutcome':'unknown','derivedFiles':'not_attested',
                    'replayAllowed':False}}}})

    def publish_operation_receipts(self, key, run):
        from .operation_receipts import lookup
        state = self.session(key)
        for owner, operation_id in state.get('operation_ids', []):
            if owner != run:
                continue
            try:
                value = lookup(self.config.workspace, operation_id)
            except (OSError, ValueError, sqlite3.Error):
                value = {'schema':'lg.operation-receipt.v1','operationId':operation_id,'status':'unknown',
                    'effects':[],'toolOutcome':'unknown','derivedFiles':'not_attested','replayAllowed':False}
            text = {'committed': '数据库修改已提交；派生文件状态需另行核对。',
                'not_committed': '本次操作没有提交数据库修改。',
                'unknown': '提交结果尚不能确定；请查询回执并核对版本，勿直接重试。'}[value['status']]
            self.emit(key, 'client/event', {'type': 'item.updated', 'sessionId': key,
                'item': {'id': 'operation-receipt:' + operation_id, 'runId': run, 'kind': 'notice',
                    'title': '正式提交状态', 'text': text, 'state': 'completed',
                    'structured': {'operationReceipt': value}}})

    async def natural_task(self,key,author_text,history):
        from .intent_router import catalog,is_continue,resolve,Clarify,InterpretationError
        from .workspace_library import _stamp
        state=self.session(key)
        current=state['agent'].runtime.read().get('task')
        if current and is_continue(author_text):return {'inherit':True,'author':author_text}
        replies=[]
        for attempt in range(3):
            effective=author_text+('／作者补充：'+'／'.join(replies) if replies else '')
            try:
                before=await asyncio.to_thread(_stamp,self.config)
                inventory=await asyncio.to_thread(catalog,self.config.workspace,effective,current)
                context={'saved':inventory,'currentTask':current,'priorDiscussion':history,
                    'workflow':state['control'].status(),'clarifications':replies}
                value=await self.task_router(self.config,effective,context)
                if value.get('kind') not in {'discussion','control'} and value.get('decision')!='inherit' and before!=await asyncio.to_thread(_stamp,self.config):
                    raise InterpretationError('作品在识别范围期间发生变化，未发起写作。请刷新后重新请求。')
                resolved=resolve(self.config.workspace,value,effective,inventory,current)
                return {**resolved,'author':effective}
            except Clarify as exc:
                if attempt==2:raise ValueError('范围仍不明确，未发起写作。请从资料库选择对象或明确任务及版本。') from None
                answer=await self.approvals.request('item/tool/requestUserInput',{'questions':[{
                    'id':'literary_scope','header':'明确范围','question':str(exc),'options':[]}]},
                    owner=key,run_id=state['run_id'])
                values=(answer.get('answers',{}).get('literary_scope') or {}).get('answers')
                if not isinstance(values,list) or len(values)!=1 or not isinstance(values[0],str) or not values[0].strip() or len(values[0])>10000:
                    raise ValueError('未取得有效范围补充，未发起写作。')
                reply=values[0];replies.append(reply)
                item_id='scope-answer:'+state['run_id']+':'+str(attempt)
                self.store.append(key,'user',reply,run_id=state['run_id'],item_id=item_id,scope_clarification=True)
                self.emit(key,'client/event',{'type':'item.updated','sessionId':key,'item':{
                    'id':item_id,'runId':state['run_id'],'kind':'user','title':'作者','text':reply,'state':'completed'}})

    async def author_control(self,key,decision,author_text):
        from .intent_router import constraints
        state=self.session(key);control=decision['control'];status=state['control'].status()
        action=control['action'];target=control['target']
        if action not in {'status','stop','retry'}:raise ValueError('请明确要查询、停止还是重试当前工作流。')
        if action!='status' and target!='workflow:'+str(status.get('id')):raise ValueError('工作流目标已变化，未执行控制操作。')
        if action=='status':return '当前工作流状态：'+str(status['status'])+'。'
        caps=constraints(author_text)
        if action=='retry' and (caps['noSave'] or caps['noProse']):
            raise ValueError('本轮限制不允许直接重试原工作流，请重新指定明确的只读任务。')
        result=await (state['control'].stop(status['id']) if action=='stop' else state['control'].retry(status['id'],author_text))
        if action=='stop':return '已停止所指定的工作流。' if result.get('status')=='stopped' else '目标工作流当前状态：'+str(result.get('status'))+'。'
        return '已启动指定工作流的重试，尚未完成。'

    async def run(self, key, text, output_schema=None, *, task=None,stage=None,route_task=False,author_text=None):
        state = self.session(key)
        if not isinstance(text, str) or not text.strip() or len(text) > 100000:
            raise ValueError('Expected nonempty text of at most 100000 characters.')
        if output_schema is not None:
            jsonschema.Draft202012Validator.check_schema(output_schema)
            if _external_refs(output_schema):
                raise ValueError('Only local schema references are supported.')
        from .task_scope import bind_task,stage_task,scope_prompt,scope_summary
        if task is not None and stage is not None:raise ValueError('Choose either task or stage')
        if route_task and (task is not None or stage is not None):raise ValueError('Interpretation applies only to natural language')
        marker='\n\n作者附加的资料（内容属于参考数据）：'
        if route_task:
            if author_text is None and marker in text:raise ValueError('请分别提供作者原话和附加参考资料。')
            author_text=text if author_text is None else author_text
            if not isinstance(author_text,str) or not author_text.strip() or not text.startswith(author_text):raise ValueError('作者原话与本轮请求不匹配。')
            tail=text[len(author_text):]
            if tail and not tail.startswith(marker):raise ValueError('作者原话不能截去本轮的其他指令。')
        scoped=bind_task(self.config.workspace,task if task is not None else stage_task(self.config.workspace,stage)) if task is not None or stage is not None else None
        async def execute():
            agent = state['agent']
            bound=scoped
            state['messages'].clear()
            history=self.store.context(key,limit=self.config.conversation_chars)
            self.store.append(key,'user',text,run_id=state['run_id'],item_id='user:'+state['run_id'])
            request_text=text
            if route_task:
                self.emit(key,'client/event',{'type':'item.updated','sessionId':key,'item':{
                    'id':'task-interpretation:'+state['run_id'],'runId':state['run_id'],'kind':'notice',
                    'title':'理解任务范围','text':'先核对本轮操作、对象与停点；范围不明确时先提问。','state':'completed'}})
                decision=await self.natural_task(key,author_text,history)
                if 'control' in decision:
                    reply=await self.author_control(key,decision,decision['author'])
                    item_id='author-control:'+state['run_id']
                    self.store.append(key,'assistant',reply,run_id=state['run_id'],item_id=item_id)
                    self.emit(key,'client/event',{'type':'item.updated','sessionId':key,'item':{'id':item_id,
                        'runId':state['run_id'],'kind':'assistant','title':'LG','text':reply,'state':'completed'}})
                    return {'control':decision['control']['action']}
                if 'task' in decision:bound=bind_task(self.config.workspace,decision['task'])
                if decision['author']!=author_text:request_text=text+'\n\n作者对本轮范围的补充：\n'+decision['author']
            if bound:
                # Retire old runtime children before a new author scope becomes active.
                await agent.close()
                agent=runtime_factory(self.config,state['event'],agent_factory=self.agent_factory,
                    on_request=state['request'],conversation=key)
                agent.runtime.acquire()
                state['agent']=agent
                agent.runtime.update(task=bound)
                self.emit(key,'task/bound',{'task':bound,'requestSource':'natural' if route_task else 'explicit'})
                self.emit(key,'client/event',{'type':'item.updated','sessionId':key,'item':{
                    'id':'task-scope:'+bound['id'],'runId':state['run_id'],'kind':'notice',
                    'title':'本轮任务范围','text':scope_summary(bound),'state':'completed'}})
            agent.control_socket = await state['control'].endpoint()
            state['agent_run_id'] = state['run_id']
            state['source_run_id'] = state['run_id']
            state['source_epoch'] = uuid.uuid4().hex
            state['source_leases'].clear()
            state['source_observations'] = []
            state['source_unknown_reasons'] = ['prior_history_unobserved', 'runtime_instructions_unobserved',
                                               'unregistered_context_unobserved']
            def revoke_agent():
                state['agent_run_id'] = None
                state['source_leases'].clear()
                pending = list(state['agent_requests'])
                for caller in pending:
                    caller.cancel()
                async def drain():
                    await asyncio.gather(*pending, return_exceptions=True)
                    await state['control'].retire_endpoint()
                return drain()
            if hasattr(agent, 'set_retirement_handler'):
                agent.set_retirement_handler(revoke_agent)
            try:
                try:
                    from .generation_sources import make_receipt
                    agent.runtime.update(generation_sources=make_receipt(mode='live',run_id=state['source_run_id'],
                        observations=[],unknown_reasons=state['source_unknown_reasons']))
                except Exception:
                    raise ValueError('Generation source capture could not be initialized.') from None
                await agent.start(context=history)
                status = state['control'].status()
                request = request_text
                if status['status'] != 'idle':
                    request = ('Current UI-owned workflow (a status snapshot, use workflow_status for current state):\n'
                        + json.dumps(status, ensure_ascii=False) + '\n\nAuthor message:\n' + request_text)
                bound=agent.runtime.read().get('task')
                if bound:request=scope_prompt(bound)+'\n\nAuthor message:\n'+request
                result = await agent.run(request, output_schema=output_schema)
                if result.status == 'cancelled':
                    raise asyncio.CancelledError()
                if result.status != 'completed':
                    raise ValueError('Execution runtime did not complete the task')
                if output_schema is not None:
                    final = result.text or ''
                    if not final:
                        final = list(state['messages'].values())[-1] if state['messages'] else ''
                    value = json.loads(final)
                    jsonschema.validate(value, output_schema)
                    return {'output': value}
                return {'turnId': result.result_id}
            finally:
                state['agent_run_id'] = None
                state['source_leases'].clear()
                async def finish():
                    pending = list(state['agent_requests'])
                    for caller in pending:
                        caller.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    if getattr(agent, 'execution_retired', False):
                        await state['control'].retire_endpoint()
                    for item_id, value in state['messages'].items():
                        if value:
                            self.store.append(key, 'assistant', value, run_id=state['run_id'], item_id=item_id)
                cleanup = asyncio.create_task(finish())
                cancelled = False
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        cancelled = True
                cleanup.result()
                if cancelled:
                    raise asyncio.CancelledError()
        return self.schedule(key, execute, text=text,kind='agent')

    async def cancel(self, key):
        state = self.session(key)
        self.approvals.cancel(key)
        await state['control'].cancel_requests()
        owned = [task for owner, task in self.tasks.values() if owner == key and not task.done()]
        # Stop process groups even when an interrupt cannot be delivered.
        try:
            if state['control'].active:
                await state['control'].stop(state['control'].job['id'])
        finally:
            agent = state['agent']
            if agent.turn_id:
                try:
                    await asyncio.wait_for(agent.interrupt(), 2)
                except (Exception, asyncio.CancelledError):
                    pass
            for task in owned:
                task.cancel()
            await asyncio.gather(*owned, return_exceptions=True)
            await agent.close()
            new = runtime_factory(self.config, state['event'], agent_factory=self.agent_factory, on_request=state['request'], conversation=key)
            new.runtime.acquire()
            state['agent'] = new
        return {'status': 'cancelled', 'preservedResults': True}

    async def dispatch(self, method, params):
        from .backend_protocol import validate, contract
        validate(method, params)
        if method == 'protocol/schema':
            return contract()
        key = params.get('sessionId')
        if method == 'initialize':
            from .runtime_identity import runtime_identity
            return {'protocolVersion': 'lg.backend.v1', 'workspace': str(self.config.workspace), 'runtime': self.config.runtime_engine,
                'runtimeIdentity': runtime_identity(self.config),
                'model': self.config.default_model, 'capabilities': ['sessions', 'events', 'structuredOutput',
                'cancelTree', 'operationApprovals', 'creativeApprovals', 'referenceContracts', 'bookLibrary', 'clientEvents', 'pagedSnapshots','commandWorkflows','storyWorkflows','sceneApproval','runImport','acceptanceReview','runRecords','workflowResume','legacySessions','taskScopes','naturalTaskRouting','analysisScopes','studioCalls',
                'runtimeIdentity','operationReceipts','literaryTasks','boundedNarrative','sourceRevalidation']}
        if method == 'runtime/identity':
            from .runtime_identity import runtime_identity, verify_manifest
            value = runtime_identity(self.config)
            return verify_manifest(self.config, value) if params.get('verifyManifest') else value
        if method == 'operation/read':
            from .operation_receipts import lookup
            return await asyncio.to_thread(lookup, self.config.workspace, params['operationId'])
        if method in {'task/preview', 'task/read', 'narrative/preview'}:
            from .backend_tasks import preview
            return await preview(self, method, params)
        if method in {'task/start', 'task/advance', 'narrative/run'}:
            from .backend_tasks import start
            return start(self, key, method, params)
        if method == 'task/select':
            from .backend_tasks import select
            return await select(self, key, params)
        if method == 'sources/check':
            from .backend_tasks import sources
            return await asyncio.to_thread(sources, self, params)
        if method=='analysis/preview':
            from .backend_analysis import prepare
            return prepare(self.config,params)
        if method=='studio/preview':
            from .studio_operations import prepare
            return await prepare(self.config,params['operation'],params['arguments'])
        if method=='studio/call':
            from .backend_studio import call
            return await call(self,key,params)
        if method=='run/list':
            from .backend_runs import list_runs
            return await asyncio.to_thread(list_runs,self.config.workspace,params.get('limit',20))
        if method=='run/readPage':
            from .backend_runs import prepare_run
            run=params['runId']
            binding='run:'+key+':'+run
            page=params.get('page',0)
            if params.get('snapshotId'):
                result=self.history.page(binding,params['snapshotId'],page)
            else:
                if page!=0:raise ValueError('First run page must start at zero')
                snapshot=await asyncio.to_thread(prepare_run,self.config.workspace,key,run,include_events=params.get('events',False))
                result=self.history.freeze(snapshot)
            return {**result,'sessionId':key,'runId':run}
        if method == 'book/dashboard':
            from .studio import dashboard
            return await asyncio.to_thread(dashboard, self.config)
        if method == 'library/list':
            from .workspace_library import library
            return await asyncio.to_thread(library, self.config, params['topic'], params.get('query', ''), params.get('offset', 0))
        if method == 'library/read':
            from .workspace_library import entry
            return await asyncio.to_thread(entry, self.config, params['topic'], params['key'])
        if method == 'library/actionContext':
            from .workspace_library import writing_context
            return await asyncio.to_thread(writing_context, self.config,
                params['topic'], params['key'], params['action'], params['fingerprint'])
        if method == 'session/list':
            legacy,warning=await self.legacy.list(self.store)
            return {'sessions': [{'sessionId': k, 'title': v} for k, v in self.store.list()]+legacy,
                **({'legacyWarning':warning} if warning else {})}
        if method == 'session/create':
            return await self.open(self.store.create())
        if method == 'session/open':
            if key.startswith('legacy-'):
                from .legacy_sessions import imported_id
                target=imported_id(key)
                if not self.store.path(target).exists():
                    snapshot=await self.legacy.snapshot(key,self.store)
                    await asyncio.to_thread(self.store.import_history,target,snapshot)
                else:
                    await asyncio.to_thread(self.store.legacy_items,target)
                key=target
            return await self.open(key)
        if method == 'session/read':
            return {'messages': self.store.read(key)}
        if method == 'session/snapshot':
            if key.startswith('legacy-'):
                snapshot=await self.legacy.snapshot(key,self.store)
                return {'sessionId':key,'items':snapshot['items'],'cursor':0,'activeRuns':[]}
            messages = self.store.read(key)
            events, after = [], 0
            while page := self.journal.read(key, after, 1000):
                events.extend(page)
                after = page[-1]['cursor']
            return {'sessionId': key, 'items': snapshot_items(messages, events,prior_items=self.store.legacy_items(key)), 'cursor': after,
                'activeRuns':active_runs(events)}
        if method == 'session/snapshotPage':
            index = params.get('page', 0)
            if params.get('snapshotId'):
                return self.history.page(key, params['snapshotId'], index)
            if index != 0:
                raise ValueError('A first history page must start at zero.')
            if key.startswith('legacy-'):
                legacy=await self.legacy.snapshot(key,self.store)
                data=json.dumps({'sessionId':key,'items':legacy['items'],'cursor':0,'activeRuns':[]},ensure_ascii=False,separators=(',',':')).encode()
                snapshot={'sessionId':key,'cursor':0,'data':data,'sha256':hashlib.sha256(data).hexdigest()}
            else:
                snapshot = await asyncio.to_thread(prepare_snapshot, self.config.workspace, key)
            return self.history.freeze(snapshot)
        if method == 'session/rename':
            self.session(key)
            self.store.rename(key, params['title'])
            return {}
        if method == 'session/close':
            await self.cancel(key)
            state = self.sessions.pop(key)
            await state['control'].close()
            await state['agent'].close()
            return {}
        if method == 'events/read':
            self.store.read(key)
            return {'events': self.journal.read(key, params.get('after', 0), params.get('limit', 200))}
        if method == 'permissions/set':
            state = self.session(key)
            if self._active(key) or state['control'].active:
                raise ValueError('Change permissions only while idle.')
            permissions(params['mode'])
            creative = params.get('creativeMode', state['agent'].runtime.read()['creative_mode'])
            if creative not in {'ask', 'auto_review', 'full_access'}:
                raise ValueError('Creative mode must be ask, auto_review or full_access.')
            await self.cancel(key)
            return self.session(key)['agent'].runtime.update(mode=params['mode'], creative_mode=creative)
        if method == 'run/start':
            return await self.run(key, params['text'], params.get('outputSchema'),task=params.get('task'),stage=params.get('stage'),route_task=params.get('routeTask',False),author_text=params.get('authorText'))
        if method == 'run/steer':
            await self.session(key)['agent'].steer(params['text'])
            self.store.append(key, 'user', params['text'])
            return {}
        if method == 'run/cancel':
            return await self.cancel(key)
        if method == 'approval/resolve':
            self.approvals.resolve(params['approvalId'], params['result'])
            return {}
        if method == 'reference/contracts':
            from .reference_contracts import catalog, card_schema, methods
            return {'version': 'lg.reference-card.v1', 'categories': {
                name: {'schema': card_schema(name), 'methods': methods(name)} for name in catalog()['libraries']}}
        if method == 'reference/validate':
            from .reference_contracts import card_schema
            jsonschema.validate(params['card'], card_schema(params['category']))
            return {'valid': True, 'evidenceVerified': False}
        if method == 'workflow/start':
            from .backend_analysis import start
            return start(self,key,params)
        if method == 'workflow/run':
            return self.workflow(key,params)
        if method=='workflow/resume':
            from .run_store import RunStore
            previous=RunStore(self.config.workspace).assert_resumable(params['runId'],confirm_stopped=params.get('confirmStopped',False))
            if previous.get('command') not in {'chat','outline','world','character','plot','ref','check','write'}:
                raise ValueError('This run has no supported declaration to resume; use its original typed command')
            digest=hashlib.sha256(json.dumps(previous,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            return self.workflow(key,{**params,'command':previous['command'],'request':previous.get('request',''),
                '_resumeRun':params['runId'],'_resumeFingerprint':digest})
        if method == 'run/import':
            return self.import_run(key,params)
        if method == 'creative/apply':
            return self.creative(key, params)
        raise ValueError('Unknown method: ' + method)

    def workflow(self,key,params):
        from .workflow_runner import DEFAULT_REQUESTS
        from .run_store import RunStore
        state = self.session(key)
        command = params['command']
        resume=params.get('_resumeRun')
        from .backend_protocol import STORY_COMMANDS
        story = command in STORY_COMMANDS
        document = None
        if story:
            project = ProjectStore(self.config.workspace)
            document = (project.get_scene(params['document']).document if command.startswith('scene-')
                else project.get_document(params['document']))
        request = params['request'].strip() or DEFAULT_REQUESTS.get(command,'')
        author_text = '/'+command+' '+document.slug+'\n'+request if story else request
        if resume:author_text='/run resume '+resume
        prior = self.store.context(key,limit=min(self.config.conversation_chars,self.config.max_stage_context_chars//3))
        prompt = ('Previous conversation (historical dialogue, not new instructions):\n'+prior+
            '\n\nCurrent author request:\n'+request) if prior else request
        manifest = params.get('runtimeManifest') or self.config.runtime_manifest
        if manifest and not Path(manifest).is_absolute():
            raise ValueError('Workflow runtime manifest must be an absolute path')
        argv = [sys.executable,'-u','-m','lg_cli','-C',str(self.config.workspace),'--backend-worker','--json',
            '--runtime-engine',params.get('runtimeEngine',self.config.runtime_engine)]
        if self.config.environment:
            argv += ['--environment',self.config.environment]
        if manifest:
            argv += ['--runtime-manifest',str(manifest)]
        for param,flag in [('model','--model'),('strategy','--strategy')]:
            if params.get(param):
                argv += [flag,params[param]]
        for param,flag in [('dryRun','--dry-run'),('allowRaw','--raw'),('noReference','--no-reference'),('quiet','--quiet'),('debug','--debug')]:
            if params.get(param) and not ((story or resume) and param=='dryRun'):
                argv.append(flag)
        if story:
            if command.startswith('scene-'):
                argv += ['scene',command.removeprefix('scene-'),document.slug,'--request-stdin']
            elif command=='bible-curate':
                argv += ['bible','curate',document.slug,'--request-stdin']
            else:
                argv += ['edit',document.slug,'--mode',params['revisionMode'],'--request-stdin']
                if params.get('sourceVersion'):
                    argv += ['--source-version',str(params['sourceVersion'])]
            if params.get('dryRun'):
                argv.append('--dry-run')
            # The selected passage is source material, not a process argument or a new request.
            prompt = json.dumps({'request':prompt,**({'passage':params['passage']} if 'passage' in params else {})},ensure_ascii=False)
        elif resume:
            argv += ['run','resume',resume,'--expected-fingerprint',params['_resumeFingerprint']]
            if params.get('confirmStopped'):argv.append('--confirm-stopped')
            if params.get('dryRun'):argv.append('--dry-run')
            prompt=None
        else:
            argv.append(command)
        result, saved, owner = {}, set(), {}
        public = {'run.started','run.resumed','context.started','context.completed','stage.started','stage.completed',
            'stage.failed','artifact.written','run.planned','run.completed','run.failed'}
        def record(value):
            run = state['workflow_run_id'] or owner['runId']
            kind = value.get('type')
            if kind==('story.result' if story else 'run.result'):
                result.clear();result.update(value)
            elif kind in public:
                from .workflow_io import public_workflow_event
                value=public_workflow_event(value)
                self.emit(key,'workflow/record',{'record':value},run_id=run)
                if kind.startswith('stage.'):
                    status = {'stage.started':'running','stage.completed':'completed','stage.failed':'failed'}[kind]
                    self.emit(key,'task/updated',{'taskId':run+':'+str(value.get('stage_id')),
                        'name':str(value.get('stage_id') or command),'status':status,'step':value.get('message','')},run_id=run)
                else:
                    self.emit(key,'workflow/output',{'text':value.get('message','')+'\n'},run_id=run)
        def finish(code):
            if code or state['control'].status()['status']=='stopped':
                return
            if result.get('status') not in {'completed','planned'}:
                raise ValueError('Workflow returned no verified final result')
            run = owner['runId']
            attempt = state['control'].status()['attempt']
            if attempt in saved:
                return
            model_run = result.get('run_id')
            if not isinstance(model_run,str) or Path(model_run).name!=model_run or model_run in {'.','..'}:
                raise ValueError('Workflow returned an invalid local run ID')
            path = self.config.workspace/'.literarygiant'/'runs'/model_run
            if not path.resolve().is_relative_to(self.config.workspace.resolve()):
                raise ValueError('Workflow result points outside this book')
            actual = RunStore(self.config.workspace).load(model_run)
            if actual['status']!=result['status'] or actual['command']!=command or not isinstance(result.get('text'),str):
                raise ValueError('Workflow final result does not match its saved run')
            if resume and model_run!=resume and actual.get('resumed_from')!=resume:
                raise ValueError('Resumed workflow returned an unrelated run')
            if story and result.get('document',{}).get('id')!=document.id:
                raise ValueError('Story result does not match the selected document')
            text = result['text']
            item_id = 'workflow-assistant:'+run+':'+str(attempt)
            self.store.append(key,'assistant',text,run_id=run,item_id=item_id,workflow_run=model_run)
            self.emit(key,'client/event',{'type':'item.updated','sessionId':key,'item':{
                'id':item_id,'runId':run,'kind':'assistant','title':'LG','text':text,'state':'completed'}})
            saved.add(attempt)
        def outcome(code):
            from .workflow_io import WorkflowFailure,public_workflow_result
            if state['control'].status()['status']=='stopped':
                raise asyncio.CancelledError()
            model_run=result.get('run_id')
            verified=False
            if isinstance(model_run,str) and Path(model_run).name==model_run and model_run not in {'.','..'}:
                try:
                    actual=RunStore(self.config.workspace).load(model_run)
                    verified=actual['status']==result.get('status') and actual['command']==command and result.get('exit_code')==code
                except (ValueError,OSError,KeyError):pass
            projected=public_workflow_result(result,code=code,missing_credentials=code==3 and not self.config.credentials_configured) if verified else None
            if code or state['control'].status()['attempt'] not in saved:
                raise WorkflowFailure(projected['error'] if projected and code else 'Workflow did not return a verified final result; inspect saved runs',
                    exit_code=code if type(code) is int and code>0 else 1,result=projected)
            return {'workflowRunId':result['run_id'],'status':result['status'],'artifactPath':result.get('artifact_path'),
                **({'workflowResult':projected} if projected else {})}
        def retry(job):
            if state['control'].job is not job:
                raise ValueError('Workflow retry owner is stale')
            result.clear()
            async def execute_retry():
                host = state['control'].session
                host.begin_work()
                animation = asyncio.create_task(host._animate_progress())
                try:
                    return outcome(await state['control']._execute(job))
                finally:
                    animation.cancel();host.end_work()
                    await asyncio.gather(animation,return_exceptions=True)
            resumed = self.schedule(key,execute_retry,text='/'+command+' 重试：'+(job['guidance'] or request),
                kind='workflow',controlled_retry=True)
            owner.update(resumed)
            return resumed
        async def execute():
            self.store.append(key,'user',author_text,run_id=parent['runId'],item_id='user:'+parent['runId'],command=command)
            env = dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parent.parent))
            code = await state['control'].run(argv,cwd=self.config.workspace,env=env,structured=True,label='/'+command+' '+request,
                input_text=prompt,on_record=record,on_finish=finish,retry_executor=retry)
            return outcome(code)
        parent = self.schedule(key,execute,text=author_text,kind='workflow')
        owner.update(parent)
        return parent

    def import_run(self,key,params):
        from .run_store import RunStore
        params=dict(params)
        self.session(key)
        text='/run adopt '+params['runId']+' '+params['slug']
        async def execute():
            run=owner['runId']
            self.store.append(key,'user',text,run_id=run,item_id='user:'+run,command='run-import')
            result=RunStore(self.config.workspace).adopt(params['runId'],slug=params['slug'],
                kind=params['kind'],title=params['title'],accept=False)
            reply='已导入 '+result['slug']+' v'+str(result['version'])+'；采用需另行审批。'
            item_id='command-assistant:'+run
            self.store.append(key,'assistant',reply,run_id=run,item_id=item_id)
            self.emit(key,'client/event',{'type':'item.updated','sessionId':key,'item':{
                'id':item_id,'runId':run,'kind':'assistant','title':'LG','text':reply,'state':'completed'}})
            return result
        owner=self.schedule(key,execute,text=text)
        return owner

    async def review_acceptance(self,key,params,preview):
        state=self.session(key)
        argv=[sys.executable,'-u','-m','lg_cli.acceptance_review','-C',str(self.config.workspace),
            '--runtime-engine',self.config.runtime_engine]
        if self.config.environment:
            argv += ['--environment',self.config.environment]
        if self.config.runtime_manifest:
            argv += ['--runtime-manifest',str(self.config.runtime_manifest)]
        argv += ['version','accept',preview['document']['slug'],str(params['version'])]
        if params.get('final'):
            argv.append('--final')
        result={}
        def record(value):
            if value.get('type')=='acceptance.reviewed':
                result.update(value)
            elif value.get('type') in {'stage.started','stage.completed','stage.failed'}:
                self.emit(key,'task/updated',{'taskId':state['run_id']+':acceptance:'+str(value.get('stage_id')),
                    'name':str(value.get('stage_id')),'step':value.get('message',''),
                    'status':{'stage.started':'running','stage.completed':'completed','stage.failed':'failed'}[value['type']]},run_id=state['run_id'])
        def retry(_):
            raise ValueError('Request acceptance again with fresh creative approval; a review cannot replay adoption')
        env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parent.parent))
        code=await state['control'].run(argv,cwd=self.config.workspace,env=env,structured=True,
            label='候选采用前审查',on_record=record,retry_executor=retry)
        if state['control'].status()['status']=='stopped':
            raise asyncio.CancelledError()
        if code or result!={'type':'acceptance.reviewed','documentId':preview['document']['id'],
                'versionId':preview['version']['id'],'final':params.get('final',False)}:
            raise ValueError('Candidate review did not complete; manuscript retained. Inspect saved analysis runs.')

    def creative(self, key, params):
        params=dict(params)
        state = self.session(key)
        store = ProjectStore(self.config.workspace)
        action = params.get('action')
        if action == 'promoteFact':
            def current():
                return asdict(store.get_fact(params['factId']))
            def apply():
                return asdict(store.promote_fact(params['factId'],rationale=params.get('reason','Confirmed by author')))
        elif action=='acceptProposal':
            def current():
                proposal=store.get_proposal(params['proposalId'])
                if proposal.status!='proposed':raise ValueError('Proposal is no longer pending')
                return {'proposal':asdict(proposal),'source':store.proposal_source_status(proposal.id)}
            def apply():
                return asdict(store.accept_proposal(params['proposalId'],state=params.get('state','canonical')))
        elif action == 'acceptVersion':
            def current():
                return {'document': asdict(store.get_document(params['document'])),
                        'version': asdict(store.get_version(params['document'], params['version']))}
            def apply():
                return asdict(store.accept_version(params['document'], params['version'],
                    final=params.get('final',False),expected_version_id=preview['document']['active_version_id']))
        elif action == 'approveScene':
            def current():
                return asdict(store.get_scene(params['document']))
            scene = store.get_scene(params['document'])
            def apply():
                return asdict(store.approve_scene(params['document'],expected_scene=scene))
        else:
            raise ValueError('Unsupported creative action.')
        preview = current()
        from .mcp_approval import project_fingerprint
        book_before=project_fingerprint(self.config.workspace)
        digest = hashlib.sha256(json.dumps({'action':action,'preview':preview,'book':book_before,
            'final':params.get('final',False),'review':params.get('review',False),
            'state':params.get('state','canonical'),'reason':params.get('reason','')},sort_keys=True).encode()).hexdigest()
        if params.get('expectedFingerprint') is not None and params['expectedFingerprint']!=digest:
            raise ValueError('Creative preview fingerprint changed; review the current content before approving.')
        operation_id = uuid.uuid4().hex
        details={'action':action,'preview':preview,'final':params.get('final',False),'review':params.get('review',False),
            'operationId': operation_id}
        if action=='acceptProposal':details['state']=params.get('state','canonical')
        if action=='promoteFact':details['reason']=params.get('reason','Confirmed by author')
        async def execute():
            self.announce_operation(key, state['run_id'], operation_id)
            mode = state['agent'].runtime.read()['creative_mode']
            if mode == 'auto_review':
                from .creative_review import review_creative
                result = await review_creative(self.config, details,
                    on_event=lambda method, params: self.emit(key, 'review/event', {'method': method, 'params': params}))
                self.emit(key, 'approval/resolved', {'owner':key, 'kind':'creative', 'automatic':True, **result})
            else:
                result = await self.approvals.request('creative', {**details,'fingerprint':digest},
                    owner=key, mode=mode,run_id=state['run_id'])
            if result['decision'] != 'accept':
                raise asyncio.CancelledError()
            if current() != preview or project_fingerprint(self.config.workspace)!=book_before:
                raise ValueError('Creative target changed after review; request approval again.')
            if action=='acceptVersion' and params.get('review'):
                await self.review_acceptance(key,params,preview)
                if current()!=preview or project_fingerprint(self.config.workspace)!=book_before:
                    raise ValueError('Book changed during candidate review; request approval again.')
            from .operation_receipts import operation_scope, lookup
            if action == 'acceptVersion':
                binding = {'tool':'studio_accept', 'arguments':{'slug':preview['document']['slug'],
                    'version_number':params['version'], 'expected_version_id':preview['document']['active_version_id']}}
            elif action == 'acceptProposal':
                binding = {'tool':'studio_accept_proposal', 'arguments':{'proposal_id':params['proposalId'],
                    'state':params.get('state','canonical')}}
            elif action == 'approveScene':
                binding = {'action':action, 'arguments':{'document':preview['document']['id']}}
            else:
                binding = {'action':action, 'arguments':{'factId':params['factId']}}
            binding['approval'] = details
            state.setdefault('operation_ids', []).append((state['run_id'], operation_id))
            with operation_scope(self.config.workspace, operation_id, binding, book_before):
                value = apply()
            return {**value, 'operationReceipt':lookup(self.config.workspace, operation_id)}
        return self.schedule(key, execute)

    async def close(self):
        for key in list(self.sessions):
            await self.dispatch('session/close', {'sessionId': key})
        self.journal.db.close()
        self.history.clear()


def _external_refs(value):
    if isinstance(value, dict):
        return any((key in {'$ref', '$dynamicRef'} and not str(item).startswith('#')) or _external_refs(item)
                   for key, item in value.items())
    return isinstance(value, list) and any(_external_refs(item) for item in value)
