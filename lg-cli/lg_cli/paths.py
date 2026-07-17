from __future__ import annotations

from pathlib import Path


def product_root() -> Path:
    return Path(__file__).resolve().parents[2]


def core_codex_root() -> Path:
    return product_root() / "core" / "codex"


def data_file(*parts: str) -> Path:
    return product_root().joinpath(*parts)
