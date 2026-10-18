"""Author-confirmed character and reader cognition in versioned story documents.

Confirmation records a perspective, including mistaken beliefs. It does not
establish the proposition as a world fact or parse narrative time from prose.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .project_store import ProjectStoreError, _json, _slug, _utc_now


KIND = 'narrative_knowledge'
SCHEMA = 'lg.narrative-knowledge.v1'


class Topic(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    audience: Literal['character', 'reader']
    holder_document_id: int | None = Field(gt=0)
    key: str = Field(min_length=1, max_length=128)
    from_position: int = Field(ge=-1000000000, le=1000000000)


class Source(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    document_id: int = Field(gt=0)
    version_id: int = Field(gt=0)


class Statement(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    content: str = Field(min_length=1, max_length=12000)
    stance: Literal['knows', 'believes', 'uncertain']
    until_position: int | None
    sources: list[Source] = Field(max_length=32)


class Proposal(Statement):
    rationale: str = Field(min_length=1, max_length=4000)


class Confirmed(Proposal):
    proposal_version_id: int = Field(gt=0)
    proposal_version_number: int = Field(gt=0)


class Payload(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    schema_name: Literal['lg.narrative-knowledge.v1'] = Field(alias='schema')
    operation: Literal['propose', 'confirm', 'withdraw']
    topic: Topic
    proposal_version_number: int = Field(gt=0)
    proposal: Proposal
    confirmed: Confirmed | None
    status: Literal['unconfirmed', 'confirmed', 'withdrawn']


def _payload(document) -> dict:
    try:
        value = Payload.model_validate(json.loads(document.content)).model_dump(by_alias=True)
        topic = value['topic']
        if not topic['key'].strip():
            raise ValueError()
        if ((topic['audience'] == 'reader') != (topic['holder_document_id'] is None)
                or (value['status'] == 'confirmed') != (value['confirmed'] is not None)):
            raise ValueError()
        for statement in (value['proposal'], value['confirmed']):
            if statement is not None:
                _statement_valid(topic, statement)
        return value
    except (ValidationError, ValueError, TypeError, KeyError):
        raise ProjectStoreError('Invalid saved narrative knowledge record.') from None


def _document(store, connection, reference):
    identity = store._resolve_document_id(connection, reference, kind=KIND)
    return store._get_document(connection, identity)


def _head(document, expected_version_id):
    if type(expected_version_id) is not int or expected_version_id != document.active_version_id:
        raise ProjectStoreError('Narrative knowledge changed; refresh before continuing.')


def _holder(store, connection, topic):
    if topic['audience'] == 'reader':
        if topic['holder_document_id'] is not None:
            raise ProjectStoreError('Reader cognition cannot name a character holder.')
        return
    identity = topic['holder_document_id']
    if type(identity) is not int or identity <= 0:
        raise ProjectStoreError('Character cognition requires a character document holder.')
    document = store._get_document(connection, store._resolve_document_id(connection, identity, kind='character'))
    if document.state in {'archived', 'rejected'}:
        raise ProjectStoreError('Character cognition holder is archived or rejected.')


def _statement_valid(topic, statement):
    if not statement['content'].strip() or not statement['rationale'].strip():
        raise ProjectStoreError('Narrative knowledge content and declared rationale cannot be blank.')
    until = statement['until_position']
    if until is not None and until <= topic['from_position']:
        raise ProjectStoreError('Narrative knowledge interval must end after its start.')
    sources = statement['sources']
    if len({(source['document_id'], source['version_id']) for source in sources}) != len(sources):
        raise ProjectStoreError('Declare each exact narrative knowledge source once.')


def _sources_status(store, connection, sources):
    details = []
    for source in sources:
        status = store._source_status(connection, source['document_id'], source['version_id'])
        details.append({**source, **status})
    states = {source['status'] for source in details}
    status = next((state for state in ('unavailable', 'stale', 'pending') if state in states),
                  'current' if details else 'unbound')
    return status, details


def _snapshot(store, connection, document):
    value = _payload(document)
    if (document.parent_id != value['topic']['holder_document_id']
            or document.sequence != value['topic']['from_position']):
        raise ProjectStoreError('Saved narrative knowledge topic projection is inconsistent.')
    proposal_version = store._resolve_version(connection, document.id, value['proposal_version_number'])
    source = _payload(proposal_version)
    if (source['operation'] != 'propose' or source['topic'] != value['topic']
            or source['proposal_version_number'] != proposal_version.version_number
            or source['proposal'] != value['proposal']):
        raise ProjectStoreError('Saved narrative knowledge proposal identity is inconsistent.')
    confirmed = value['confirmed']
    if confirmed is not None:
        confirmed_version = store._resolve_version(connection, document.id, confirmed['proposal_version_number'])
        original = _payload(confirmed_version)
        if (confirmed_version.id != confirmed['proposal_version_id'] or original['operation'] != 'propose'
                or original['topic'] != value['topic']
                or original['proposal'] != {key: confirmed[key] for key in value['proposal']}):
            raise ProjectStoreError('Saved narrative knowledge confirmation identity is inconsistent.')
    status, details = _sources_status(store, connection,
        value['confirmed']['sources'] if value['confirmed'] is not None else value['proposal']['sources'])
    proposal_status, proposal_details = _sources_status(store, connection, value['proposal']['sources'])
    return {'document_id': document.id, 'slug': document.slug, 'title': document.title,
        'version_id': document.active_version_id, 'version_number': document.active_version_number,
        'topic': value['topic'], 'status': value['status'],
        'proposal_version_id': proposal_version.id, 'proposal_version_number': proposal_version.version_number,
        'proposal': value['proposal'], 'confirmed': value['confirmed'],
        'source_status': status, 'source_details': details,
        'proposal_source_status': proposal_status, 'proposal_source_details': proposal_details}


def snapshot(store, reference):
    store._require_initialized()
    with store._connection() as connection:
        connection.execute('BEGIN')
        return _snapshot(store, connection, _document(store, connection, reference))


def snapshots(store, *, limit=100):
    store._require_initialized()
    if limit is not None and (type(limit) is not int or not 1 <= limit <= 1000):
        raise ProjectStoreError('Narrative knowledge limit must be between 1 and 1000 or None.')
    with store._connection() as connection:
        connection.execute('BEGIN')
        rows = connection.execute('SELECT id FROM documents WHERE kind=? ORDER BY id LIMIT ?',
            (KIND, -1 if limit is None else limit))
        return [_snapshot(store, connection, store._get_document(connection, row['id'])) for row in rows]


def _write(store, connection, document, payload, reason):
    if payload['operation'] == 'propose':
        payload['proposal_version_number'] = connection.execute(
            'SELECT COALESCE(MAX(version_number),0)+1 FROM document_versions WHERE document_id=?',
            (document.id,)).fetchone()[0]
    version_id = store._insert_version(connection, document_id=document.id, content=_json(payload),
        state='draft', reason=reason, metadata={'previous_version_id': document.active_version_id})
    store._activate_version(connection, document.id, version_id, state='draft')
    store._refresh_document_search(connection, document.id)
    return _snapshot(store, connection, store._get_document(connection, document.id))


def propose(store, *, slug, title, topic, statement, rationale, expected_version_id):
    store._require_initialized()
    if not isinstance(slug, str):
        raise ProjectStoreError('Narrative knowledge requires a document slug.')
    normalized_slug = _slug(slug)
    if not isinstance(title, str) or not title.strip():
        raise ProjectStoreError('Narrative knowledge title cannot be blank.')
    if type(expected_version_id) is not int or expected_version_id < 0:
        raise ProjectStoreError('An exact narrative knowledge head is required; use zero only for a new record.')
    try:
        topic = Topic.model_validate(topic).model_dump()
        statement = Statement.model_validate(statement).model_dump()
        proposal = Proposal.model_validate({**statement, 'rationale': rationale}).model_dump()
    except (ValidationError, ValueError, TypeError):
        raise ProjectStoreError('Invalid narrative knowledge topic, statement, sources or declared rationale.') from None
    topic['key'] = topic['key'].strip()
    if not topic['key']:
        raise ProjectStoreError('Narrative knowledge topic key cannot be blank.')
    _statement_valid(topic, proposal)
    with store._connection() as connection:
        connection.execute('BEGIN IMMEDIATE')
        _holder(store, connection, topic)
        for source in proposal['sources']:
            version = store._get_version(connection, source['version_id'])
            if version.document_id != source['document_id']:
                raise ProjectStoreError('Narrative knowledge source version belongs to a different document.')
        row = connection.execute('SELECT id FROM documents WHERE slug=?', (normalized_slug,)).fetchone()
        if row:
            document = _document(store, connection, row['id'])
            _head(document, expected_version_id)
            previous = _payload(document)
            if previous['topic'] != topic or document.title != title.strip():
                raise ProjectStoreError('Narrative knowledge topic and title cannot change; use the existing identity.')
        else:
            if expected_version_id != 0:
                raise ProjectStoreError('New narrative knowledge requires expected_version_id=0.')
            for existing in connection.execute('SELECT id FROM documents WHERE kind=?', (KIND,)):
                if _payload(store._get_document(connection, existing['id']))['topic'] == topic:
                    raise ProjectStoreError('This narrative knowledge topic already has a record; update its existing identity.')
            now = _utc_now()
            cursor = connection.execute('''INSERT INTO documents
                (kind,slug,title,state,parent_id,sequence,active_version_id,metadata_json,created_at,updated_at)
                VALUES (?,?,?,'draft',?,?,NULL,'{}',?,?)''',
                (KIND, normalized_slug, title.strip(), topic['holder_document_id'], topic['from_position'], now, now))
            document = store._get_document(connection, int(cursor.lastrowid))
            previous = {'confirmed': None, 'status': 'unconfirmed'}
        payload = {'schema': SCHEMA, 'operation': 'propose', 'topic': topic,
            'proposal': proposal, 'confirmed': previous['confirmed'], 'status': previous['status']}
        return _write(store, connection, document, payload, 'Saved provisional narrative cognition; confirmation retained')


def _overlap(store, connection, document, topic, proposed):
    start = topic['from_position']
    until = proposed['until_position']
    for row in connection.execute('SELECT id FROM documents WHERE kind=? AND id!=? AND parent_id IS ? ORDER BY id',
                                 (KIND, document.id, topic['holder_document_id'])):
        other_document = store._get_document(connection, row['id'])
        other = _payload(other_document)
        if (other_document.parent_id != other['topic']['holder_document_id']
                or other_document.sequence != other['topic']['from_position']):
            raise ProjectStoreError('Saved narrative knowledge topic projection is inconsistent.')
        if (other['confirmed'] is None or other['topic']['audience'] != topic['audience']
                or other['topic']['key'] != topic['key']):
            continue
        other_start = other['topic']['from_position']
        other_until = other['confirmed']['until_position']
        if (until is None or other_start < until) and (other_until is None or start < other_until):
            raise ProjectStoreError('Confirmed narrative knowledge intervals overlap; withdraw or revise the existing confirmation first.')


def _confirmation_preview(store, connection, reference, *, proposal_version_number, expected_version_id):
    document = _document(store, connection, reference)
    _head(document, expected_version_id)
    if type(proposal_version_number) is not int or proposal_version_number < 1:
        raise ProjectStoreError('An exact narrative knowledge proposal version number is required.')
    version = store._resolve_version(connection, document.id, proposal_version_number)
    value = _payload(version)
    current = _payload(document)
    if (value['operation'] != 'propose' or value['topic'] != current['topic']
            or value['proposal_version_number'] != version.version_number):
        raise ProjectStoreError('Confirm an actual proposal version of this exact narrative knowledge topic.')
    _holder(store, connection, current['topic'])
    status, _ = _sources_status(store, connection, value['proposal']['sources'])
    if status not in {'current', 'unbound'}:
        raise ProjectStoreError('Narrative knowledge source is not current accepted text; reconcile before confirming.')
    proposed = {'proposal_version_id': version.id, 'proposal_version_number': version.version_number,
                **value['proposal']}
    if current['confirmed'] == proposed:
        raise ProjectStoreError('This exact narrative knowledge proposal is already confirmed.')
    _overlap(store, connection, document, current['topic'], proposed)
    return {'action': 'confirm', 'knowledge': _snapshot(store, connection, document),
        'proposed_confirmation': proposed, 'proposal_version_id': version.id}


def confirmation_preview(store, reference, **arguments):
    store._require_initialized()
    with store._connection() as connection:
        connection.execute('BEGIN')
        return _confirmation_preview(store, connection, reference, **arguments)


def confirm(store, reference, **arguments):
    store._require_initialized()
    with store._connection() as connection:
        connection.execute('BEGIN IMMEDIATE')
        preview = _confirmation_preview(store, connection, reference, **arguments)
        document = _document(store, connection, reference)
        payload = _payload(document)
        payload.update(operation='confirm', confirmed=preview['proposed_confirmation'], status='confirmed')
        return _write(store, connection, document, payload, 'Author confirmed exact narrative cognition; no world fact promotion')


def _withdrawal_preview(store, connection, reference, *, expected_version_id):
    document = _document(store, connection, reference)
    _head(document, expected_version_id)
    if _payload(document)['confirmed'] is None:
        raise ProjectStoreError('There is no current narrative knowledge confirmation to withdraw.')
    return {'action': 'withdraw', 'knowledge': _snapshot(store, connection, document), 'proposed_confirmation': None}


def withdrawal_preview(store, reference, **arguments):
    store._require_initialized()
    with store._connection() as connection:
        connection.execute('BEGIN')
        return _withdrawal_preview(store, connection, reference, **arguments)


def withdraw(store, reference, **arguments):
    store._require_initialized()
    with store._connection() as connection:
        connection.execute('BEGIN IMMEDIATE')
        _withdrawal_preview(store, connection, reference, **arguments)
        document = _document(store, connection, reference)
        payload = _payload(document)
        payload.update(operation='withdraw', confirmed=None, status='withdrawn')
        return _write(store, connection, document, payload, 'Author withdrew current narrative cognition; no historical fallback')
