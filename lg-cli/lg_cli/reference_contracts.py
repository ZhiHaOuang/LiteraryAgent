"""Versioned Library instance-card contracts for new book analyses."""
from __future__ import annotations

import copy
import json
from importlib import resources

import jsonschema


def catalog() -> dict:
    return json.loads(resources.files('lg_cli.resources').joinpath('reference-contracts.json').read_text())


def _schema(value):
    if isinstance(value, dict):
        return {'type': 'object', 'additionalProperties': False, 'required': list(value),
                'properties': {key: _schema(item) for key, item in value.items()}}
    if isinstance(value, list):
        return {'type': 'array', 'maxItems': 32, 'items': _schema(value[0]) if value else {'type': 'string'}}
    if isinstance(value, int):
        return {'type': 'integer', 'minimum': 1}
    if value == '1-5':
        return {'type': 'integer', 'minimum': 1, 'maximum': 5}
    if value == '-2 to 2':
        return {'type': 'integer', 'minimum': -2, 'maximum': 2}
    if value == '0 to 2':
        return {'type': 'integer', 'minimum': 0, 'maximum': 2}
    if value in ('reader_only/character_only/shared/uncertain', 'private/public/relational/systemic/uncertain'):
        return {'type': 'string', 'enum': value.split('/')}
    return {'type': 'string', 'maxLength': 2000}


def card_schema(category: str) -> dict:
    data = catalog()
    if category not in data['libraries']:
        raise ValueError('Unknown reference category: ' + category)
    template = {key: [] for key in ('portable_core', 'implementation_details', 'source_locked_details', 'fusion_hooks')}
    template.update(data['INSTANCE_CARD_PROMPT_SCHEMAS'][category])
    return _schema(template)


def methods(category: str) -> str:
    spec = catalog()['libraries'][category]
    return ('## Reference methods: ' + category + '\n' + spec['perspective']
        + '\nFocus: ' + ', '.join(spec['focus_fields'])
        + '\nExclude: ' + '；'.join(spec['excluded_perspectives'])
        + '\nGenerate instance_card using the exact Library category fields. Review field types, category boundaries, '
          'and source evidence before submitting. Use empty strings/lists for facts the source does not establish; '
          'never invent missing details. Worldview event_examples.chunk_id must be one of this entry’s evidence span IDs. '
          'The host adds schema/version/quality metadata. This is interpretation, not Canonical promotion.')


def apply_contract(schema: dict, category: str, *, stored=False) -> dict:
    schema = copy.deepcopy(schema)
    entry = schema['properties']['entries']['items']
    contract = card_schema(category)
    if stored:
        for key, value in {
            'schema_version': {'const': 'narrative_instance_card.v1'},
            'card_type': {'const': catalog()['INSTANCE_CARD_TYPES'][category]},
            'grounding_mode': {'const': 'llm_grounded'},
            'derivation_warnings': {'type': 'array', 'items': {'type': 'string'}},
            'card_quality': {'type': 'object'},
        }.items():
            contract['properties'][key] = value
            contract['required'].append(key)
    entry['properties']['instance_card'] = contract
    if 'instance_card' not in entry['required']:
        entry['required'].append('instance_card')
    return schema


def finalize_cards(response: dict, category: str, spans: dict) -> dict:
    response = copy.deepcopy(response)
    data = catalog()
    for entry in response['entries']:
        card = entry['instance_card']
        jsonschema.validate(card, card_schema(category))
        if category == 'Worldview':
            ids = {e['span_id'] for e in entry['evidence']}
            for example in card['rule_application']['event_examples']:
                if example['chunk_id'] not in ids or example['chunk_id'] not in spans:
                    raise ValueError('Worldview example must cite this entry’s evidence span.')
        paths = data['COMMON_QUALITY_PATHS'] + data['LIBRARY_QUALITY_PATHS'][category]
        def populated(path):
            value = card
            for key in path.split('.'):
                value = value.get(key) if isinstance(value, dict) else None
            return bool(value)
        count = sum(populated(path) for path in paths)
        card.update(schema_version='narrative_instance_card.v1', card_type=data['INSTANCE_CARD_TYPES'][category],
                    grounding_mode='llm_grounded', derivation_warnings=[], card_quality={
                        'expected_field_count': len(paths), 'populated_field_count': count,
                        'directly_grounded_field_count': count, 'completeness': round(count / len(paths), 4),
                        'direct_grounding_completeness': round(count / len(paths), 4),
                        'needs_source_plot_lookup': count < len(paths)})
    return response
