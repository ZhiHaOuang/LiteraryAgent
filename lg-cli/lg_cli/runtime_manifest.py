"""Local runtime identity checks, independent of literary state and model auth."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimeIdentity:
    binary: Path
    version: str
    kind: str


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _hex(value: object, length: int) -> bool:
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{' + str(length) + '}', value) is not None


def read_runtime_identity(path: Path, *, pin: str | None) -> RuntimeIdentity:
    try:
        manifest = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise ValueError('Cannot read runtime manifest') from None
    if not isinstance(manifest, dict) or type(manifest.get('schema_version')) is not int or manifest['schema_version'] not in {1, 2}:
        raise ValueError('Unsupported runtime manifest schema')
    if not _hex(pin, 40) or manifest.get('commit') != pin:
        raise ValueError('Runtime commit does not match CODEX_CORE_COMMIT')
    schema = manifest['schema_version']
    kind = manifest.get('kind', 'upstream-unpatched' if schema == 1 else None)
    if kind not in {'upstream-unpatched', 'lg-derived'} or (schema == 1 and kind != 'upstream-unpatched'):
        raise ValueError('Unsupported runtime kind')
    version = manifest.get('version')
    tag = manifest.get('tag')
    if not isinstance(tag, str) or re.fullmatch(r'rust-v\d+\.\d+\.\d+', tag) is None:
        raise ValueError('Invalid upstream runtime tag')
    if not isinstance(version, str) or not version.strip() or len(version) > 200 or any(ord(c) < 32 for c in version):
        raise ValueError('Invalid runtime version')
    if kind == 'upstream-unpatched' and version != f'codex-cli {tag[6:]}':
        raise ValueError('Runtime tag and version mismatch')
    if schema == 2:
        if manifest.get('protocol') != 'codex-app-server-v2':
            raise ValueError('Unsupported runtime protocol')
        profile = manifest.get('build_profile')
        if not isinstance(profile, str) or re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', profile) is None:
            raise ValueError('Invalid runtime build profile')
        if not _hex(manifest.get('source_commit'), 40) or not _hex(manifest.get('source_tree'), 40):
            raise ValueError('Runtime source provenance is missing')
        if kind == 'upstream-unpatched' and manifest['source_commit'] != pin:
            raise ValueError('Upstream source commit does not match pin')
    binary = manifest.get('binary')
    if not isinstance(binary, str) or not Path(binary).is_absolute():
        raise ValueError('Runtime binary path must be absolute')
    checksum = manifest.get('binary_sha256')
    if not _hex(checksum, 64):
        raise ValueError('Invalid runtime binary checksum')
    try:
        actual = file_sha256(Path(binary))
    except OSError:
        raise ValueError('Cannot read runtime binary') from None
    if actual != checksum:
        raise ValueError('Runtime binary checksum mismatch')
    return RuntimeIdentity(Path(binary), version, kind)
