"""Build pinned Codex or an explicitly selected descendant in an isolated checkout.

Produces evidence, not an active runtime manifest. Protocol and product checks
must pass separately before an artifact can replace the installed runtime.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {'codex': 'codex-cli', 'codex-app-server': 'codex-app-server'}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def source_identity(source: Path, pin: str, source_commit: str | None = None) -> dict[str, str]:
    def git(*args: str) -> str:
        return subprocess.check_output(['git', *args], cwd=source, text=True,
                                       stderr=subprocess.PIPE).strip()

    if source.is_relative_to(ROOT.resolve()):
        raise ValueError('Build from an isolated checkout outside the product repository')
    if Path(git('rev-parse', '--show-toplevel')).resolve() != source:
        raise ValueError('Source must be the root of an isolated Codex checkout')
    expected = source_commit or pin
    if re.fullmatch('[0-9a-f]{40}', expected) is None:
        raise ValueError('source-commit must be a full commit hash')
    if git('rev-parse', 'HEAD') != expected:
        raise ValueError('Source must equal the explicit source-commit or CODEX_CORE_COMMIT')
    git('merge-base', '--is-ancestor', pin, expected)
    if git('status', '--porcelain', '--untracked-files=all'):
        raise ValueError('Baseline source must be clean, including untracked files')
    return {'upstream_commit': pin, 'commit': expected, 'tree': git('rev-parse', 'HEAD^{tree}'),
            'cargo_lock_sha256': digest(source / 'codex-rs/Cargo.lock')}


def build(args: argparse.Namespace) -> dict:
    source = args.source.expanduser().resolve(strict=True)
    output = args.output.expanduser().resolve()
    target = args.target_dir.expanduser().resolve() if args.target_dir else output / 'target'
    for path in (output, target):
        if path.is_relative_to(source) or path.is_relative_to(ROOT.resolve()):
            raise ValueError('Build output and target directory must be outside source and product')
    if args.jobs < 1:
        raise ValueError('jobs must be positive')
    pin = (ROOT / 'CODEX_CORE_COMMIT').read_text().strip()
    if re.fullmatch('[0-9a-f]{40}', pin) is None:
        raise ValueError('Invalid CODEX_CORE_COMMIT')
    before = source_identity(source, pin, args.source_commit)
    workspace = source / 'codex-rs'
    toolchain = tomllib.loads((workspace / 'rust-toolchain.toml').read_text())['toolchain']['channel']
    version = tomllib.loads((workspace / 'Cargo.toml').read_text())['workspace']['package']['version']
    executable = shutil.which(args.cargo)
    if executable is None:
        raise ValueError('Cargo executable not found; install the pinned toolchain separately')
    # Do not resolve the cargo symlink: rustup dispatches using its executable name.
    cargo = str(Path(executable).absolute())
    env = dict(os.environ, RUSTUP_TOOLCHAIN=toolchain, CARGO_TARGET_DIR=str(target))
    env['PATH'] = str(Path(cargo).parent) + os.pathsep + env.get('PATH', '')
    output.mkdir(parents=True, exist_ok=False)
    report = {'schema_version': 1, 'status': 'building', 'source': str(source),
              'source_identity': before, 'toolchain': toolchain, 'profile': args.profile,
              'source_kind': 'upstream' if before['commit'] == pin else 'derived',
              'builder_sha256': digest(Path(__file__)),
              'target_dir': str(target), 'jobs': args.jobs, 'offline': args.offline,
              'commands': [], 'artifacts': [], 'runtime_validation': 'not_run',
              'active_runtime_changed': False}
    started = time.monotonic()

    def save() -> None:
        (output / 'build.json').write_text(json.dumps(report, indent=2) + '\n')

    def run(stage: str, command: list[str]) -> str:
        report['stage'] = stage
        report['commands'].append(command)
        save()
        with (output / f'{stage}.stdout').open('w') as stdout, (output / f'{stage}.stderr').open('w') as stderr:
            result = subprocess.run(command, cwd=workspace, env=env, stdout=stdout, stderr=stderr,
                                    check=False)
        if result.returncode:
            raise RuntimeError(f'{stage} failed with exit code {result.returncode}; see build logs')
        return (output / f'{stage}.stdout').read_text()

    try:
        report['rustc'] = run('rustc', ['rustc', '-vV']).strip()
        if f'release: {toolchain}' not in report['rustc'].splitlines():
            raise ValueError('rustc version does not match the pinned toolchain')
        report['cargo'] = run('cargo-version', [cargo, '--version']).strip()
        flags = ['--locked', *(['--offline'] if args.offline else [])]
        host = next(line.removeprefix('host: ') for line in report['rustc'].splitlines() if line.startswith('host: '))
        report['host'] = host
        run('metadata', [cargo, 'metadata', *flags, '--format-version', '1', '--filter-platform', host])
        binaries = list(dict.fromkeys(args.binary or ['codex']))
        for name in binaries:
            run(f'build-{name}', [cargo, 'build', *flags, '--profile', args.profile,
                '--jobs', str(args.jobs), '-p', TARGETS[name], '--bin', name])
            binary = target / args.profile / (name + ('.exe' if os.name == 'nt' else ''))
            checksum = digest(binary)
            probe = run(f'version-{name}', [str(binary), '--version']).strip()
            expected = f'{TARGETS[name]} {version}'
            if probe != expected or digest(binary) != checksum:
                raise ValueError('Built artifact identity changed or has an unexpected version')
            artifact = output / 'bin' / binary.name
            artifact.parent.mkdir(exist_ok=True)
            shutil.copy2(binary, artifact)
            if digest(artifact) != checksum:
                raise ValueError('Artifact changed while copying it out of the build cache')
            report['artifacts'].append({'name': name, 'binary': str(artifact), 'version': probe,
                'sha256': checksum, 'bytes': binary.stat().st_size})
        if source_identity(source, pin, args.source_commit) != before:
            raise ValueError('Source identity changed during the build')
        report['status'] = 'built_unvalidated'
        report['stage'] = 'complete'
    except Exception as error:
        report['status'] = 'failed'
        report['error'] = str(error)
        raise
    finally:
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        save()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--source-commit', help='Explicit clean descendant commit; defaults to the upstream pin')
    parser.add_argument('--output', type=Path, required=True, help='New evidence directory; never overwritten')
    parser.add_argument('--target-dir', type=Path, help='Optional reusable external Cargo target directory')
    parser.add_argument('--cargo', default='cargo')
    parser.add_argument('--profile', choices=['dev-small', 'release'], default='dev-small')
    parser.add_argument('--binary', action='append', choices=sorted(TARGETS))
    parser.add_argument('--jobs', type=int, default=2)
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    try:
        report = build(args)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, f'Build refused or failed: {error}\n')
    print(json.dumps({'status': report['status'], 'report': str(args.output / 'build.json')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
