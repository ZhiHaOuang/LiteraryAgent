"""Read-only process provenance; no credentials, provider calls or runtime probes."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
from importlib import metadata
from pathlib import Path

from . import __version__


def _package_source(package: Path) -> dict:
    digest = hashlib.sha256()
    count = 0
    try:
        # Only shipped code/resource extensions, never workspace data or caches.
        for path in sorted(package.rglob('*')):
            if path.suffix not in {'.py', '.json', '.txt'} or not path.is_file():
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(package):
                return {'sourceSha256': None, 'sourceStatus': 'unavailable'}
            relative = path.relative_to(package).as_posix()
            digest.update(relative.encode() + b'\0' + hashlib.sha256(path.read_bytes()).digest())
            count += 1
    except OSError:
        return {'sourceSha256': None, 'sourceStatus': 'unavailable'}
    return {'sourceSha256': digest.hexdigest(), 'sourceStatus': 'read', 'sourceFiles': count,
            'sourceScope': 'imported-package-python-json-txt'}


def _runtime(config) -> dict:
    value = {'engine': config.runtime_engine, 'verified': False}
    if config.runtime_engine == 'native':
        return {**value, 'selection': 'native'}
    selected = os.environ.get('LG_CODEX_RUNTIME_MANIFEST', '').strip() or config.runtime_manifest
    if not selected:
        return {**value, 'selection': 'explicit-development-command' if os.environ.get('LG_CODEX_COMMAND', '').strip()
                else 'unconfigured'}
    path = Path(selected).expanduser().resolve()
    value.update(selection='manifest', manifest=str(path))
    try:
        # No referenced binary is opened and arbitrary manifest fields are not returned.
        if path.stat().st_size > 65536:
            raise ValueError()
        raw = path.read_bytes()
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError()
    except (OSError, ValueError):
        return {**value, 'status': 'unreadable'}
    declared = {}
    for field in ('kind', 'version', 'tag', 'protocol', 'build_profile'):
        item = data.get(field)
        if isinstance(item, str) and 0 < len(item) <= 200 and all(ord(c) >= 32 for c in item):
            declared[field] = item
    for field, length in (('commit', 40), ('source_commit', 40), ('source_tree', 40), ('binary_sha256', 64)):
        item = data.get(field)
        if isinstance(item, str) and re.fullmatch('[0-9a-f]{' + str(length) + '}', item):
            declared[field] = item
    if type(data.get('schema_version')) is int:
        declared['schema_version'] = data['schema_version']
    return {**value, 'status': 'declared-only', 'manifestSha256': hashlib.sha256(raw).hexdigest(),
            'declared': declared}


def runtime_identity(config) -> dict:
    """Identify the imported package, not an assumed checkout or installed Runtime.

    Source hash covers the imported Python package and bundled JSON/text resources,
    not the UI, a complete Git tree, or a reproducible build. Manifest fields are
    declarations; normal Runtime startup still performs its existing validation.
    """
    package = Path(__file__).resolve().parent
    try:
        distribution_version = metadata.version('literarygiant-cli')
    except metadata.PackageNotFoundError:
        distribution_version = None
    return {'schema': 'lg.runtime-identity.v1',
            'python': {'executable': str(Path(sys.executable).resolve()), 'version': platform.python_version(),
                       'implementation': platform.python_implementation(), 'machine': platform.machine()},
            'package': {'name': 'literarygiant-cli', 'version': __version__, 'path': str(package),
                        'distributionVersion': distribution_version, **_package_source(package)},
            'runtime': _runtime(config)}


def verify_manifest(config, value=None):
    """Opt-in file verification; no executable, login or model is started."""
    value = value or runtime_identity(config)
    runtime = dict(value['runtime'])
    if runtime.get('selection') != 'manifest':
        return {**value, 'runtime': {**runtime, 'verification': 'not_applicable'}}
    from .core_adapter import _bundled_core_pin
    from .runtime_manifest import read_runtime_identity, file_sha256
    try:
        manifest = Path(runtime['manifest'])
        # Verify against the same manifest bytes that the public identity names.
        before = hashlib.sha256(manifest.read_bytes()).hexdigest()
        if before != runtime.get('manifestSha256'):
            raise ValueError('Runtime manifest changed while checking identity')
        checked = read_runtime_identity(manifest, pin=_bundled_core_pin())
        digest = file_sha256(checked.binary)
        if (hashlib.sha256(manifest.read_bytes()).hexdigest() != before or
                digest != runtime['declared'].get('binary_sha256')):
            raise ValueError('Runtime changed while checking identity')
    except (OSError, ValueError):
        return {**value, 'runtime': {**runtime, 'verified': False,
            'verification': 'failed', 'reason': 'manifest_or_binary_invalid_or_changed'}}
    return {**value, 'runtime': {**runtime, 'verified': True,
        'binary': str(checked.binary), 'binarySha256': digest,
        'verification': 'file_hash_and_pin_only', 'processProbed': False}}
