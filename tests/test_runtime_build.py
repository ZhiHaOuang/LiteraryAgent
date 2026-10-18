from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
REAL_RUN = subprocess.run
SPEC = importlib.util.spec_from_file_location('build_codex_runtime', ROOT / 'scripts/build_codex_runtime.py')
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class RuntimeBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / 'source'
        self.source.mkdir()
        self.product = self.root / 'product'
        self.product.mkdir()
        self.workspace = self.source / 'codex-rs'
        self.workspace.mkdir()
        (self.workspace / 'Cargo.lock').write_text('version = 4\n')
        (self.workspace / 'rust-toolchain.toml').write_text('[toolchain]\nchannel = "1.95.0"\n')
        (self.workspace / 'Cargo.toml').write_text('[workspace.package]\nversion = "0.154.0"\n')
        self.git('init')
        self.git('add', '.')
        self.git('-c', 'user.name=Build Test', '-c', 'user.email=build@example.invalid',
                 '-c', 'commit.gpgsign=false', 'commit', '-m', 'pinned source')
        self.pin = self.git('rev-parse', 'HEAD')
        (self.product / 'CODEX_CORE_COMMIT').write_text(self.pin)
        self.args = argparse.Namespace(source=self.source, output=self.root / 'evidence',
            source_commit=None, target_dir=None, cargo='cargo', jobs=2, profile='dev-small', binary=None, offline=True)
        self.addCleanup(patch.stopall)
        patch.object(builder, 'ROOT', self.product).start()

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.source, text=True,
                                       stderr=subprocess.DEVNULL).strip()

    def fake_run(self, command, **kwargs):
        if command[0] == 'git':
            return REAL_RUN(command, **kwargs)
        stage = Path(kwargs['stdout'].name).stem
        output = {'rustc': 'release: 1.95.0\nhost: x86_64-unknown-linux-gnu\n', 'cargo-version': 'cargo 1.95.0\n',
                  'metadata': '{}\n', 'version-codex': 'codex-cli 0.154.0\n'}.get(stage, '')
        kwargs['stdout'].write(output)
        if stage == 'build-codex':
            binary = Path(kwargs['env']['CARGO_TARGET_DIR']) / 'dev-small' / ('codex.exe' if os.name == 'nt' else 'codex')
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b'fixture executable')
        return subprocess.CompletedProcess(command, 0)

    def test_rejects_wrong_revision_and_dirty_source_before_cargo(self):
        (self.product / 'CODEX_CORE_COMMIT').write_text('a' * 40)
        with patch.object(builder.shutil, 'which') as which:
            with self.assertRaisesRegex(ValueError, 'must equal'):
                builder.build(self.args)
            (self.product / 'CODEX_CORE_COMMIT').write_text(self.pin)
            (self.source / 'untracked.txt').write_text('uncommitted change')
            with self.assertRaisesRegex(ValueError, 'must be clean'):
                builder.build(self.args)
            which.assert_not_called()
        self.assertFalse(self.args.output.exists())

    def test_rejects_vendored_source_and_output_inside_source(self):
        with patch.object(builder, 'ROOT', self.root):
            with self.assertRaisesRegex(ValueError, 'outside source and product'):
                builder.build(self.args)
        self.args.output = self.source / 'build-output'
        with self.assertRaisesRegex(ValueError, 'outside source and product'):
            builder.build(self.args)
        self.assertFalse(self.args.output.exists())

    def test_success_is_unvalidated_and_does_not_register_or_overwrite(self):
        with patch.object(builder.shutil, 'which', return_value='/test/cargo'), \
             patch.object(builder.subprocess, 'run', side_effect=self.fake_run):
            report = builder.build(self.args)
            self.assertEqual(report['status'], 'built_unvalidated')
            self.assertEqual(report['runtime_validation'], 'not_run')
            self.assertFalse(report['active_runtime_changed'])
            self.assertEqual(report['source_identity']['commit'], self.pin)
            self.assertEqual(report['artifacts'][0]['bytes'], len(b'fixture executable'))
            self.assertEqual(Path(report['artifacts'][0]['binary']).parent, self.args.output / 'bin')
            self.assertFalse((self.args.output / 'runtime.json').exists())
            command = next(c for c in report['commands'] if 'build' in c)
            self.assertIn('--locked', command)
            self.assertIn('--offline', command)
            before = (self.args.output / 'build.json').read_bytes()
            with self.assertRaises(FileExistsError):
                builder.build(self.args)
            self.assertEqual((self.args.output / 'build.json').read_bytes(), before)

    def test_build_failure_records_stage_without_publishing_success(self):
        def fail(command, **kwargs):
            if 'build' in command:
                return subprocess.CompletedProcess(command, 101)
            return self.fake_run(command, **kwargs)
        with patch.object(builder.shutil, 'which', return_value='/test/cargo'), \
             patch.object(builder.subprocess, 'run', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'build-codex failed'):
                builder.build(self.args)
        report = json.loads((self.args.output / 'build.json').read_text())
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['stage'], 'build-codex')
        self.assertEqual(report['artifacts'], [])

    def test_source_mutation_during_build_invalidates_evidence(self):
        def mutate(command, **kwargs):
            result = self.fake_run(command, **kwargs)
            if 'build' in command:
                (self.workspace / 'Cargo.lock').write_text('changed dependency lock\n')
            return result
        with patch.object(builder.shutil, 'which', return_value='/test/cargo'), \
             patch.object(builder.subprocess, 'run', side_effect=mutate):
            with self.assertRaisesRegex(ValueError, 'must be clean'):
                builder.build(self.args)
        report = json.loads((self.args.output / 'build.json').read_text())
        self.assertEqual(report['status'], 'failed')
        self.assertFalse((self.args.output / 'runtime.json').exists())

    def test_descendant_requires_explicit_commit_and_keeps_upstream_identity(self):
        (self.workspace / 'Cargo.lock').write_text('version = 4\n# local release metadata\n')
        self.git('add', '.')
        self.git('-c', 'user.name=Build Test', '-c', 'user.email=build@example.invalid',
                 '-c', 'commit.gpgsign=false', 'commit', '-m', 'release metadata')
        commit = self.git('rev-parse', 'HEAD')
        with self.assertRaisesRegex(ValueError, 'must equal'):
            builder.source_identity(self.source, self.pin)
        identity = builder.source_identity(self.source, self.pin, commit)
        self.assertEqual(identity['commit'], commit)
        self.assertEqual(identity['upstream_commit'], self.pin)

    def test_unrelated_explicit_commit_is_rejected(self):
        self.git('checkout', '--orphan', 'unrelated')
        self.git('-c', 'user.name=Build Test', '-c', 'user.email=build@example.invalid',
                 '-c', 'commit.gpgsign=false', 'commit', '-m', 'unrelated source')
        with self.assertRaises(subprocess.CalledProcessError):
            builder.source_identity(self.source, self.pin, self.git('rev-parse', 'HEAD'))
