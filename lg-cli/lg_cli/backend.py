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

import jsonschema

from .approvals import ApprovalBroker
from .conversation_store import ConversationStore
from .live_agent import LiveAgent
from .project_store import ProjectStore
from .session_runtime import permissions
from .workflow_control import WorkflowControl


class EventJournal:
    def __init__(self, workspace):
        path = workspace / '.literarygiant' / 'client-events.sqlite3'
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, session TEXT, event TEXT)')
        self.db.commit()

    def append(self, session, method, params):
        record = {'method': method, 'params': params, 'sessionId': session}
        with self.db:
            cursor = self.db.execute('INSERT INTO events(session,event) VALUES (?,?)', (session, json.dumps(record, ensure_ascii=False)))
        return {'cursor': cursor.lastrowid, **record}

    def read(self, session, after=0, limit=200):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Invalid event cursor or limit.')
        return [{'cursor': key, **json.loads(event)} for key, event in self.db.execute(
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
        self.process = await asyncio.create_subprocess_exec(*argv, cwd=kwargs.get('cwd'), env=kwargs.get('env'),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        try:
            while chunk := await self.process.stdout.read(8192):
                self.append(chunk.decode('utf-8', errors='replace'))
            return await self.process.wait()
        finally:
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
    def __init__(self, config, notify=lambda record: None, *, agent_factory=LiveAgent):
        if not ProjectStore(config.workspace).initialized:
            raise ValueError('Choose an initialized book workspace.')
        self.config, self.notify, self.agent_factory = config, notify, agent_factory
        self.store = ConversationStore(config.workspace)
        self.journal = EventJournal(config.workspace)
        self.sessions, self.tasks = {}, {}
        self.approvals = ApprovalBroker(self._approval_event)

    def emit(self, session, method, params):
        record = self.journal.append(session, method, params)
        self.notify(record)

    def _approval_event(self, method, params):
        self.emit(params['owner'], method, params)

    def session(self, key):
        if key not in self.sessions:
            raise ValueError('Session is not open in this client.')
        return self.sessions[key]

    async def open(self, key):
        if key in self.sessions:
            return {'sessionId': key, **self.sessions[key]['agent'].runtime.read()}
        self.store.read(key)
        messages = {}
        def event(method, params):
            main = params.get('threadId') in {None, agent.thread_id}
            if main and method == 'item/agentMessage/delta':
                item = params.get('itemId', '')
                messages[item] = messages.get(item, '') + params.get('delta', '')
            elif main and method == 'item/completed' and params.get('item', {}).get('type') == 'agentMessage':
                item = params['item']
                messages[item['id']] = item.get('text', messages.get(item['id'], ''))
            self.emit(key, 'engine/event', {'method': method, 'params': params})
        async def request(method, params):
            if method == 'item/permissions/requestApproval':
                result = await self.approvals.request('permissions', params, owner=key)
                return {'permissions': params.get('permissions', {}) if result['decision'] == 'accept' else {}, 'scope': 'turn'}
            if method == 'mcpServer/elicitation/request':
                return {'action': 'decline', 'content': None}
            return await self.approvals.request(method, params, owner=key)
        agent = self.agent_factory(self.config, event, on_request=request, conversation=key)
        agent.runtime.acquire()
        host = ProcessHost(lambda method, params: self.emit(key, method, params))
        control = WorkflowControl(host)
        async def approve(details):
            mode = agent.runtime.read()['mode']
            if mode == 'auto_review':
                from .creative_review import review_creative
                result = await review_creative(self.config, {'operation': details},
                    on_event=lambda method, params: self.emit(key, 'review/event', {'method':method, 'params':params}))
                self.emit(key, 'approval/resolved', {'owner':key,'kind':'operation','automatic':True,**result})
                return result
            return await self.approvals.request('operation', details, owner=key, mode=mode)
        control.approval_handler = approve
        unfinished = {}
        for row in self.journal.db.execute('SELECT event FROM events WHERE session=? ORDER BY id', (key,)):
            prior = json.loads(row[0])
            if prior['method'] == 'run/started':
                unfinished[prior['params']['runId']] = prior
            elif prior['method'] == 'run/completed':
                unfinished.pop(prior['params']['runId'], None)
        for run in unfinished:
            self.emit(key, 'run/completed', {'runId':run, 'status':'interrupted', 'reason':'Previous client disconnected; completed artifacts retained.'})
        self.sessions[key] = {'agent': agent, 'control': control, 'messages': messages, 'request': request, 'event': event}
        return {'sessionId': key, **agent.runtime.read()}

    def _active(self, key):
        return any(owner == key and not task.done() for owner, task in self.tasks.values())

    def schedule(self, key, execute):
        self.session(key)
        if self._active(key):
            raise ValueError('A run is already active; steer or cancel it first.')
        run = uuid.uuid4().hex
        async def work():
            self.emit(key, 'run/started', {'runId': run})
            try:
                result = await execute()
                self.emit(key, 'run/completed', {'runId': run, 'status': 'completed', 'result': result})
            except asyncio.CancelledError:
                self.emit(key, 'run/completed', {'runId': run, 'status': 'cancelled'})
            except Exception as exc:
                self.emit(key, 'run/completed', {'runId': run, 'status': 'failed', 'error': str(exc)})
        task = asyncio.create_task(work())
        self.tasks[run] = (key, task)
        return {'runId': run, 'sessionId': key}

    async def run(self, key, text, output_schema=None):
        state = self.session(key)
        if not isinstance(text, str) or not text.strip() or len(text) > 100000:
            raise ValueError('Expected nonempty text of at most 100000 characters.')
        if output_schema is not None:
            jsonschema.Draft202012Validator.check_schema(output_schema)
            if _external_refs(output_schema):
                raise ValueError('Only local schema references are supported.')
        async def execute():
            agent = state['agent']
            state['messages'].clear()
            agent.control_socket = await state['control'].endpoint()
            await agent.start(context=self.store.context(key, limit=self.config.conversation_chars))
            self.store.append(key, 'user', text)
            try:
                result = await agent.run(text, output_schema=output_schema)
                if result.get('status') == 'interrupted':
                    raise asyncio.CancelledError()
                if output_schema is not None:
                    final = next((item.get('text', '') for item in reversed(result.get('items', []))
                                  if item.get('type') == 'agentMessage'), '')
                    if not final:
                        final = list(state['messages'].values())[-1] if state['messages'] else ''
                    value = json.loads(final)
                    jsonschema.validate(value, output_schema)
                    return {'output': value}
                return {'turnId': result.get('id')}
            finally:
                for value in state['messages'].values():
                    if value:
                        self.store.append(key, 'assistant', value)
        return self.schedule(key, execute)

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
            new = self.agent_factory(self.config, state['event'], on_request=state['request'], conversation=key)
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
            return {'protocolVersion': 'lg.backend.v1', 'capabilities': ['sessions', 'events', 'structuredOutput',
                'cancelTree', 'operationApprovals', 'creativeApprovals', 'referenceContracts']}
        if method == 'session/list':
            return {'sessions': [{'sessionId': k, 'title': v} for k, v in self.store.list()]}
        if method == 'session/create':
            return await self.open(self.store.create())
        if method == 'session/open':
            return await self.open(key)
        if method == 'session/read':
            return {'messages': self.store.read(key)}
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
            return await self.run(key, params['text'], params.get('outputSchema'))
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
            state = self.session(key)
            category = params.get('category')
            from .book_analysis import CATEGORIES
            if category not in CATEGORIES or not isinstance(params.get('chapter'), str):
                raise ValueError('Expected an analysis category and chapter.')
            chapter = ProjectStore(self.config.workspace).get_document(params['chapter'], kind='chapter').slug
            async def execute():
                decision = await state['control'].approval_handler({'operation': 'analysis', 'chapter': chapter,
                    'category': category})
                if decision['decision'] != 'accept':
                    raise asyncio.CancelledError()
                code = await state['control'].run([sys.executable, '-m', 'lg_cli', '-C', str(self.config.workspace),
                    'analysis', 'chapter', chapter, '--category', category], cwd=self.config.workspace)
                if code:
                    raise ValueError('Analysis workflow failed; inspect workflow/output events.')
                return {'status': 'completed'}
            return self.schedule(key, execute)
        if method == 'creative/apply':
            return self.creative(key, params)
        raise ValueError('Unknown method: ' + method)

    def creative(self, key, params):
        state = self.session(key)
        store = ProjectStore(self.config.workspace)
        action = params.get('action')
        if action == 'promoteFact':
            def current():
                return asdict(store.get_fact(params['factId']))
            def apply():
                return asdict(store.promote_fact(params['factId']))
        elif action == 'acceptVersion':
            def current():
                return {'document': asdict(store.get_document(params['document'])),
                        'version': asdict(store.get_version(params['document'], params['version']))}
            def apply():
                return asdict(store.accept_version(params['document'], params['version']))
        else:
            raise ValueError('Creative action must be promoteFact or acceptVersion.')
        preview = current()
        digest = hashlib.sha256(json.dumps(preview, sort_keys=True).encode()).hexdigest()
        async def execute():
            mode = state['agent'].runtime.read()['creative_mode']
            if mode == 'auto_review':
                from .creative_review import review_creative
                result = await review_creative(self.config, {'action': action, 'preview': preview},
                    on_event=lambda method, params: self.emit(key, 'review/event', {'method': method, 'params': params}))
                self.emit(key, 'approval/resolved', {'owner':key, 'kind':'creative', 'automatic':True, **result})
            else:
                result = await self.approvals.request('creative', {'action': action, 'preview': preview,
                    'fingerprint': digest}, owner=key, mode=mode)
            if result['decision'] != 'accept':
                raise asyncio.CancelledError()
            if current() != preview:
                raise ValueError('Creative target changed after review; request approval again.')
            return apply()
        return self.schedule(key, execute)

    async def close(self):
        for key in list(self.sessions):
            await self.dispatch('session/close', {'sessionId': key})
        self.journal.db.close()


def _external_refs(value):
    if isinstance(value, dict):
        return any((key in {'$ref', '$dynamicRef'} and not str(item).startswith('#')) or _external_refs(item)
                   for key, item in value.items())
    return isinstance(value, list) and any(_external_refs(item) for item in value)
