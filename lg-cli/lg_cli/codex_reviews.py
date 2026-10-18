"""Core dynamic-tool adapter for host-bound, non-replayable expert reviews."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re

import jsonschema

from .bounded_reviews import BoundedReviews, MAX_RESULT_BYTES, TERMINAL
from .native_model import NativeModel


class CodexReviews:
    def __init__(self, config, policy, *, runtime, root, turn, enabled, emit,
                 model_factory=NativeModel):
        self.config, self.policy, self.runtime = config, policy, runtime
        self.root, self.turn, self.enabled = root, turn, enabled
        self.tool = copy.deepcopy(policy.tool)
        self._emit = emit
        self._state = None
        self._restored = self._failed = False
        self._revoked = True
        self._calls = set()
        self.reviews = BoundedReviews(policy, config=lambda: self.config, scope=self._scope,
            parent=turn, enabled=lambda: not self._revoked and self.enabled(),
            model_factory=model_factory, save=self._save, emit=lambda child: self._emit(copy.deepcopy(child)))

    def _scope(self):
        return self.runtime.read().get('task') if self.runtime else None

    def available(self):
        return not self._failed and self.enabled() and self.policy.available(self._scope())

    @staticmethod
    def _identity(value):
        return isinstance(value, str) and 1 <= len(value) <= 128

    def _check_state(self, state):
        if (not isinstance(state, dict) or set(state) !=
            {'schema', 'rootThreadId', 'parentTurnId', 'used', 'seen', 'calls'}
            or state['schema'] != 'lg.codex-reviews.v1' or not self._identity(state['rootThreadId'])
            or state['parentTurnId'] is not None and not self._identity(state['parentTurnId'])):
            raise ValueError()
        used, seen, calls = state['used'], state['seen'], state['calls']
        for values in (used, seen):
            if (not isinstance(values, list) or len(values) > 2
                or any(not self._identity(value) for value in values) or len(set(values)) != len(values)):
                raise ValueError()
        if not isinstance(calls, dict) or set(calls) != set(seen):
            raise ValueError()
        if calls and state['parentTurnId'] is None:
            raise ValueError()
        experts, children_seen = set(), set()
        for identity, entry in calls.items():
            if (not self._identity(identity) or not isinstance(entry, dict)
                or set(entry) != {'argumentsHash', 'experts', 'record', 'settled', 'interrupted'}
                or not isinstance(entry['argumentsHash'], str)
                or not re.fullmatch('[0-9a-f]{64}', entry['argumentsHash'])
                or type(entry['settled']) is not bool or type(entry['interrupted']) is not bool):
                raise ValueError()
            names = entry['experts']
            jsonschema.validate(names, self.tool['parameters']['properties']['experts'])
            if (not isinstance(names, list) or not 1 <= len(names) <= 2
                or any(not self._identity(name) for name in names) or len(set(names)) != len(names)
                or experts.intersection(names)):
                raise ValueError()
            experts.update(names)
            record = entry['record']
            if (not isinstance(record, dict) or set(record) !=
                {'schema', 'parentTurn', 'taskId', 'taskFingerprint', 'sourceFingerprint', 'children'}
                or record['schema'] != 'lg.expert-review.v1'
                or record['parentTurn'] != state['parentTurnId']
                or any(not self._identity(record[key]) for key in ('taskId', 'taskFingerprint', 'sourceFingerprint'))
                or not isinstance(record['children'], list)
                or [child.get('expert') if isinstance(child, dict) else None for child in record['children']] != names):
                raise ValueError()
            for child in record['children']:
                if (set(child) != {'id', 'expert', 'state', 'result', 'error'}
                    or not self._identity(child['id']) or child['id'] in children_seen
                    or child['state'] not in TERMINAL | {'waiting', 'running'}
                    or child['error'] not in {None, 'expert_analysis_failed', 'review_source_changed', 'process_interrupted'}
                    or entry['settled'] and child['state'] not in TERMINAL):
                    raise ValueError()
                children_seen.add(child['id'])
                if child['state'] == 'completed':
                    if child['error'] is not None or len(json.dumps(child['result'], ensure_ascii=False).encode()) > MAX_RESULT_BYTES:
                        raise ValueError()
                    jsonschema.validate(child['result'], self.policy.output_schema(child['expert']))
                elif child['result'] is not None:
                    raise ValueError()
        if experts != set(used) or len(experts) > 2:
            raise ValueError()

    def _write(self):
        if self._failed:
            raise ValueError('Codex expert checkpoint is unavailable; reopen and inspect the session.')
        try:
            if self.runtime:
                self.runtime.update(codex_delegations=copy.deepcopy(self._state))
        except Exception:
            self._failed = self._revoked = True
            self.reviews.live.clear()
            for task in self.reviews.tasks:
                task.cancel()
            raise ValueError('Codex expert checkpoint could not be saved; reopen and inspect the session.') from None

    def _save(self):
        for identity, record in self.reviews.records.items():
            self._state['calls'][identity]['record'] = copy.deepcopy(record)
        self._state['used'] = sorted(self.reviews.used)
        self._state['seen'] = sorted(self.reviews.seen)
        self._write()

    def restore(self):
        if self._failed:
            raise ValueError('Codex expert checkpoint is unavailable; reopen and inspect the session.')
        try:
            root = self.root()
            if not self._identity(root):
                raise ValueError()
            if self._restored:
                if self._state['rootThreadId'] != root:
                    raise ValueError()
                return
            state = self.runtime.read().get('codex_delegations') if self.runtime else None
            if state is None:
                self._state = {'schema': 'lg.codex-reviews.v1', 'rootThreadId': root,
                    'parentTurnId': None, 'used': [], 'seen': [], 'calls': {}}
            else:
                self._check_state(state)
                if state['rootThreadId'] != root:
                    raise ValueError()
                self._state = copy.deepcopy(state)
                for entry in self._state['calls'].values():
                    if not entry['settled']:
                        for child in entry['record']['children']:
                            if child['state'] not in TERMINAL:
                                child.update(state='cancelled', result=None, error='process_interrupted')
                        entry.update(settled=True, interrupted=True)
                self._write()
            self.reviews.parent_turn = self._state['parentTurnId']
            self.reviews.used = set(self._state['used'])
            self.reviews.seen = set(self._state['seen'])
            self._restored = True
        except Exception:
            self._failed = self._revoked = True
            raise ValueError('Invalid or unavailable Codex expert checkpoint; no expert request was replayed.') from None

    def begin(self):
        if self._failed or not self._restored or self._state['rootThreadId'] != self.root():
            raise ValueError('Codex expert recovery must finish before beginning a parent turn.')
        self.reviews.begin()
        self._revoked = False
        self._state.update(parentTurnId=self.turn(), used=[], seen=[], calls={})
        self._write()

    @staticmethod
    def _response(value):
        return {'contentItems': [{'type': 'inputText', 'text': json.dumps(value, ensure_ascii=False)}],
            'success': value['status'] == 'completed'}

    @classmethod
    def _error(cls):
        return cls._response({'status': 'failed', 'provisional': True, 'sourceCurrent': False,
            'error': 'expert_review_unavailable', 'experts': []})

    def _validate_call(self, params):
        if (self._failed or not self._restored or self._revoked or not self.available()
            or not isinstance(params, dict)
            or set(params) - {'threadId', 'turnId', 'callId', 'namespace', 'tool', 'arguments'}
            or params.get('namespace') is not None or params.get('tool') != self.tool['name']
            or params.get('threadId') != self.root() or params.get('threadId') != self._state['rootThreadId']
            or params.get('turnId') != self.turn() or params.get('turnId') != self.reviews.parent_turn
            or not self._identity(params.get('callId')) or not isinstance(params.get('arguments'), dict)
            or params['callId'] in self._state['calls']):
            raise ValueError('Expert request is outside the current host-bound parent turn.')

    async def call(self, params):
        identity = None
        caller = asyncio.current_task()
        self._calls.add(caller)
        try:
            self._validate_call(params)
            identity, arguments = params['callId'], copy.deepcopy(params['arguments'])
            self.reviews.prepare(identity, arguments)
            record = self.reviews.records[identity]
            self._state['calls'][identity] = {'argumentsHash': hashlib.sha256(json.dumps(
                arguments, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                'experts': list(arguments['experts']), 'record': copy.deepcopy(record),
                'settled': False, 'interrupted': False}
            self._save()
            value = await self.reviews.execute(identity)
            interrupted = (self._revoked or self._failed or self.root() != params['threadId']
                or self.turn() != params['turnId'] or not self.available())
            entry = self._state['calls'][identity]
            entry.update(settled=True, interrupted=entry['interrupted'] or interrupted)
            if entry['interrupted']:
                value = self.reviews.result(record, interrupted=True)
            self._save()
            return self._response(value)
        except asyncio.CancelledError:
            self.revoke()
            return self._error()
        except Exception:
            if identity in self.reviews.records:
                self.revoke()
            return self._error()
        finally:
            if identity is not None and identity not in self.reviews.live:
                self.reviews.finish(identity)
            self._calls.discard(caller)

    def revoke(self):
        self._revoked = True
        if self._state is None:
            return
        current = asyncio.current_task() if self._calls else None
        for task in self._calls:
            if task is not current:
                task.cancel()
        try:
            self.reviews.settle_interrupted()
            if self._state:
                for entry in self._state['calls'].values():
                    if not entry['settled']:
                        entry.update(settled=True, interrupted=True)
                self._save()
        except Exception:
            self._failed = True

    async def drain(self):
        current = asyncio.current_task()
        while True:
            pending = [task for task in self._calls | self.reviews.tasks if task is not current and not task.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    @property
    def pending(self):
        return bool(self.reviews.live or any(not task.done() for task in self._calls | self.reviews.tasks)
            or any(child['state'] not in TERMINAL for record in self.reviews.records.values() for child in record['children']))
