"""Versioned creative choices in the existing story database.

An active document version is the latest record, not an author selection. Only
the dedicated select/withdraw transitions change that selection.
"""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .project_store import ProjectStoreError, _json, _slug, _utc_now

KIND = 'creative_decision'
SCHEMA = 'lg.creative-decision.v1'


class Topic(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    scope: Literal['work', 'part', 'chapter']
    document_id: int | None = None
    key: str = Field(min_length=1)


class Option(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)


class Source(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    document_id: int = Field(gt=0)
    version_id: int = Field(gt=0)


def guard_kind(kind: str) -> None:
    from .managed_documents import guard_kind as guard
    guard(kind)


def _payload(document) -> dict:
    try:
        value = json.loads(document.content)
    except (ValueError, TypeError):
        raise ProjectStoreError('Invalid saved creative decision record.') from None
    if not isinstance(value, dict) or value.get('schema') != SCHEMA:
        raise ProjectStoreError('Invalid saved creative decision record.')
    return value


def _document(store, connection, reference):
    identity = store._resolve_document_id(connection, reference, kind=KIND)
    return store._get_document(connection, identity)


def _head(document, expected_version_id):
    if type(expected_version_id) is not int or expected_version_id != document.active_version_id:
        raise ProjectStoreError('Creative decision changed; refresh before continuing.')


def _sources_status(store, connection, sources, *, projected_versions=None):
    details = []
    for source in sources:
        row = connection.execute('''SELECT d.active_version_id,d.state,v.document_id AS owner,v.state AS version_state
            FROM documents d LEFT JOIN document_versions v ON v.id=? WHERE d.id=?''',
            (source['version_id'], source['document_id'])).fetchone()
        projection = (projected_versions or {}).get(source['document_id'])
        active = projection['version_id'] if projection else row['active_version_id'] if row else None
        state = projection['state'] if projection else row['state'] if row else None
        version_state = row['version_state'] if row else None
        if projection and projection['version_id'] == source['version_id']:
            version_state = projection['state']
        current = bool(row and row['owner'] == source['document_id']
            and active == source['version_id'] and state not in {'archived', 'rejected'}
            and version_state not in {'archived', 'rejected'})
        details.append({**source, 'status': 'current' if current else 'stale',
            'active_version_id': active, 'document_state': state, 'version_state': version_state})
    return ('stale' if any(item['status'] != 'current' for item in details)
        else 'current' if sources else 'unbound'), details


def _require_sources(store, connection, sources):
    status, _ = _sources_status(store, connection, sources)
    if status == 'stale':
        raise ProjectStoreError('Creative decision source changed; regenerate or reconcile before selecting.')


def _snapshot(store, connection, document):
    value = _payload(document)
    options_version = store._resolve_version(connection, document.id, value['options_version_number'])
    selection = value['selection']
    status, details = _sources_status(store, connection, selection['sources'] if selection else value['sources'])
    options_status, options_details = _sources_status(store, connection, value['sources'])
    return {'document_id': document.id, 'slug': document.slug, 'title': document.title,
        'version_id': document.active_version_id, 'version_number': document.active_version_number,
        'options_version_id': options_version.id, 'options_version_number': options_version.version_number,
        'topic': value['topic'], 'status': value['selection_status'], 'selection': selection,
        'source_status': status, 'source_details': details,
        'options_source_status': options_status, 'options_source_details': options_details,
        'options': value['options'], 'sources': value['sources'], 'request': value['request'],
        'request_sha256': value['request_sha256']}


def snapshot(store, reference):
    store._require_initialized()
    with store._connection() as connection:
        connection.execute('BEGIN')
        return _snapshot(store, connection, _document(store, connection, reference))


def snapshots(store, *, limit=100):
    store._require_initialized()
    if limit is not None and (type(limit) is not int or not 1 <= limit <= 1000):
        raise ProjectStoreError('Creative decision limit must be between 1 and 1000 or None.')
    with store._connection() as connection:
        connection.execute('BEGIN')
        rows = connection.execute('SELECT id FROM documents WHERE kind=? ORDER BY id LIMIT ?',
            (KIND, -1 if limit is None else limit)).fetchall()
        return [_snapshot(store, connection, store._get_document(connection, row['id'])) for row in rows]


def context_snapshot(store, *, limit=100):
    """Bounded current choices, with a count from the same read transaction."""
    store._require_initialized()
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ProjectStoreError('Creative decision context limit must be between 1 and 1000.')
    with store._connection() as connection:
        connection.execute('BEGIN')
        total = connection.execute('SELECT COUNT(*) FROM documents WHERE kind=?', (KIND,)).fetchone()[0]
        rows = connection.execute('SELECT id FROM documents WHERE kind=? ORDER BY id LIMIT ?', (KIND, limit)).fetchall()
        values = []
        for row in rows:
            snapshot = _snapshot(store, connection, store._get_document(connection, row['id']))
            value = {key: value for key, value in snapshot.items()
                if key not in {'options', 'sources', 'request', 'request_sha256',
                    'options_source_details', 'options_source_status'}}
            if value['selection'] is not None:
                value['selection'] = {key: item for key, item in value['selection'].items()
                    if key not in {'request', 'request_sha256'}}
            values.append(value)
        return {'decisions': values, 'total': total, 'limit': limit, 'truncated': total > len(values)}


def review_context(store, chapter, *, projected_versions=None):
    """Selected plans relevant to a chapter, independent of provisional option heads.

Candidate reviews describe the state after adoption. Projecting source validity
here keeps pre-adoption evidence equal to the published post-adoption snapshot.
This records review inputs, not what a model used when generating the candidate.
"""
    store._require_initialized()
    with store._connection() as connection:
        connection.execute('BEGIN')
        chapter_id = store._resolve_document_id(connection, chapter, kind='chapter')
        target = store._get_document(connection, chapter_id)
        projections = {} if projected_versions is None else projected_versions
        if not isinstance(projections, dict):
            raise ProjectStoreError('Invalid projected review versions.')
        for identity, projection in projections.items():
            if (type(identity) is not int or not isinstance(projection, dict)
                    or set(projection) != {'version_id', 'state'}
                    or type(projection['version_id']) is not int
                    or projection['state'] not in {'accepted', 'final'}):
                raise ProjectStoreError('Invalid projected review versions.')
            document = store._get_document(connection, identity)
            version = store._get_version(connection, projection['version_id'])
            if version.document_id != identity or not (identity == chapter_id or
                    document.kind == 'scene' and document.parent_id == chapter_id):
                raise ProjectStoreError('Projected review version is outside this chapter.')
        result = []
        rows = connection.execute('SELECT id FROM documents WHERE kind=? ORDER BY id', (KIND,))
        for row in rows:
            document = store._get_document(connection, row['id'])
            payload = _payload(document)
            selection = payload['selection']
            topic = payload['topic']
            if selection is None:
                continue
            if not (topic['scope'] == 'work' or
                    topic['scope'] == 'chapter' and topic['document_id'] == chapter_id or
                    topic['scope'] == 'part' and topic['document_id'] == target.parent_id):
                continue
            status, details = _sources_status(store, connection, selection['sources'], projected_versions=projections)
            result.append({'document_id': document.id, 'topic': topic,
                'selection': {key: value for key, value in selection.items() if key != 'request'},
                'source_status': status, 'source_details': details})
            if len(result) >= 1000:
                raise ProjectStoreError('Decision review context exceeds the limit; refusing a partial audit.')
        return result


def _normalized(store, connection, topic, options, sources, request):
    try:
        normalized_topic = Topic.model_validate(topic).model_dump()
        if not isinstance(options, list) or len(options) != 3 or not isinstance(sources, list):
            raise ValueError()
        normalized_options = [Option.model_validate(item).model_dump() for item in options]
        normalized_sources = [Source.model_validate(item).model_dump() for item in sources]
    except (ValidationError, ValueError, TypeError):
        raise ProjectStoreError('A creative decision requires an exact topic, three identified options and versioned sources.') from None
    normalized_topic['key'] = normalized_topic['key'].strip()
    if not normalized_topic['key'] or any(not item[field].strip() for item in normalized_options for field in ('title', 'content')):
        raise ProjectStoreError('Creative decision topics and actual option content cannot be empty.')
    if len({item['id'] for item in normalized_options}) != 3:
        raise ProjectStoreError('Creative option IDs must be unique within their version.')
    if len({item['content'].strip() for item in normalized_options}) != 3:
        raise ProjectStoreError('Three distinct creative options are required.')
    if len({item['document_id'] for item in normalized_sources}) != len(normalized_sources):
        raise ProjectStoreError('Declare each creative decision source document once.')
    if not isinstance(request, str) or (not normalized_sources and not request.strip()):
        raise ProjectStoreError('A decision without document sources requires saved request context.')
    scope = normalized_topic['scope']
    target = normalized_topic['document_id']
    if scope == 'work':
        if target is not None:
            raise ProjectStoreError('A work-level topic cannot target a chapter or part.')
    else:
        if target is None:
            raise ProjectStoreError('A scoped creative decision requires its target document.')
        store._resolve_document_id(connection, target, kind=scope)
        if target not in {item['document_id'] for item in normalized_sources}:
            raise ProjectStoreError('The scoped target must be included among the versioned sources.')
    for source in normalized_sources:
        document = store._get_document(connection, source['document_id'])
        if document.kind == KIND:
            raise ProjectStoreError('Creative decision dependencies require a separate dependency contract.')
    _require_sources(store, connection, normalized_sources)
    return normalized_topic, normalized_options, normalized_sources, request


def _write(store, connection, document, payload, reason):
    if payload['operation'] == 'options':
        # The write transaction reserves this number until _insert_version below.
        payload['options_version_number'] = connection.execute(
            'SELECT COALESCE(MAX(version_number),0)+1 FROM document_versions WHERE document_id=?',
            (document.id,)).fetchone()[0]
    version_id = store._insert_version(connection, document_id=document.id, content=_json(payload),
        state='draft', reason=reason, metadata={'previous_version_id': document.active_version_id})
    store._activate_version(connection, document.id, version_id, state='draft')
    store._refresh_document_search(connection, document.id)
    return _snapshot(store, connection, store._get_document(connection, document.id))


def save_options(store, *, slug, title, topic, options, sources, request, expected_version_id):
    store._require_initialized()
    normalized_slug = _slug(slug)
    if not isinstance(title, str) or not title.strip():
        raise ProjectStoreError('Creative decision title cannot be empty.')
    if type(expected_version_id) is not int or expected_version_id < 0:
        raise ProjectStoreError('An exact creative decision head is required; use zero only for a new decision.')
    with store._connection() as connection:
        connection.execute('BEGIN IMMEDIATE')
        topic, options, sources, request = _normalized(store, connection, topic, options, sources, request)
        row = connection.execute('SELECT id FROM documents WHERE slug=?', (normalized_slug,)).fetchone()
        if row:
            document = _document(store, connection, row['id'])
            _head(document, expected_version_id)
            previous = _payload(document)
            if previous['topic'] != topic:
                raise ProjectStoreError('A creative decision topic cannot change; use its existing identity.')
            if document.title != title.strip():
                raise ProjectStoreError('Regenerating options cannot rename the creative decision.')
        else:
            if expected_version_id != 0:
                raise ProjectStoreError('New creative decision requires expected_version_id=0.')
            for existing in connection.execute('SELECT id FROM documents WHERE kind=?', (KIND,)).fetchall():
                if _payload(store._get_document(connection, existing['id']))['topic'] == topic:
                    raise ProjectStoreError('This creative topic already has a decision; update its existing record.')
            now = _utc_now()
            cursor = connection.execute('''INSERT INTO documents
                (kind,slug,title,state,parent_id,sequence,active_version_id,metadata_json,created_at,updated_at)
                VALUES (?,?,?,'draft',NULL,NULL,NULL,'{}',?,?)''', (KIND, normalized_slug, title.strip(), now, now))
            document = store._get_document(connection, int(cursor.lastrowid))
            previous = {'selection': None, 'selection_status': 'unselected'}
        payload = {'schema': SCHEMA, 'operation': 'options', 'topic': topic, 'options': options,
            'sources': sources, 'request': request, 'request_sha256': hashlib.sha256(request.encode()).hexdigest(),
            'selection': previous['selection'], 'selection_status': previous['selection_status']}
        return _write(store, connection, document, payload, 'Saved provisional creative options; choice retained')


def _selection_preview(store, connection, reference, *, options_version_number, option_id, expected_version_id):
    document = _document(store, connection, reference)
    _head(document, expected_version_id)
    if type(options_version_number) is not int or options_version_number < 1:
        raise ProjectStoreError('An exact options version number is required.')
    version = store._resolve_version(connection, document.id, options_version_number)
    value = _payload(version)
    current = _payload(document)
    if value['operation'] != 'options' or value['topic'] != current['topic']:
        raise ProjectStoreError('Select an actual options version of this exact decision topic.')
    option = next((item for item in value['options'] if item['id'] == option_id), None)
    if option is None:
        raise ProjectStoreError('Creative option does not belong to the selected options version.')
    _require_sources(store, connection, value['sources'])
    proposed = {'options_version_id': version.id, 'options_version_number': version.version_number,
        'option_id': option['id'], 'title': option['title'], 'content': option['content'],
        'sources': value['sources'], 'request': value['request'], 'request_sha256': value['request_sha256']}
    if current['selection'] == proposed:
        raise ProjectStoreError('This exact creative option is already selected.')
    return {'action': 'select', 'decision': _snapshot(store, connection, document),
        'proposed_selection': proposed, 'options_version_id': version.id}


def selection_preview(store, reference, **arguments):
    store._require_initialized()
    with store._connection() as connection:
        connection.execute('BEGIN')
        return _selection_preview(store, connection, reference, **arguments)


def select_option(store, reference, **arguments):
    store._require_initialized()
    with store._connection() as connection:
        connection.execute('BEGIN IMMEDIATE')
        preview = _selection_preview(store, connection, reference, **arguments)
        document = _document(store, connection, reference)
        payload = _payload(document)
        payload.update(operation='select', selection=preview['proposed_selection'], selection_status='selected')
        return _write(store, connection, document, payload, 'Author selected exact creative option')


def _withdrawal_preview(store, connection, reference, *, expected_version_id):
    document = _document(store, connection, reference)
    _head(document, expected_version_id)
    if _payload(document)['selection'] is None:
        raise ProjectStoreError('There is no current creative selection to withdraw.')
    return {'action': 'withdraw', 'decision': _snapshot(store, connection, document), 'proposed_selection': None}


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
        payload.update(operation='withdraw', selection=None, selection_status='withdrawn')
        return _write(store, connection, document, payload, 'Author withdrew current creative choice')
