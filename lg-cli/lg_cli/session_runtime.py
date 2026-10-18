"""Shared CLI/extension session identity, permissions and exclusive ownership."""
from __future__ import annotations

import fcntl
import json

from .conversation_store import ConversationStore
from .output_writer import _atomic_text

MODES = {'ask', 'auto_review', 'full_access'}
CREATIVE_TOOLS = frozenset({'studio_accept', 'studio_adopt_reference', 'studio_accept_proposal',
    'studio_select_decision', 'studio_withdraw_decision', 'studio_knowledge_confirm', 'studio_knowledge_withdraw'})


def operation_policy(settings: dict, details: dict) -> tuple[str, str]:
    """Select permission from the actual host-bound tool, never a model-supplied classification."""
    kind = 'creative' if details.get('tool') in CREATIVE_TOOLS else 'operation'
    mode = settings.get('creative_mode' if kind == 'creative' else 'mode', 'ask')
    permissions(mode)
    return kind, mode


def permissions(mode: str) -> dict:
    if mode not in MODES:
        raise ValueError('Permission mode must be ask, auto_review or full_access.')
    return {'approvalPolicy': 'never' if mode == 'full_access' else 'on-request',
            'approvalsReviewer': 'auto_review' if mode == 'auto_review' else 'user',
            'sandbox': 'danger-full-access' if mode == 'full_access' else 'workspace-write'}


class SessionRuntime:
    def __init__(self, workspace, conversation):
        self.store = ConversationStore(workspace)
        self.store.read(conversation)
        self.path = self.store.path(conversation).with_suffix('.runtime.json')
        self.lock_path = self.store.path(conversation).with_suffix('.lock')
        self._lock = None

    def read(self):
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {'schema': 'lg.session.v1', 'thread_id': None, 'mode': 'ask', 'creative_mode': 'ask'}
        if data.get('schema') != 'lg.session.v1' or data.get('mode') not in MODES or data.get('creative_mode') not in MODES:
            raise ValueError('Invalid session runtime metadata.')
        if data.get('task') is not None:
            from .task_scope import normalize_definition
            task=data['task']
            if not isinstance(task,dict) or task.get('schema')!='lg.task.v1' or not isinstance(task.get('id'),str):
                raise ValueError('Invalid literary task metadata')
            normalize_definition(task.get('definition'))
            if any(type(task.get(key)) is not int or task[key]<0 for key in ('grants','overviewGrants')):
                raise ValueError('Invalid literary task grant count')
            if type(task.get('revisionGrants',0)) is not int or task.get('revisionGrants',0)<0:
                raise ValueError('Invalid literary task revision grant count')
        return data

    def update(self, **values):
        if self._lock is None:
            raise RuntimeError('Session ownership required.')
        data = {**self.read(), **values}
        permissions(data['mode'])
        if data['creative_mode'] not in MODES:
            raise ValueError('Creative mode must be ask, auto_review or full_access.')
        _atomic_text(self.path, json.dumps(data, ensure_ascii=False) + '\n')
        self.path.chmod(0o600)
        return data

    def acquire(self):
        if self._lock:
            return
        stream = self.lock_path.open('a+')
        self.lock_path.chmod(0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stream.close()
            raise ValueError('This conversation is in use by another CLI or extension. Close it there first.') from None
        self._lock = stream

    def select_engine(self, name):
        if name not in {'codex', 'native'}:
            raise ValueError('Unknown execution engine')
        data = self.read()
        previous = data.get('engine', 'codex')
        bindings = dict(data.get('engine_threads', {}))
        if data.get('thread_id'):
            bindings[previous] = data['thread_id']
        self.update(engine=name, engine_threads=bindings,
            thread_id=data.get('thread_id') if previous == name else bindings.get(name))

    def release(self):
        if self._lock:
            self._lock.close()
            self._lock = None
