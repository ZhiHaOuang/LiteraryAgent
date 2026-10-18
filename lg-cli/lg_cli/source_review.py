"""Read-only checks of identified captured inputs, never inferred literary causation."""
from __future__ import annotations

from dataclasses import asdict
import json

from .generation_sources import SCHEMA, canonical_sha, inherited_receipt, text_sha
from .project_store import ProjectStoreError


STATUSES = ('current', 'changed', 'unavailable', 'unknown', 'index_only', 'historical')
MAX_REFERENCES = 10000


def _limit(value):
    if type(value) is not int or not 1 <= value <= 1000:
        raise ProjectStoreError('Source review limit must be between 1 and 1000.')
    return value


def _identity(value):
    return type(value) is int and value > 0


def _digest(value):
    return isinstance(value, str) and len(value) == 64 and all(char in '0123456789abcdef' for char in value)


def _result(status, reason, **current):
    return {'status': status, 'reason': reason, 'current': current}


def _check_reference(store, connection, observation, ref):
    """Compare only the producer's identified projection in one caller-owned snapshot."""
    tool = observation.get('tool')
    kind = ref.get('kind')
    if kind == 'document':
        if not _identity(ref.get('document_id')) or not _identity(ref.get('version_id')):
            return _result('unknown', 'invalid_document_reference')
        if ref.get('exposure') == 'index':
            return _result('index_only', 'index_exposure_does_not_establish_content_input')
        if tool not in {'read_document', 'render_story_context', 'story_source_document'}:
            return _result('unknown', 'unrecognized_document_projection')
        if ref.get('exposure') not in {'content', 'excerpt'}:
            return _result('unknown', 'unrecognized_document_exposure')
        document = connection.execute('SELECT id,active_version_id,state FROM documents WHERE id=?',
                                      (ref['document_id'],)).fetchone()
        version = connection.execute('SELECT id,document_id,state FROM document_versions WHERE id=?',
                                     (ref['version_id'],)).fetchone()
        if document is None or version is None or version['document_id'] != ref['document_id']:
            return _result('unavailable', 'document_or_owned_version_unavailable')
        current = {'active_version_id': document['active_version_id'], 'version_id': version['id'],
            'document_state': document['state'], 'version_state': version['state']}
        if document['state'] in {'archived', 'rejected'} or version['state'] in {'archived', 'rejected'}:
            return _result('unavailable', 'document_or_version_retired', **current)
        if tool == 'story_source_document':
            # This producer can intentionally deliver an older selected candidate.
            # Its receipt did not record the active pointer at capture time.
            status = 'current' if document['active_version_id'] == version['id'] else 'historical'
            return _result(status, 'explicit_version_available_active_baseline_not_captured', **current)
        if document['active_version_id'] != version['id']:
            return _result('changed', 'active_source_version_replaced', **current)
        return _result('current', 'same_active_source_version', **current)

    if kind == 'creative_decision':
        if not _identity(ref.get('document_id')) or not _digest(ref.get('selection_sha256')):
            return _result('unknown', 'invalid_decision_reference')
        if tool not in {'project_memory', 'studio_decision', 'render_story_context'}:
            return _result('unknown', 'unrecognized_decision_projection')
        from .creative_decisions import _document, _snapshot
        try:
            document = _document(store, connection, ref['document_id'])
            value = _snapshot(store, connection, document)
        except ProjectStoreError:
            return _result('unavailable', 'decision_unavailable')
        if document.state in {'archived', 'rejected'}:
            return _result('unavailable', 'decision_retired')
        selection = value['selection']
        if tool != 'studio_decision' and selection is not None:
            selection = {key: item for key, item in selection.items() if key not in {'request', 'request_sha256'}}
        semantic = {key: value.get(key) for key in ('topic', 'status', 'source_status')}
        semantic['selection'] = selection
        if tool != 'render_story_context':
            semantic['source_details'] = value['source_details']
        digest = canonical_sha(semantic)
        current = {'selection_status': value['status'], 'source_status': value['source_status'],
            'selection_sha256': digest, 'options_version_id': selection['options_version_id'] if selection else None,
            'option_id': selection['option_id'] if selection else None}
        return _result('current' if digest == ref['selection_sha256'] else 'changed',
            'same_identified_choice_projection' if digest == ref['selection_sha256'] else 'choice_or_declared_basis_changed',
            **current)

    if kind == 'fact':
        if not _identity(ref.get('fact_id')):
            return _result('unknown', 'invalid_fact_reference')
        try:
            fact = store._get_fact(connection, ref['fact_id'])
        except ProjectStoreError:
            return _result('unavailable', 'fact_unavailable')
        if fact.state in {'archived', 'rejected'}:
            return _result('unavailable', 'fact_retired', state=fact.state)
        source = store._fact_source_status(connection, fact)
        if tool == 'project_memory' and _digest(ref.get('payload_sha256')):
            previous = ref['payload_sha256']
            digest = canonical_sha({**asdict(fact), 'source': source})
        elif tool == 'render_story_context' and _digest(ref.get('excerpt_sha256')):
            from .story_context import _facts_section
            previous = ref['excerpt_sha256']
            digest = text_sha(_facts_section('', [fact], {fact.id: source}).partition('\n')[2])
        else:
            return _result('unknown', 'unrecognized_fact_projection')
        return _result('current' if digest == previous else 'changed',
            'same_identified_fact_projection' if digest == previous else 'fact_or_declared_basis_changed',
            state=fact.state, source_status=source['status'], projection_sha256=digest)

    if kind == 'scene_card':
        if tool != 'render_scene_card' or not _identity(ref.get('document_id')) or not _digest(observation.get('payload_sha256')):
            return _result('unknown', 'unrecognized_scene_card_projection')
        from .story_context import render_scene_card
        try:
            scene = store._get_scene(connection, ref['document_id'])
        except ProjectStoreError:
            return _result('unavailable', 'scene_card_unavailable')
        if scene.document.state in {'archived', 'rejected'}:
            return _result('unavailable', 'scene_document_retired')
        digest = text_sha(render_scene_card(scene))
        return _result('current' if digest == observation['payload_sha256'] else 'changed',
            'same_scene_card_projection' if digest == observation['payload_sha256'] else 'scene_card_changed',
            projection_sha256=digest)
    return _result('unknown', 'unrecognized_reference_kind')


