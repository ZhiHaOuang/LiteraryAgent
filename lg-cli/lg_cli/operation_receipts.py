"""Database commit evidence, not an authorization token or a tool replay log.

Only explicitly bound formal tools participate. Derived files are not attested;
an interrupted pending operation remains unknown until a DB effect is proven.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path
import re
import sqlite3


SUPPORTED = frozenset({'studio_accept', 'studio_adopt_reference', 'studio_accept_proposal',
    'studio_select_decision', 'studio_withdraw_decision', 'studio_knowledge_confirm',
    'studio_knowledge_withdraw', 'promoteFact', 'approveScene'})
_active = ContextVar('lg_operation_receipt', default=None)
_SCHEMA = '''CREATE TABLE IF NOT EXISTS operation_receipts (
    operation_id TEXT PRIMARY KEY, binding TEXT NOT NULL, tool TEXT NOT NULL,
    state TEXT NOT NULL, effects TEXT NOT NULL, outcome TEXT NOT NULL)'''


def _identity(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
        raise ValueError('Invalid operation identity')


def _database(workspace):
    root = Path(workspace).resolve()
    path = root / '.literarygiant' / 'story.sqlite3'
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError('Operation receipts must remain in the selected project')
    return path


def binding_fingerprint(details, book_fingerprint):
    return hashlib.sha256(json.dumps({'details': details, 'book': book_fingerprint},
        sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


@contextmanager
def _connect(path, *, create=False):
    # mode=rw refuses to create an unrelated/empty story database.
    connection = sqlite3.connect(path.as_uri() + ('?mode=rwc' if create else '?mode=rw'), uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _read(path, operation_id):
    if not path.exists():
        return None
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='operation_receipts'").fetchone():
            return None
        row = connection.execute('SELECT * FROM operation_receipts WHERE operation_id=?', (operation_id,)).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def _pending_path(database):
    path = database.with_name('operation-receipts.sqlite3')
    if path.is_symlink() or not path.resolve().is_relative_to(database.parent.resolve()):
        raise ValueError('Operation attempts must remain in the selected project')
    return path


def lookup(workspace, operation_id):
    """Read-only proof lookup. Missing/pending never means definitely not committed."""
    _identity(operation_id)
    result = {'schema': 'lg.operation-receipt.v1', 'operationId': operation_id,
        'status': 'unknown', 'effects': [], 'toolOutcome': 'unknown',
        'derivedFiles': 'not_attested', 'replayAllowed': False}
    path = _database(workspace)
    if not path.exists():
        return result
    # Read termination before commit evidence. The opposite order could miss a
    # commit, then observe its just-finished attempt and falsely say noncommit.
    attempt = _read(_pending_path(path), operation_id)
    committed = _read(path, operation_id)
    row = committed or attempt
    if row is None:
        return result
    return {**result, 'status': row['state'] if row['state'] in {'committed', 'not_committed'} else 'unknown',
        'effects': json.loads(row['effects']), 'toolOutcome': attempt['outcome'] if attempt else 'unknown',
        'bindingFingerprint': row['binding'], 'tool': row['tool']}


@contextmanager
def operation_scope(workspace, operation_id, details, book_fingerprint):
    """Call after approval/freshness checks. A used ID can never execute again."""
    tool = details.get('tool') or details.get('action')
    if operation_id is None or tool not in SUPPORTED:
        yield
        return
    _identity(operation_id)
    if _active.get() is not None:
        raise ValueError('Nested formal operation receipts are unsupported')
    path = _database(workspace)
    if not path.is_file():
        raise ValueError('Initialize the story project before recording formal operations')
    attempt_path = _pending_path(path)
    binding = binding_fingerprint(details, book_fingerprint)
    with _connect(attempt_path, create=True) as connection:
        connection.execute(_SCHEMA)
        connection.execute('BEGIN IMMEDIATE')
        old = connection.execute('SELECT binding FROM operation_receipts WHERE operation_id=?', (operation_id,)).fetchone()
        old = old or _read(path, operation_id)
        if old is not None:
            raise ValueError('Operation binding changed' if old['binding'] != binding else 'Operation already attempted; query its receipt, never replay')
        connection.execute('INSERT INTO operation_receipts VALUES (?,?,?,\'pending\',\'[]\',\'unknown\')',
            (operation_id, binding, tool))
    token = _active.set({'path': path, 'id': operation_id, 'tool': tool, 'binding': binding,
        'arguments': dict(details.get('arguments', {}))})
    outcome = 'failed'
    try:
        yield
        outcome = 'returned'
    finally:
        _active.reset(token)
        # A process kill skips this block: pending remains unknown, committed
        # remains committed. Never replace a prior commit with noncommit.
        with _connect(attempt_path) as connection:
            connection.execute("UPDATE operation_receipts SET state=CASE WHEN state='pending' THEN 'not_committed' ELSE state END, outcome=? WHERE operation_id=?",
                (outcome, operation_id))


def record_transaction(connection, workspace):
    """Called inside ProjectStore's write transaction immediately before commit."""
    current = _active.get()
    if current is None or not connection.total_changes:
        return
    if _database(workspace) != current['path']:
        raise ValueError('Formal operation attempted to write a different project')
    connection.execute(_SCHEMA)
    args, tool = current['arguments'], current['tool']
    if tool == 'studio_accept_proposal':
        row = connection.execute('SELECT id,status,category,fact_key FROM fact_proposals WHERE id=?',
            (args['proposal_id'],)).fetchone()
        if row is None:
            raise ValueError('Formal proposal effect is unavailable')
        effect = {'kind': 'proposal', 'proposalId': row['id'], 'status': row['status']}
        # Mirror _upsert_fact's exact state/ordering, not every historical idea
        # sharing the key: those records were not effects of this adoption.
        facts = connection.execute('''SELECT id,state FROM story_facts
            WHERE category=? AND fact_key=? AND state=?
            ORDER BY updated_at DESC,id DESC LIMIT 1''',
            (row['category'], row['fact_key'], args.get('state', 'canonical'))).fetchall()
        effect['facts'] = [{'factId': fact['id'], 'state': fact['state']} for fact in facts]
    elif tool == 'promoteFact':
        row = connection.execute('SELECT id,state FROM story_facts WHERE id=?', (args['factId'],)).fetchone()
        if row is None:
            raise ValueError('Formal fact effect is unavailable')
        effect = {'kind': 'fact', 'factId': row['id'], 'state': row['state']}
    else:
        target = args.get('slug', args.get('document'))
        if isinstance(target, str) and target.startswith('doc:'):
            target = int(target[4:])
        row = connection.execute('SELECT id,slug,active_version_id,state FROM documents WHERE ' +
            ('id=?' if type(target) is int else 'slug=?'), (target,)).fetchone()
        if row is None:
            raise ValueError('Formal document effect is unavailable')
        effect = {'kind': 'document', 'documentId': row['id'], 'slug': row['slug'],
            'versionId': row['active_version_id'], 'state': row['state']}
        if tool in {'studio_select_decision', 'studio_withdraw_decision',
                    'studio_knowledge_confirm', 'studio_knowledge_withdraw'}:
            version = connection.execute('SELECT content FROM document_versions WHERE id=?',
                (row['active_version_id'],)).fetchone()
            payload = json.loads(version['content'])
            effect['operation'] = payload['operation']
            if tool in {'studio_select_decision', 'studio_withdraw_decision'}:
                effect.update(semantic='creative_direction', domainStatus=payload['selection_status'])
                selection = payload['selection']
                effect['optionId'] = selection['option_id'] if selection else None
                effect['optionsVersionId'] = selection['options_version_id'] if selection else None
            else:
                effect.update(semantic='narrative_knowledge', domainStatus=payload['status'])
                confirmation = payload['confirmed']
                effect['proposalVersionId'] = confirmation['proposal_version_id'] if confirmation else None
        if tool == 'approveScene':
            scene = connection.execute('SELECT status FROM scene_cards WHERE document_id=?', (row['id'],)).fetchone()
            effect['sceneStatus'] = scene['status'] if scene else None
    row = connection.execute('SELECT state,effects FROM operation_receipts WHERE operation_id=?', (current['id'],)).fetchone()
    if row is None:
        connection.execute('INSERT INTO operation_receipts VALUES (?,?,?,\'committed\',?,\'unknown\')',
            (current['id'], current['binding'], tool, json.dumps([effect], ensure_ascii=False)))
        return
    # Supported formal tools each own one domain transaction. A later write
    # must not masquerade as another effect on the first tool's target.
    raise ValueError('Formal operation already committed; a second write transaction is not authorized by this receipt')
