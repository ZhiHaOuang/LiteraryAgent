"""Immutable, connection-local history pages; no new authoritative story state."""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import time
import uuid
from collections import OrderedDict
from contextlib import closing

from .backend_events import active_runs, snapshot_items
from .conversation_store import ConversationStore

PAGE_BYTES = 256 * 1024
CACHE_LIMIT = 4
TTL_SECONDS = 180


def collect_snapshot(workspace, session):
    """Use a separate read connection so rebuilding history does not block the RPC loop."""
    store = ConversationStore(workspace)
    path = store.path(session)
    def signature():
        value = path.stat()
        return (value.st_ino, value.st_size, value.st_mtime_ns)
    before = signature()
    legacy_path=path.with_suffix('.legacy.json')
    def legacy_signature():
        if not legacy_path.exists():return None
        value=legacy_path.stat()
        return value.st_ino,value.st_size,value.st_mtime_ns
    legacy_before=legacy_signature()
    journal = workspace / '.literarygiant' / 'client-events.sqlite3'
    with closing(sqlite3.connect(journal.as_uri() + '?mode=ro', uri=True)) as database:
        database.execute('BEGIN')
        cursor = database.execute('SELECT COALESCE(MAX(id),0) FROM events WHERE session=?', (session,)).fetchone()[0]
        messages = store.read(session)
        def events():
            for raw, in database.execute('SELECT event FROM events WHERE session=? AND id<=? ORDER BY id', (session, cursor)):
                yield json.loads(raw)
        prior=store.legacy_items(session)
        items = snapshot_items(messages, events(),**({'prior_items':prior} if prior else {}))
        active = active_runs(events())
        database.commit()
        latest = database.execute('SELECT COALESCE(MAX(id),0) FROM events WHERE session=?', (session,)).fetchone()[0]
    if before != signature() or cursor != latest or legacy_before!=legacy_signature():
        raise ValueError('Session changed while reading history; refresh before continuing.')
    return {'sessionId': session, 'items': items, 'cursor': cursor,'activeRuns':active}


def prepare_snapshot(workspace, session):
    snapshot = collect_snapshot(workspace, session)
    data = json.dumps(snapshot, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return {'sessionId': session, 'cursor': snapshot['cursor'], 'data': data,
        'sha256': hashlib.sha256(data).hexdigest()}


class SnapshotPages:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.snapshots = OrderedDict()

    def freeze(self, snapshot):
        key = uuid.uuid4().hex
        now = self.clock()
        for old in list(self.snapshots):
            if self.snapshots[old]['expires'] <= now:
                self.snapshots.pop(old)
        while len(self.snapshots) >= CACHE_LIMIT:
            self.snapshots.popitem(last=False)
        self.snapshots[key] = {'session': snapshot['sessionId'], 'cursor': snapshot['cursor'],
            'data': snapshot['data'], 'sha256': snapshot['sha256'], 'expires': now + TTL_SECONDS}
        return self.page(snapshot['sessionId'], key, 0)

    def page(self, session, key, index):
        cached = self.snapshots.get(key)
        if cached is None or cached['expires'] <= self.clock():
            self.snapshots.pop(key, None)
            raise ValueError('History snapshot expired; request a new first page.')
        if cached['session'] != session:
            raise ValueError('History snapshot belongs to another session.')
        pages = (len(cached['data']) + PAGE_BYTES - 1) // PAGE_BYTES
        if type(index) is not int or not 0 <= index < pages:
            raise ValueError('Invalid history page index.')
        self.snapshots.move_to_end(key)
        return {'sessionId': session, 'snapshotId': key, 'cursor': cached['cursor'],
            'page': index, 'pages': pages, 'sha256': cached['sha256'],
            'data': base64.b64encode(cached['data'][index * PAGE_BYTES:(index + 1) * PAGE_BYTES]).decode('ascii'),
            'nextPage': index + 1 if index + 1 < pages else None}

    def clear(self):
        self.snapshots.clear()
