"""Local structured-output contracts shared by execution adapters."""
import json
from pathlib import Path

import jsonschema


def validate_local_schema(schema):
    jsonschema.Draft202012Validator.check_schema(schema)

    def check(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {'$ref', '$dynamicRef', '$recursiveRef'} and (
                    not isinstance(child, str) or not child.startswith('#')
                ):
                    raise ValueError('Only local schema references are supported')
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)

    check(schema)


def read_schema(path: Path) -> tuple[str, dict | bool]:
    text = path.read_text(encoding='utf-8')
    schema = json.loads(text)
    validate_local_schema(schema)
    return text, schema


def validate_output(text: str, schema: dict | bool) -> None:
    jsonschema.validate(json.loads(text), schema)
