#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
from pathlib import Path


MAPPING = {
    "CODEX_CORE_COMMIT": "CODEX_CORE_COMMIT.txt",
    "lg-agent/task_modes.json": "task_modes.json",
    "lg-agent/workflows.json": "workflows.json",
    "lg-skills/skills.json": "skills.json",
    "lg-subagents/subagents.json": "subagents.json",
    "lg-tools/tool_policy.json": "tool_policy.json",
    "lg-context/context_spec.json": "context_spec.json",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync checkout catalogs into the LG wheel package.")
    parser.add_argument("--check", action="store_true", help="Fail when bundled resources are stale.")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    destination = root / "lg-cli" / "lg_cli" / "resources"
    destination.mkdir(parents=True, exist_ok=True)
    stale: list[str] = []
    for source_name, destination_name in MAPPING.items():
        source = root / source_name
        target = destination / destination_name
        if target.exists() and target.read_bytes() == source.read_bytes():
            continue
        stale.append(source_name)
        if not args.check:
            shutil.copyfile(source, target)
    if args.check and stale:
        print("Stale bundled resources:")
        for item in stale:
            print(f"  - {item}")
        return 1
    if not args.check:
        print(f"Synchronized {len(MAPPING)} runtime resource(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
