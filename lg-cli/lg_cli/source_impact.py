"""Bounded reverse lookup of known source associations in the story database.

This is a diagnostic projection, not a dependency authority or a literary verdict.
"""
from __future__ import annotations

import json

from .generation_sources import SCHEMA as RECEIPT_SCHEMA, canonical_sha, text_sha
from .project_store import ProjectStoreError


SCHEMA = 'lg.source-impact.v1'
MAX_SCAN = 5000
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_REFS_PER_ITEM = 100


def _identity(value):
    return type(value) is int and value > 0


def _digest(value):
    return (isinstance(value, str) and len(value) == 64
            and all(char in '0123456789abcdef' for char in value))


def _metadata(row):
    if row['metadata_json'] is None:
        return None
    try:
        value = json.loads(row['metadata_json'])
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _receipt(connection, row, metadata):
    """Validate the saved output before using its captured input associations."""
    value = metadata.get('generation_sources')
    if (not isinstance(value, dict) or value.get('schema') != RECEIPT_SCHEMA
            or value.get('coverage') not in {'partial', 'unknown'}
            or not isinstance(value.get('observations'), list)
            or not isinstance(value.get('run_id'), str)
            or not isinstance(value.get('mode'), str)
            or not _digest(metadata.get('generation_output_sha256'))):
        return None
    for observation in value['observations']:
        if (not isinstance(observation, dict) or not isinstance(observation.get('tool'), str)
                or not _digest(observation.get('payload_sha256'))
                or observation.get('coverage') not in {'partial', 'unknown'}
                or not isinstance(observation.get('refs'), list)
                or any(not isinstance(ref, dict) for ref in observation['refs'])):
            return None
    try:
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError):
        return None
    # Load one body at a time, only for rows with a structurally usable receipt.
    content = connection.execute('SELECT content FROM document_versions WHERE id=?', (row['id'],)).fetchone()
    if content is None or text_sha(content['content']) != metadata['generation_output_sha256']:
        return None
    return value


