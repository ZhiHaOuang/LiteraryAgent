"""Build an LG TUI overlay without modifying the vendored core tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path

import tomllib


def verify_workspace_lock(original: dict, updated: dict, version: str) -> None:
    normalized = json.loads(json.dumps(updated))
    originals = {p["name"]: p for p in original["package"] if "source" not in p}
    for package in normalized["package"]:
        if "source" not in package:
            before = originals.get(package["name"])
            if before is None or package["version"] not in {before["version"], version}:
                raise ValueError("Unexpected workspace dependency change")
            package["version"] = before["version"]
    if normalized != original:
        raise ValueError("Refusing a lock update that changes third-party dependencies")


def source_digest(source: Path) -> str:
    checksum = hashlib.sha256()
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            checksum.update(
                f"link:{path.relative_to(source)}:{os.readlink(path)}\0".encode()
            )
        elif path.is_file():
            checksum.update(f"file:{path.relative_to(source)}\0".encode())
            with path.open("rb") as stream:
                checksum.update(hashlib.file_digest(stream, "sha256").digest())
    return checksum.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--jobs", type=int, default=1)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    pin = (root / "CODEX_CORE_COMMIT").read_text().strip()
    subprocess.run([str(root / "scripts/update_codex_core.sh"), "--verify"], check=True)
    destination = args.destination.expanduser().resolve()
    if destination.is_relative_to(root / "core"):
        raise ValueError("Native builds must be outside the vendored core")
    source = destination / "source"
    patches = sorted((root / "native-ui" / "patches").glob("*.patch"))
    if not patches:
        raise ValueError("No LG UI patches found")
    patch_ids = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in patches}
    lock = destination / "native-runtime.json"
    if args.resume:
        manifest = json.loads(lock.read_text())
        if manifest.get("commit") != pin or manifest.get("patches") != patch_ids:
            raise ValueError("Core or patches changed; prepare a new build directory")
        if manifest.get("source_sha256") != source_digest(source):
            raise ValueError(
                "Prepared source changed or has no recorded checksum; refusing resume"
            )
    else:
        if destination.exists():
            raise FileExistsError(
                "Build destination exists; use --resume for a verified prepared build"
            )
        destination.mkdir(parents=True)
        source.mkdir()
        with tempfile.TemporaryDirectory(prefix="lg-source-") as temporary:
            archive = Path(temporary) / "source.tar"
            subprocess.run(
                ["git", "archive", "--format=tar", f"--output={archive}", pin],
                cwd=root,
                check=True,
            )
            with tarfile.open(archive) as bundle:
                bundle.extractall(source, filter="data")
        for patch in patches:
            command = ["git", "apply", "--unidiff-zero", str(patch)]
            subprocess.run(
                [*command[:2], "--check", *command[2:]], cwd=source, check=True
            )
            subprocess.run(command, cwd=source, check=True)
        manifest = {
            "schema_version": 1,
            "commit": pin,
            "patches": patch_ids,
            "kind": "lg-native-tui",
            "status": "prepared",
        }
        manifest["source_sha256"] = source_digest(source)
    lock.write_text(json.dumps(manifest, indent=2) + "\n")
    if args.prepare_only:
        print(lock)
        return 0
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = str(destination / "target")
    env["CARGO_PROFILE_DEV_DEBUG"] = "0"
    cargo_root = source / "codex-rs"
    cargo_lock = cargo_root / "Cargo.lock"
    original_lock = tomllib.loads(
        subprocess.check_output(
            ["git", "show", f"{pin}:codex-rs/Cargo.lock"], cwd=root, text=True
        )
    )
    version = tomllib.loads((cargo_root / "Cargo.toml").read_text())["workspace"][
        "package"
    ]["version"]
    subprocess.run(
        ["cargo", "update", "--workspace", "--offline"],
        cwd=cargo_root,
        env=env,
        check=True,
    )
    verify_workspace_lock(original_lock, tomllib.loads(cargo_lock.read_text()), version)
    manifest["cargo_lock_sha256"] = hashlib.sha256(cargo_lock.read_bytes()).hexdigest()
    manifest["source_sha256"] = source_digest(source)
    manifest["status"] = "prepared"
    lock.write_text(json.dumps(manifest, indent=2) + "\n")
    subprocess.run(
        [
            "cargo",
            "build",
            "--locked",
            "--bin",
            "codex-tui",
            "-p",
            "codex-tui",
            "-j",
            str(args.jobs),
        ],
        cwd=source / "codex-rs",
        env=env,
        check=True,
    )
    binary = destination / "target" / "debug" / "codex-tui"
    version = subprocess.check_output([str(binary), "--version"], text=True).strip()
    with binary.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    manifest.update(
        status="built", binary=str(binary), binary_sha256=checksum, version=version
    )
    lock.write_text(json.dumps(manifest, indent=2) + "\n")
    print(lock)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
