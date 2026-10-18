"""Host-captured inputs, not claims about model influence or complete context."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
from typing import Any


SCHEMA = 'lg.generation-sources.v1'
RESERVED_METADATA = frozenset({'generation_sources', 'generation_output_sha256'})
_receipt: ContextVar[dict | None] = ContextVar('lg_generation_sources', default=None)
_include_non_candidate: ContextVar[bool] = ContextVar('lg_generation_sources_import', default=False)


def canonical_sha(value: Any) -> str:
    return text_sha(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False))


def text_sha(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _identity(value):
    return type(value) is int and value > 0


def _document_ref(value, exposure):
    identity = value.get('document_id', value.get('id'))
    version = value.get('active_version_id', value.get('version_id'))
    if _identity(identity) and _identity(version):
        return {'kind': 'document', 'document_id': identity, 'version_id': version, 'exposure': exposure}
    return None


def _decision_ref(value):
    identity = value.get('document_id')
    selection = value.get('selection')
    if not _identity(identity):
        return None
    semantic = {key: value.get(key) for key in ('topic', 'status', 'selection', 'source_status', 'source_details')}
    result = {'kind': 'creative_decision', 'document_id': identity,
        'selection_sha256': canonical_sha(semantic), 'exposure': 'selected'}
    if isinstance(selection, dict):
        result.update(options_version_id=selection.get('options_version_id'), option_id=selection.get('option_id'))
    else:
        result['exposure'] = 'unselected'
    return result


def make_observation(tool: str, payload: Any) -> dict:
    """Summarize the actual structured return. Never resolve references against live state."""
    refs = []
    if isinstance(payload, dict):
        if tool == 'project_memory':
            for document in payload.get('documents', []):
                ref = _document_ref(document, 'index') if isinstance(document, dict) else None
                if ref:
                    refs.append(ref)
            for decision in payload.get('creative_decisions', []):
                ref = _decision_ref(decision) if isinstance(decision, dict) else None
                if ref:
                    refs.append(ref)
            for name in ('facts', 'ideas'):
                for fact in payload.get(name, []):
                    if isinstance(fact, dict) and _identity(fact.get('id')):
                        refs.append({'kind': 'fact', 'fact_id': fact['id'], 'payload_sha256': canonical_sha(fact),
                            'exposure': name})
        elif tool == 'read_document':
            ref = _document_ref(payload, 'content')
            if ref:
                refs.append(ref)
        elif tool == 'studio_decision':
            ref = _decision_ref(payload)
            if ref:
                refs.append(ref)
    return {'tool': tool, 'payload_sha256': canonical_sha(payload), 'refs': refs,
        'coverage': 'partial' if refs else 'unknown'}


def make_receipt(*, mode: str, run_id: str, observations: list[dict], stage_id: str | None = None,
                 prompt: str | None = None, instructions: str | None = None,
                 unknown_reasons: list[str] | None = None) -> dict:
    value = {'schema': SCHEMA, 'mode': mode, 'run_id': run_id,
        'coverage': 'partial' if observations else 'unknown', 'observations': _copy(observations),
        'unknown_reasons': list(unknown_reasons or [])}
    if stage_id is not None:
        value['stage_id'] = stage_id
    if prompt is not None or instructions is not None:
        value['input'] = {'prompt_sha256': text_sha(prompt or ''), 'instructions_sha256': text_sha(instructions or '')}
    return value


@contextmanager
def bind_generation_sources(receipt: dict | None, *, include_non_candidate: bool = False):
    """Use only at host capture/import boundaries. None explicitly suppresses inheritance."""
    if receipt is not None and (not isinstance(receipt, dict) or receipt.get('schema') != SCHEMA
                                or receipt.get('coverage') not in {'partial', 'unknown'}):
        raise ValueError('Invalid generation source receipt.')
    token = _receipt.set(_copy(receipt))
    import_token = _include_non_candidate.set(include_non_candidate)
    try:
        yield
    finally:
        _include_non_candidate.reset(import_token)
        _receipt.reset(token)


def check_metadata(metadata: dict | None) -> None:
    if metadata and RESERVED_METADATA.intersection(metadata):
        raise ValueError('Generation source metadata is reserved for host capture.')


def version_metadata(metadata: dict, *, content: str, state: str) -> dict:
    check_metadata(metadata)
    value = dict(metadata)
    receipt = _receipt.get()
    if receipt is not None and (state == 'candidate' or _include_non_candidate.get()):
        value.update(generation_sources=_copy(receipt), generation_output_sha256=text_sha(content))
    return value


def inherited_receipt(metadata: dict, content: str) -> dict | None:
    receipt = metadata.get('generation_sources')
    if receipt is not None and metadata.get('generation_output_sha256') != text_sha(content):
        raise ValueError('Generation source receipt does not match the stored output.')
    return receipt