def analyze(store, reference, *, version_number=None, limit=100):
    """Find direct declared/captured/edit associations; never infer a transitive edge."""
    from .creative_decisions import _snapshot as decision_snapshot
    from .narrative_knowledge import _snapshot as knowledge_snapshot
    from .source_review import _check_reference, _review_version

    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ProjectStoreError('Source impact limit must be between 1 and 1000.')
    if version_number is not None and not _identity(version_number):
        raise ProjectStoreError('Expected a positive source version number.')
    store._require_initialized()
    with store._connection() as connection:
        connection.execute('BEGIN')
        document_id = store._resolve_document_id(connection, reference)
        document = store._get_document(connection, document_id)
        selected = store._resolve_version(connection, document_id, version_number) if version_number is not None else None
        reviewed = selected if selected is not None else store._get_version(connection, document.active_version_id)
        result = {
            'schema': SCHEMA, 'purpose': 'author_global', 'semantic_impact_proven': False,
            'subject': {'document_id': document.id, 'slug': document.slug, 'title': document.title,
                'kind': document.kind, 'active_version_id': document.active_version_id,
                'version_id': selected.id if selected else None,
                'version_number': selected.version_number if selected else None},
            'generationSourceReview': _review_version(store, connection, document, reviewed, limit=limit),
            'direct_dependents': {'items': [], 'discovered': 0, 'limit': limit, 'truncated': False,
                'scan': {'tables': {}, 'unknown_versions': 0, 'index_only_refs': 0,
                    'invalid_receipts': 0, 'unresolved_version_refs': 0}},
            'coverage': 'partial',
            'unknown_reasons': [
                'Known associations do not prove semantic influence or literary inconsistency.',
                'Captured inputs are partial; absent references do not prove absence of impact.',
                'Fact inputs are not propagated to document sources; scene-card inputs do not bind prose versions.',
                'Discovered counts relation items; one version may have multiple relation types.',
            ],
        }
        dependents = result['direct_dependents']
        scan = dependents['scan']

        def reason(text):
            if text not in result['unknown_reasons']:
                result['unknown_reasons'].append(text)

        def add(item):
            dependents['discovered'] += 1
            if len(dependents['items']) < limit:
                if len(item['refs']) > MAX_REFS_PER_ITEM:
                    item = {**item, 'refs': item['refs'][:MAX_REFS_PER_ITEM], 'refs_truncated': True}
                    reason('Some dependent reference details exceeded the output budget.')
                dependents['items'].append(item)

        def rows(name, query, parameters=(), *, where=''):
            total = connection.execute(f'SELECT COUNT(*) FROM {name}' + where).fetchone()[0]
            state = {'total': total, 'scanned': 0, 'truncated': total > MAX_SCAN}
            if where:
                state['scope'] = {'kinds': ['creative_decision', 'narrative_knowledge']}
            scan['tables'][name] = state
            for row in connection.execute(query + ' LIMIT ?', (*parameters, MAX_SCAN)):
                state['scanned'] += 1
                yield row
            if state['truncated']:
                reason('Scan budget omitted stored records; discovered associations are a lower bound.')

        def matches(owner, identity):
            return (_identity(owner) and owner == document.id and _identity(identity)
                    and (selected is None or identity == selected.id))

        for table, kind in (('story_facts', 'fact'), ('fact_proposals', 'proposal')):
            query = f'SELECT id,source_document_id,source_version_id,' + ('state' if kind == 'fact' else 'status')
            query += f' AS state,category,fact_key FROM {table} ORDER BY id'
            for row in rows(table, query):
                if not matches(row['source_document_id'], row['source_version_id']):
                    continue
                status = store._source_status(connection, row['source_document_id'], row['source_version_id'])
                add({'kind': kind, 'id': row['id'], 'state': row['state'], 'category': row['category'],
                    'key': row['fact_key'], 'relation': 'declared_source', 'source_status': status['status'],
                    'refs': [{'kind': 'document', 'document_id': row['source_document_id'],
                              'version_id': row['source_version_id']}]})

        query = '''SELECT id,kind FROM documents
            WHERE kind IN ('creative_decision','narrative_knowledge') ORDER BY id'''
        for row in rows('documents', query, where=" WHERE kind IN ('creative_decision','narrative_knowledge')"):
            try:
                managed = store._get_document(connection, row['id'])
                snapshot = (decision_snapshot if row['kind'] == 'creative_decision' else knowledge_snapshot)(
                    store, connection, managed)
                selection = snapshot['selection'] if row['kind'] == 'creative_decision' else snapshot['confirmed']
                if selection is None:
                    continue
                sources = selection['sources']
                if not isinstance(sources, list) or any(not isinstance(source, dict) for source in sources):
                    raise ValueError()
                matched = [{'kind': 'document', 'document_id': source['document_id'], 'version_id': source['version_id']}
                    for source in sources if matches(source.get('document_id'), source.get('version_id'))]
                if not matched:
                    continue
                identity = ({'confirmed_proposal_version_id': selection['proposal_version_id']}
                    if row['kind'] == 'narrative_knowledge' else {})
                add({'kind': row['kind'], 'id': row['id'], 'document_id': row['id'],
                    'slug': managed.slug, 'title': managed.title, 'document_kind': row['kind'],
                    'version_id': snapshot['version_id'], 'version_number': snapshot['version_number'],
                    'state': managed.state, 'is_active': True, 'relation': 'declared_source',
                    'source_status': snapshot['source_status'], 'refs': matched, **identity})
            except (ValueError, TypeError, KeyError):
                reason('An invalid managed record prevented complete declared-source inspection.')

        query = '''SELECT v.id,v.document_id,v.version_number,v.state,
            CASE WHEN length(CAST(v.metadata_json AS BLOB))<=? THEN v.metadata_json ELSE NULL END AS metadata_json,
            d.slug,d.title,d.kind,d.active_version_id
            FROM document_versions v JOIN documents d ON d.id=v.document_id ORDER BY v.id'''
        for row in rows('document_versions', query, (MAX_METADATA_BYTES,)):
            metadata = _metadata(row)
            if metadata is None:
                scan['unknown_versions'] += 1
                scan['invalid_receipts'] += 1
                reason('Invalid or oversized version metadata was not interpreted.')
                continue
            item = {'kind': 'document_version', 'id': row['id'], 'document_id': row['document_id'],
                'slug': row['slug'], 'title': row['title'], 'document_kind': row['kind'],
                'version_id': row['id'], 'version_number': row['version_number'], 'state': row['state'],
                'is_active': row['active_version_id'] == row['id']}
            bases = []
            for field in ('base_version_id', 'previous_version_id'):
                identity = metadata.get(field)
                if row['document_id'] == document.id and matches(row['document_id'], identity):
                    source = connection.execute('SELECT document_id FROM document_versions WHERE id=?', (identity,)).fetchone()
                    if source and source['document_id'] == document.id:
                        bases.append({'kind': 'edit_basis', 'document_id': document.id,
                                      'version_id': identity, 'field': field})
            if bases:
                add({**item, 'relation': 'edit_basis', 'refs': bases})
            if metadata.get('generation_sources') is None:
                scan['unknown_versions'] += 1
                continue
            receipt = _receipt(connection, row, metadata)
            if receipt is None:
                scan['unknown_versions'] += 1
                scan['invalid_receipts'] += 1
                reason('Invalid receipts or changed outputs were excluded from captured-input associations.')
                continue
            matched = {}
            for observation in receipt['observations']:
                for ref in observation['refs']:
                    if ref.get('document_id') != document.id or type(ref.get('document_id')) is not int:
                        continue
                    if ref.get('kind') == 'document':
                        identity = ref.get('version_id')
                        if not matches(ref.get('document_id'), identity):
                            continue
                        source = connection.execute('SELECT document_id FROM document_versions WHERE id=?', (identity,)).fetchone()
                        if not source or source['document_id'] != document.id:
                            reason('A captured reference has a missing or wrong-owner source version.')
                            continue
                        if ref.get('exposure') == 'index':
                            scan['index_only_refs'] += 1
                            continue
                        if ref.get('exposure') not in {'content', 'excerpt'}:
                            reason('An unrecognized document exposure was not interpreted as content.')
                            continue
                    elif ref.get('kind') == 'creative_decision' and document.kind == 'creative_decision':
                        if ref.get('exposure') not in {'selected', 'unselected'}:
                            reason('An unrecognized decision exposure was not interpreted.')
                            continue
                        identity = ref.get('options_version_id')
                        if selected is not None and not _identity(identity):
                            scan['unresolved_version_refs'] += 1
                            reason('Some decision observations identify no exact options version; exact-version associations are unknown.')
                            continue
                        if selected is not None and identity != selected.id:
                            continue
                        if identity is not None:
                            source = connection.execute('SELECT document_id FROM document_versions WHERE id=?', (identity,)).fetchone()
                            if not source or source['document_id'] != document.id:
                                reason('A decision observation has a missing or wrong-owner options version.')
                                continue
                    elif ref.get('kind') == 'scene_card' and document.kind == 'scene':
                        if selected is not None:
                            scan['unresolved_version_refs'] += 1
                            reason('Scene-card observations identify no prose version; exact-version associations are unknown.')
                            continue
                    else:
                        continue
                    # Preserve which producer supplied the reference; its hashes have producer-specific projections.
                    try:
                        check = _check_reference(store, connection, observation, ref)
                    except (ProjectStoreError, KeyError, TypeError, ValueError):
                        check = {'status': 'unknown', 'reason': 'source_projection_could_not_be_reconstructed'}
                    if check['status'] == 'unknown':
                        reason('A matching captured projection could not be reconstructed; no input association was inferred.')
                        continue
                    copied = {key: ref[key] for key in ('kind', 'document_id', 'version_id', 'exposure',
                        'excerpt_sha256', 'selection_sha256', 'options_version_id', 'option_id') if key in ref}
                    copied.update(observation_tool=observation['tool'], source_status=check['status'],
                                  source_reason=check['reason'])
                    if ref.get('kind') == 'scene_card':
                        copied['observation_payload_sha256'] = observation['payload_sha256']
                    matched[canonical_sha(copied)] = copied
            if matched:
                add({**item, 'relation': 'captured_input', 'refs': list(matched.values())})
        if scan['unknown_versions']:
            reason('Some scanned versions have no verifiable captured generation sources; their impact remains unknown.')
        dependents['truncated'] = (dependents['discovered'] > limit
            or any(state['truncated'] for state in scan['tables'].values())
            or any(item.get('refs_truncated') for item in dependents['items']))
        return result
