from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lg_cli.core_adapter import discover_core_commands
from lg_cli.runtime_manifest import read_runtime_identity

ROOT = Path(__file__).resolve().parents[1]


class RuntimeManifestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.core = self.root / 'core' / 'codex'
        self.core.mkdir(parents=True)
        (self.root / 'CODEX_CORE_COMMIT').write_text('a' * 40)
        self.binary = self.root / 'lg-runtime'
        self.binary.write_text('#!/bin/sh\nprintf "lg-runtime 0.1\\n"\n')
        self.binary.chmod(0o755)
        self.manifest = self.root / 'runtime.json'
        self.data = {
            'schema_version': 2, 'kind': 'lg-derived', 'commit': 'a' * 40,
            'tag': 'rust-v0.154.0', 'version': 'lg-runtime 0.1',
            'source_commit': 'b' * 40, 'source_tree': 'c' * 40,
            'build_profile': 'lg-api-only', 'protocol': 'codex-app-server-v2',
            'binary': str(self.binary),
            'binary_sha256': hashlib.sha256(self.binary.read_bytes()).hexdigest(),
        }

    def candidates(self, data=None):
        self.manifest.write_text(json.dumps(self.data if data is None else data))
        with patch.dict(os.environ, {'LG_CODEX_RUNTIME_MANIFEST': str(self.manifest), 'LG_CODEX_COMMAND': 'must-not-fallback'}):
            return discover_core_commands(self.core)

    def test_derivative_has_its_own_version_but_retains_upstream_pin(self):
        candidates = self.candidates()
        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0].available, candidates[0].reason)
        self.assertEqual(candidates[0].version, 'lg-runtime 0.1')

    def test_identity_failures_never_probe_or_fall_back(self):
        for field, value in (
            ('schema_version', True), ('schema_version', 99), ('kind', 'unknown'),
            ('commit', 'd' * 40), ('source_commit', None), ('source_tree', 'bad'),
            ('build_profile', ''), ('protocol', 'future-protocol'),
            ('binary_sha256', '0' * 64), ('binary', 'relative/binary'),
            ('tag', 'main'), ('version', 'bad\nversion'),
        ):
            with self.subTest(field=field, value=value), patch('subprocess.run') as run:
                candidates = self.candidates({**self.data, field: value})
                self.assertEqual(len(candidates), 1)
                self.assertFalse(candidates[0].available)
                run.assert_not_called()

    def test_version_probe_mismatch_is_rejected(self):
        candidates = self.candidates({**self.data, 'version': 'lg-runtime wrong'})
        self.assertFalse(candidates[0].available)
        self.assertIn('version probe', candidates[0].reason)

    def test_v1_cannot_claim_to_be_a_derivative(self):
        self.assertFalse(self.candidates({**self.data, 'schema_version': 1})[0].available)

    def test_absent_manifest_does_not_search_path_or_build(self):
        with patch.dict(os.environ, {}, clear=True), patch('shutil.which') as which, patch('subprocess.run') as run:
            candidates = discover_core_commands(self.core)
        self.assertEqual(len(candidates), 1)
        self.assertFalse(candidates[0].available)
        which.assert_not_called()
        run.assert_not_called()

    def test_explicit_command_failure_has_no_fallback(self):
        with patch.dict(os.environ, {'LG_CODEX_COMMAND': 'missing-lg-runtime'}, clear=True):
            candidates = discover_core_commands(self.core)
        self.assertEqual(len(candidates), 1)
        self.assertFalse(candidates[0].available)

    def test_invalid_manifest_does_not_echo_its_contents(self):
        self.manifest.write_text('{secret-broken-json')
        with self.assertRaises(ValueError) as error:
            read_runtime_identity(self.manifest, pin='a' * 40)
        self.assertNotIn('secret', str(error.exception))


class DerivativeRegistrationTests(unittest.TestCase):
    def test_clean_source_registration_and_drift_refusal(self):
        spec = importlib.util.spec_from_file_location('register_runtime', ROOT / 'scripts' / 'register_codex_runtime.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / 'source'
            source.mkdir()
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=source, text=True, stderr=subprocess.DEVNULL).strip()
            git('init')
            git('config', 'user.name', 'Runtime Test')
            git('config', 'user.email', 'runtime@example.invalid')
            (source / 'source.txt').write_text('upstream')
            git('add', 'source.txt')
            git('-c', 'commit.gpgsign=false', 'commit', '-m', 'baseline')
            pin = git('rev-parse', 'HEAD')
            git('tag', 'rust-v0.154.0')
            (root / 'CODEX_CORE_COMMIT').write_text(pin)
            (source / 'source.txt').write_text('derivative')
            git('add', 'source.txt')
            git('-c', 'commit.gpgsign=false', 'commit', '-m', 'derivative')
            binary = root / 'runtime'
            binary.write_text('#!/bin/sh\nprintf "lg-runtime test\\n"\n')
            binary.chmod(0o755)
            output = root / 'runtime.json'
            argv = ['register', '--source', str(source), '--binary', str(binary),
                '--tag', 'rust-v0.154.0', '--build-profile', 'lg-api-only',
                '--version', 'lg-runtime test', '--output', str(output)]
            with patch.object(module, 'ROOT', root), patch('sys.argv', argv):
                self.assertEqual(module.main(), 0)
                before = output.read_bytes()
                identity = read_runtime_identity(output, pin=pin)
                self.assertEqual(identity.kind, 'lg-derived')
                with self.assertRaises(FileExistsError):
                    module.main()
                self.assertEqual(output.read_bytes(), before)
                (source / 'untracked.txt').write_text('drift')
                with self.assertRaisesRegex(ValueError, 'Commit derivative'):
                    module.main()
                self.assertEqual(output.read_bytes(), before)