def _review_version(store, connection, document, version, *, limit=100):
    _limit(limit)
    result = {'schema': 'lg.source-review.v1', 'document_id': document.id, 'version_id': version.id,
        'version_number': version.version_number, 'receipt_status': 'unrecorded', 'coverage': 'unknown',
        'status': 'unknown', 'items': [], 'summary': dict.fromkeys(STATUSES, 0),
        'total_refs': 0, 'truncated': False, 'unknown_reasons': []}
    receipt = version.metadata.get('generation_sources')
    if receipt is None:
        result['unknown_reasons'] = ['generation_inputs_not_recorded']
        return result
    try:
        inherited_receipt(version.metadata, version.content)
        if (not isinstance(receipt, dict) or receipt.get('schema') != SCHEMA
                or receipt.get('coverage') not in {'partial', 'unknown'}
                or not isinstance(receipt.get('run_id'), str) or not isinstance(receipt.get('mode'), str)
                or not isinstance(receipt.get('observations'), list)
                or not isinstance(receipt.get('unknown_reasons', []), list)
                or not all(isinstance(reason, str) for reason in receipt.get('unknown_reasons', []))):
            raise ValueError()
        for observation in receipt['observations']:
            if (not isinstance(observation, dict) or not isinstance(observation.get('refs'), list)
                    or not isinstance(observation.get('tool'), str)
                    or not _digest(observation.get('payload_sha256'))
                    or observation.get('coverage') not in {'partial', 'unknown'}):
                raise ValueError()
        # Validate JSON data without retaining a mutable link into stored metadata.
        json.dumps(receipt, allow_nan=False)
    except (ValueError, TypeError):
        result['receipt_status'] = 'invalid'
        result['unknown_reasons'] = ['generation_receipt_or_output_binding_invalid']
        return result
    result.update(receipt_status='recorded', coverage=receipt['coverage'],
                  unknown_reasons=list(receipt.get('unknown_reasons', [])))
    seen = set()
    checked = 0
    items = []
    for observation in receipt['observations']:
        if not observation['refs']:
            result['unknown_reasons'].append('observation_has_no_identified_sources')
        result['total_refs'] += len(observation['refs'])
        for ref in observation['refs']:
            if checked >= MAX_REFERENCES:
                result['truncated'] = True
                continue
            checked += 1
            if not isinstance(ref, dict):
                result['summary']['unknown'] += 1
                continue
            key = canonical_sha([observation.get('tool'), ref, observation.get('payload_sha256')
                                 if ref.get('kind') == 'scene_card' else None])
            if key in seen:
                continue
            seen.add(key)
            try:
                check = _check_reference(store, connection, observation, ref)
            except (KeyError, TypeError, ValueError):
                check = _result('unknown', 'source_projection_could_not_be_reconstructed')
            result['summary'][check['status']] += 1
            items.append({'tool': observation.get('tool'), 'ref': ref, **check})
    if result['truncated']:
        result['unknown_reasons'].append('source_check_budget_exhausted')
    result['items'] = items[:limit]
    result['truncated'] = result['truncated'] or len(items) > limit
    counts = result['summary']
    if counts['changed'] or counts['unavailable']:
        result['status'] = 'needs_review'
    elif counts['unknown'] or checked >= MAX_REFERENCES and result['total_refs'] > checked:
        result['status'] = 'unknown'
    elif counts['current'] or counts['historical']:
        result['status'] = 'no_detected_change'
    result['unknown_reasons'] = list(dict.fromkeys([*result['unknown_reasons'],
        'identified_inputs_only_not_complete_dependencies_or_model_influence']))
    return result


def review(store, reference, version_number, *, limit=100):
    _limit(limit)
    if type(version_number) is not int or version_number < 1:
        raise ProjectStoreError('Source review requires an exact positive version number.')
    store._require_initialized()
    with store._connection() as connection:
        connection.execute('BEGIN')
        identity = store._resolve_document_id(connection, reference)
        document = store._get_document(connection, identity)
        version = store._resolve_version(connection, identity, version_number)
        return _review_version(store, connection, document, version, limit=limit)
