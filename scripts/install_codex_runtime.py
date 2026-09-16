"""Install a release-matched upstream runtime without replacing system Codex."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path


def fetch_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "LiteraryGiant-runtime-installer"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    release = fetch_json(f"https://api.github.com/repos/openai/codex/releases/tags/{args.tag}")
    if release.get("draft") or release.get("prerelease") or release.get("tag_name") != args.tag:
        raise ValueError("A matching stable release is required")
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("This installer currently supports Linux x86_64 only")
    target = "x86_64-unknown-linux-musl"
    name = f"codex-{target}.tar.gz"
    asset = next(item for item in release["assets"] if item["name"] == name)
    digest = asset.get("digest", "")
    if not digest.startswith("sha256:") or len(digest) != 71:
        raise ValueError("Release asset has no valid SHA-256 digest")
    commit = subprocess.check_output(
        ["git", "rev-parse", f"refs/tags/{args.tag}^{{commit}}"], cwd=root, text=True
    ).strip()
    pin = (root / "CODEX_CORE_COMMIT").read_text().strip()
    if pin != commit:
        raise ValueError("Update and verify CODEX_CORE_COMMIT before installing its runtime")
    subprocess.run([str(root / "scripts/update_codex_core.sh"), "--verify"], check=True)
    destination = args.destination.expanduser().resolve() / args.tag
    destination.mkdir(parents=True, exist_ok=True)
    binary = destination / "codex"
    if binary.exists():
        raise FileExistsError(f"Runtime already exists; refusing to replace: {binary}")
    with tempfile.TemporaryDirectory(prefix="lg-runtime-", dir=destination) as temporary:
        archive = Path(temporary) / name
        request = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": "LiteraryGiant"})
        checksum = hashlib.sha256()
        with urllib.request.urlopen(request, timeout=60) as response, archive.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                checksum.update(chunk)
                output.write(chunk)
        if checksum.hexdigest() != digest.removeprefix("sha256:"):
            raise ValueError("Downloaded release checksum does not match GitHub metadata")
        candidate = Path(temporary) / "codex"
        with tarfile.open(archive, "r:gz") as bundle:
            members = [member for member in bundle if member.isfile() and Path(member.name).name == f"codex-{target}"]
            if len(members) != 1:
                raise ValueError("Release archive must contain exactly one matching executable")
            source = bundle.extractfile(members[0])
            assert source is not None
            with source, candidate.open("wb") as output:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
        candidate.chmod(0o755)
        version = subprocess.check_output([str(candidate), "--version"], text=True, timeout=15).strip()
        if version != f"codex-cli {args.tag.removeprefix('rust-v')}":
            raise ValueError(f"Unexpected runtime version: {version}")
        with candidate.open("rb") as stream:
            binary_digest = hashlib.file_digest(stream, "sha256").hexdigest()
        manifest = {
            "schema_version": 1,
            "tag": args.tag,
            "commit": commit,
            "platform": target,
            "version": version,
            "asset_sha256": checksum.hexdigest(),
            "binary_sha256": binary_digest,
            "binary": str(binary),
            "source": release["html_url"],
            "kind": "upstream-unpatched",
        }
        os.replace(candidate, binary)
        (destination / "runtime.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