class UpstreamInstallerTests(unittest.TestCase):
    def test_installer_emits_a_valid_v2_identity_without_replacing_binary(self):
        spec = importlib.util.spec_from_file_location('install_runtime', ROOT / 'scripts' / 'install_codex_runtime.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            pin = 'a' * 40
            (root / 'CODEX_CORE_COMMIT').write_text(pin)
            binary = b'#!/bin/sh\nprintf "codex-cli 0.154.0\\n"\n'
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode='w:gz') as bundle:
                info = tarfile.TarInfo('codex-x86_64-unknown-linux-musl')
                info.size = len(binary)
                bundle.addfile(info, io.BytesIO(binary))
            content = archive.getvalue()
            release = {'tag_name': 'rust-v0.154.0', 'html_url': 'https://example.invalid/release',
                'assets': [{'name': 'codex-x86_64-unknown-linux-musl.tar.gz',
                    'digest': 'sha256:' + hashlib.sha256(content).hexdigest(),
                    'browser_download_url': 'https://example.invalid/asset'}]}
            destination = root / 'installed'
            argv = ['install', '--tag', 'rust-v0.154.0', '--destination', str(destination)]
            with patch.object(module, '__file__', str(root/'scripts'/'install_codex_runtime.py')), \
                 patch('sys.argv', argv), patch.object(module, 'fetch_json', return_value=release), \
                 patch.object(module.platform, 'system', return_value='Linux'), \
                 patch.object(module.platform, 'machine', return_value='x86_64'), \
                 patch.object(module.urllib.request, 'urlopen', side_effect=lambda *a, **kw: io.BytesIO(content)), \
                 patch.object(module.subprocess, 'check_output', side_effect=[pin, 'codex-cli 0.154.0', 'c' * 40, pin]), \
                 patch.object(module.subprocess, 'run'):
                self.assertEqual(module.main(), 0)
                manifest = destination / 'rust-v0.154.0' / 'runtime.json'
                identity = read_runtime_identity(manifest, pin=pin)
                self.assertEqual(identity.kind, 'upstream-unpatched')
                self.assertEqual(identity.binary.read_bytes(), binary)
                with self.assertRaises(FileExistsError):
                    module.main()


class FrozenUpdateTests(unittest.TestCase):
    def test_default_check_is_offline_and_apply_requires_explicit_target(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=root, text=True, stderr=subprocess.DEVNULL).strip()
            git('init')
            git('config', 'user.name', 'Runtime Test')
            git('config', 'user.email', 'runtime@example.invalid')
            (root / 'upstream.txt').write_text('upstream')
            git('add', 'upstream.txt')
            git('-c', 'commit.gpgsign=false', 'commit', '-m', 'upstream')
            pin = git('rev-parse', 'HEAD')
            core = root / 'core' / 'codex'
            core.mkdir(parents=True)
            (core / 'upstream.txt').write_text('upstream')
            (root / 'CODEX_CORE_COMMIT').write_text(pin)
            scripts = root / 'scripts'
            scripts.mkdir()
            script = scripts / 'update_codex_core.sh'
            script.write_bytes((ROOT / 'scripts' / script.name).read_bytes())
            git('add', '.')
            git('-c', 'commit.gpgsign=false', 'commit', '-m', 'product')
            env = {key: value for key, value in os.environ.items() if not key.startswith('LG_CODEX_')}
            def run(flag):
                return subprocess.run(['bash', str(script), flag], cwd=root, env=env, text=True, capture_output=True)
            checked = run('--check')
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertIn('no upstream request', checked.stdout)
            applied = run('--apply')
            self.assertEqual(applied.returncode, 2)
            self.assertIn('frozen', applied.stderr)
            self.assertEqual(git('status', '--porcelain'), '')
            (core / 'untracked.txt').write_text('drift')
            self.assertNotEqual(run('--verify').returncode, 0)
