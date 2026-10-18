"""Project registered knowledge on one explicit axis; this is not Runtime isolation."""
from __future__ import annotations

from .generation_sources import canonical_sha
from .project_store import ProjectStoreError


MAX_SCAN = 5000


def project(store, *, audience, position, holder_document=None, limit=100):
    from .narrative_knowledge import KIND, _snapshot

    if not isinstance(audience, str) or audience not in {'character', 'reader'}:
        raise ProjectStoreError('Knowledge view audience must be character or reader.')
    if type(position) is not int or not -10**9 <= position <= 10**9:
        raise ProjectStoreError('Knowledge view requires an explicit integer position.')
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ProjectStoreError('Knowledge view limit must be between 1 and 1000.')
    if audience == 'reader' and holder_document is not None:
        raise ProjectStoreError('Reader knowledge cannot use a character holder.')
    if audience == 'character' and (holder_document is None or type(holder_document) is bool):
        raise ProjectStoreError('Character knowledge requires an exact character document.')
    store._require_initialized()
    with store._connection() as connection:
        connection.execute('BEGIN')
        holder_id = None
        if audience == 'character':
            holder_id = store._resolve_document_id(connection, holder_document, kind='character')
            holder = store._get_document(connection, holder_id)
            if holder.state in {'archived', 'rejected'}:
                raise ProjectStoreError('Knowledge holder is no longer available.')
        result = {
            'schema': 'lg.narrative-view.v1',
            'view': {'audience': audience, 'axis': 'story' if audience == 'character' else 'reading',
                'position': position, 'holder_document_id': holder_id, 'scope': 'registered_records_only',
                'registered_filter_applied': True, 'model_input_isolation': False},
            'statements': [], 'withheld': [],
            'counts': {'considered': 0, 'visible': 0, 'withheld': 0, 'unconfirmed': 0,
                       'outside_interval': 0, 'invalid': 0},
            'truncated': False, 'coverage': 'unknown',
            'unknown_reasons': [
                'Only explicitly confirmed registered statements are projected; legacy/global data are not migrated.',
                'Positions are author-defined coordinates, not parsed times or chapter numbers.',
                'Runtime history, tools, instructions and other inputs are not isolated by this projection.',
                'Source versions are declared evidence, not proof that a proposition is objectively true.',
            ],
        }
        # Filter holder and interval start before budgets. Other characters and
        # future assertions cannot evict this observer's applicable statements.
        where = "kind=? AND parent_id IS ? AND sequence<=? AND state NOT IN ('archived','rejected')"
        parameters = (KIND, holder_id, position)
        total = connection.execute('SELECT COUNT(*) FROM documents WHERE ' + where, parameters).fetchone()[0]
        rows = connection.execute('SELECT id FROM documents WHERE ' + where +
            ' ORDER BY sequence DESC,id LIMIT ?', (*parameters, MAX_SCAN))
        for row in rows:
            result['counts']['considered'] += 1
            try:
                document = store._get_document(connection, row['id'])
                snapshot = _snapshot(store, connection, document)
                topic = snapshot['topic']
                if (topic['audience'] != audience or topic['holder_document_id'] != holder_id
                        or topic['from_position'] != document.sequence or document.parent_id != holder_id):
                    raise ValueError()
                confirmed = snapshot['confirmed']
                if confirmed is None:
                    result['counts']['unconfirmed'] += 1
                    continue
                until = confirmed['until_position']
                if until is not None and position >= until:
                    result['counts']['outside_interval'] += 1
                    continue
                source = snapshot['source_status']
                if source not in {'current', 'unbound'}:
                    result['counts']['withheld'] += 1
                    if len(result['withheld']) < limit:
                        result['withheld'].append({'record_id': document.id, 'source_status': source,
                            'reason': 'confirmed_record_retained_but_declared_source_needs_review'})
                    continue
                result['counts']['visible'] += 1
                if len(result['statements']) < limit:
                    result['statements'].append({'record_id': document.id, 'slug': document.slug,
                        'title': document.title, 'topic': topic,
                        **{key: confirmed[key] for key in ('proposal_version_id', 'proposal_version_number',
                            'content', 'stance', 'until_position', 'sources')}, 'source_status': source})
            except (ValueError, TypeError, KeyError):
                result['counts']['invalid'] += 1
                result['unknown_reasons'].append('An invalid registered record was excluded without exposing its contents.')
        if total > result['counts']['considered']:
            result['truncated'] = True
            result['unknown_reasons'].append('The registered-record scan budget was exhausted; unchecked records remain unknown.')
        if (result['counts']['visible'] > len(result['statements'])
                or result['counts']['withheld'] > len(result['withheld'])):
            result['truncated'] = True
            result['unknown_reasons'].append('Some applicable record details were omitted by the output budget.')
        result['coverage'] = 'partial' if result['statements'] else 'unknown'
        result['unknown_reasons'] = list(dict.fromkeys(result['unknown_reasons']))
        result['fingerprint'] = canonical_sha(result)
        return result
