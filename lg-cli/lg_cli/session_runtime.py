"""Shared CLI/extension session identity, permissions and exclusive ownership."""
from __future__ import annotations

import fcntl
import json

from .conversation_store import ConversationStore
from .output_writer import _atomic_text

MODES = {'ask', 'auto_review', 'full_access'}


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

    def release(self):
        if self._lock:
            self._lock.close()
            self._lock = None
