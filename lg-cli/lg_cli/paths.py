from __future__ import annotations

import os
from pathlib import Path


def product_root() -> Path:
    configured = os.environ.get("LITERARYGIANT_HOME") or os.environ.get("LG_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "CODEX_CORE_COMMIT").exists() or (candidate / "lg-agent").exists():
        return candidate
    return candidate


def core_codex_root() -> Path:
    configured = os.environ.get("LG_CODEX_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return product_root() / "core" / "codex"


def data_file(*parts: str) -> Path:
    return product_root().joinpath(*parts)
