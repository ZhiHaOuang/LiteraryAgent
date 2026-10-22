"""Author-invoked bounded tasks; shared presentation, approval and cancellation."""
from __future__ import annotations

import asyncio
import json
import threading


async def settled_worker(function, *args, **kwargs):
    """Keep ownership until a cancelled blocking stage has actually settled."""
    stop = threading.Event()
    worker = asyncio.create_task(asyncio.to_thread(function, *args, cancelled=stop.is_set, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        stop.set()
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                stop.set()
            except Exception:
                break
        # Retrieve any worker exception, without publishing cancelled output.
        if worker.done() and not worker.cancelled():
            worker.exception()
        raise


def publish(backend, key, value, *, title, field):
    state = backend.session(key)
    run = state['run_id']
    text = json.dumps(value, ensure_ascii=False)
    item_id = 'bounded-task:' + run
    backend.store.append(key, 'assistant', text, run_id=run, item_id=item_id)
    item = {'id': item_id, 'runId': run, 'kind': 'notice', 'title': title,
        'text': value.get('text') or value.get('message') or value.get('status', 'completed'),
        'state': 'completed', 'structured': {field: value}}
    backend.emit(key, 'client/event', {'type': 'item.updated', 'sessionId': key, 'item': item})
    return value


async def preview(backend, method, params):
    if method == 'task/preview':
        from .literary_tasks import prepare
        return await asyncio.to_thread(prepare, backend.config, params['options'])
    if method == 'task/read':
        from .literary_tasks import read
        return await asyncio.to_thread(read, backend.config, params['taskId'])
    if method == 'narrative/preview':
        from .narrative_execution import prepare
        return await asyncio.to_thread(prepare, backend.config, params['options'])
    raise ValueError('Unsupported bounded task preview.')


async def _approval(backend, key, details):
    state = backend.session(key)
    response = await state['control'].approval_handler(details,
        run_id=state['run_id'], author_action=True)
    if response.get('decision') != 'accept':
        raise asyncio.CancelledError()


def start(backend, key, method, params):
    backend.session(key)
    async def execute():
        if method == 'narrative/run':
            from .narrative_execution import prepare, run
            prepared = await asyncio.to_thread(prepare, backend.config, params['options'])
            if prepared['status'] != 'ready':
                return publish(backend, key, prepared, title='有限认知推演', field='narrativeResult')
            if params.get('expectedFingerprint') is not None and params['expectedFingerprint'] != prepared['fingerprint']:
                raise ValueError('Narrative preview changed; review the current visible evidence.')
            await _approval(backend, key, {'action': 'narrative_inference',
                'fingerprint': prepared['fingerprint'], 'preview': prepared})
            value = await settled_worker(run, backend.config, params['options'],
                expected_fingerprint=prepared['fingerprint'])
            return publish(backend, key, value, title='有限认知推演 · 候选分析', field='narrativeResult')
        from .literary_tasks import prepare, start as begin, advance, read
        prepared = await asyncio.to_thread(prepare, backend.config, params['options']) if method == 'task/start' \
            else await asyncio.to_thread(read, backend.config, params['taskId'])
        if params.get('expectedFingerprint') is not None and params['expectedFingerprint'] != prepared.get('fingerprint'):
            raise ValueError('Literary task preview changed; refresh before continuing.')
        await _approval(backend, key, {'action': 'literary_task',
            'fingerprint': prepared['fingerprint'], 'preview': prepared})
        if method == 'task/start':
            value = await settled_worker(begin, backend.config, params['options'],
                expected_fingerprint=prepared['fingerprint'])
        else:
            value = await settled_worker(advance, backend.config, params['taskId'],
                expected_fingerprint=prepared['fingerprint'])
        return publish(backend, key, value, title='文学任务', field='literaryTask')
    return backend.schedule(key, execute, text={'task/start':'开始文学任务',
        'task/advance':'继续文学任务', 'narrative/run':'有限认知推演'}[method])


async def select(backend, key, params):
    """Selecting a direction is a separate existing creative operation."""
    from .literary_tasks import read, validate_current
    from .backend_studio import call
    value = await asyncio.to_thread(read, backend.config, params['taskId'])
    if value.get('status') != 'waiting_author' or value.get('fingerprint') != params['expectedFingerprint']:
        raise ValueError('Direction task changed; refresh its saved state before choosing.')
    await asyncio.to_thread(validate_current, backend.config, params['taskId'])
    decision = value.get('decision') or {}
    if params['optionId'] not in {option['id'] for option in decision.get('options', [])}:
        raise ValueError('Option does not belong to this task.')
    return await call(backend, key, {'operation': 'select-decision', 'arguments': {
        'slug': decision['slug'], 'options_version_number': decision['optionsVersionNumber'],
        'option_id': params['optionId'], 'expected_version_id': decision['expectedVersionId']}})


def sources(backend, params):
    from .project_store import ProjectStore
    store = ProjectStore(backend.config.workspace)
    document = store.get_document(params['document'])
    result = {'documentId': document.id, 'document': document.slug,
        'impact': store.source_impact(document.id, **({'version_number': params['version']} if 'version' in params else {})),
        'coverage': 'known_direct_associations_only', 'automaticChanges': False}
    if 'version' in params:
        result['generationSourceReview'] = store.generation_source_review(document.id, params['version'])
    candidates = {}
    for item in result['impact']['direct_dependents']['items']:
        if (item.get('kind') == 'document_version' and item.get('state') == 'candidate'
                and item.get('document_kind') in {'chapter','scene'}):
            candidate = store.get_version(item['document_id'], item['version_number'])
            if candidate.id == item['version_id'] and candidate.state == 'candidate':
                candidates[candidate.id] = {'document':item['slug'], 'version':candidate.version_number,
                    'title':item['title'], 'action':'review_then_author_adoption'}
    result['reviewCandidates'] = list(candidates.values())
    result['nextActions'] = ['Review affected candidates with the existing review/accept flow.',
        'Re-propose changed facts, directions or knowledge with current sources for an explicit author decision.',
        'Keep existing author confirmations until an explicit replacement or withdrawal.']
    return result
