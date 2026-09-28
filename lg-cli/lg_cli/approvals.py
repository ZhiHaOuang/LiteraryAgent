"""Host-owned approval requests; replies never execute arbitrary client code."""
from __future__ import annotations

import asyncio
import uuid


class ApprovalBroker:
    def __init__(self, emit):
        self.emit = emit
        self.pending = {}

    async def request(self, kind, details, *, owner, mode='ask'):
        if mode == 'full_access':
            self.emit('approval/resolved', {'owner': owner, 'kind': kind, 'decision': 'accept', 'automatic': True})
            return {'decision': 'accept'}
        key = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = (future, owner, kind)
        self.emit('approval/requested', {'approvalId': key, 'owner': owner, 'kind': kind, 'details': details})
        try:
            return await future
        finally:
            self.pending.pop(key, None)

    def resolve(self, key, result):
        if key not in self.pending:
            raise ValueError('Approval is expired or already resolved.')
        future, owner, kind = self.pending[key]
        if future.done():
            raise ValueError('Approval is already resolved.')
        if kind == 'item/tool/requestUserInput':
            if not isinstance(result.get('answers'), dict):
                raise ValueError('Expected answers object.')
            if any(not isinstance(v, dict) or not isinstance(v.get('answers'), list)
                   or any(not isinstance(a, str) for a in v['answers']) for v in result['answers'].values()):
                raise ValueError('Invalid question answers.')
        elif result not in ({'decision':'accept'}, {'decision':'decline'}, {'decision':'cancel'}):
            raise ValueError('Expected accept, decline or cancel.')
        future.set_result(result)
        self.emit('approval/resolved', {'approvalId': key, 'owner': owner, 'result': result})

    def cancel(self, owner):
        for key, (future, target, kind) in list(self.pending.items()):
            if target == owner and not future.done():
                future.cancel()
                self.emit('approval/cancelled', {'approvalId': key, 'owner': owner})
