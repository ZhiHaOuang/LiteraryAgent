import hashlib
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.helpers import make_config


class RuntimeIdentityTests(unittest.TestCase):
    def test_explicit_binary_verification_reads_hash_and_pin_without_execution(self):
        from lg_cli.runtime_identity import verify_manifest
        from lg_cli.core_adapter import _bundled_core_pin
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw).resolve();binary=root/'binary';binary.write_bytes(b'nonexecutable synthetic artifact')
            manifest=root/'runtime.json'
            manifest.write_text(json.dumps({'schema_version':1,'commit':_bundled_core_pin(),
                'kind':'upstream-unpatched','tag':'rust-v0.154.0','version':'codex-cli 0.154.0',
                'binary':str(binary),'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest()}))
            config=replace(make_config(root),runtime_manifest=manifest)
            with patch.dict(os.environ,{'LG_CODEX_RUNTIME_MANIFEST':''}), \
                    patch('subprocess.run',side_effect=AssertionError('No executable is run')):
                value=verify_manifest(config)['runtime']
                self.assertTrue(value['verified'])
                self.assertFalse(value['processProbed'])
                self.assertEqual(value['verification'],'file_hash_and_pin_only')
                binary.write_bytes(b'changed after registration')
                changed=verify_manifest(config)['runtime']
                self.assertFalse(changed['verified'])
                self.assertEqual(changed['verification'],'failed')

    def test_source_digest_changes_with_code_but_not_unrelated_cache(self):
        from lg_cli.runtime_identity import _package_source
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw)
            source = package / 'module.py'
            source.write_text('value = 1\n')
            first = _package_source(package)
            (package / 'module.pyc').write_bytes(b'cache')
            self.assertEqual(_package_source(package), first)
            source.write_text('value = 2\n')
            self.assertNotEqual(_package_source(package)['sourceSha256'], first['sourceSha256'])
            outside = package.parent / (package.name + '-absent')
            (package / 'linked.py').symlink_to(outside)
            # A dangling link is not imported source and cannot expose its target.
            self.assertEqual(_package_source(package)['sourceStatus'], 'read')

    def test_reports_imported_package_and_does_not_launch_or_read_credentials(self):
        from lg_cli.runtime_identity import runtime_identity
        import lg_cli
        with tempfile.TemporaryDirectory() as raw, patch('subprocess.run', side_effect=AssertionError('no process')), \
                patch('lg_cli.credentials.read_profiles', side_effect=AssertionError('no credentials')):
            config = replace(make_config(Path(raw)), runtime_engine='native')
            value = runtime_identity(config)
        self.assertEqual(value['python']['executable'], str(Path(sys.executable).resolve()))
        self.assertEqual(value['package']['path'], str(Path(lg_cli.__file__).resolve().parent))
        self.assertEqual(value['package']['version'], lg_cli.__version__)
        self.assertRegex(value['package']['sourceSha256'], r'^[0-9a-f]{64}$')
        self.assertEqual(value['runtime'], {'engine': 'native', 'selection': 'native', 'verified': False})
        self.assertNotIn(config.api_key, json.dumps(value))

    def test_manifest_reports_only_allowed_fields_and_environment_selection_wins(self):
        from lg_cli.runtime_identity import runtime_identity
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = root / 'runtime.json'
            payload = {'schema_version': 1, 'commit': 'a' * 40, 'kind': 'upstream-unpatched',
                       'version': 'codex-cli 0.154.0', 'binary': '/not/read/by/identity',
                       'binary_sha256': 'b' * 64, 'api_key': 'PRIVATE_SENTINEL'}
            manifest.write_text(json.dumps(payload))
            config = replace(make_config(root), runtime_manifest=root / 'unused')
            with patch.dict(os.environ, {'LG_CODEX_RUNTIME_MANIFEST': str(manifest), 'LG_CODEX_COMMAND': 'PRIVATE_COMMAND'}):
                value = runtime_identity(config)['runtime']
            self.assertEqual(value['manifest'], str(manifest))
            self.assertEqual(value['manifestSha256'], hashlib.sha256(manifest.read_bytes()).hexdigest())
            self.assertEqual(value['declared']['commit'], 'a' * 40)
            self.assertFalse(value['verified'])
            self.assertNotIn('PRIVATE', json.dumps(value))

    def test_unavailable_manifest_and_development_command_are_not_echoed_as_errors(self):
        from lg_cli.runtime_identity import runtime_identity
        with tempfile.TemporaryDirectory() as raw:
            config = make_config(Path(raw))
            with patch.dict(os.environ, {'LG_CODEX_RUNTIME_MANIFEST': str(Path(raw) / 'absent')}):
                value = runtime_identity(config)['runtime']
            self.assertEqual(value['status'], 'unreadable')
            with patch.dict(os.environ, {'LG_CODEX_RUNTIME_MANIFEST': '', 'LG_CODEX_COMMAND': 'PRIVATE_COMMAND'}):
                value = runtime_identity(config)['runtime']
            self.assertEqual(value['selection'], 'explicit-development-command')
            self.assertNotIn('PRIVATE', json.dumps(value))
