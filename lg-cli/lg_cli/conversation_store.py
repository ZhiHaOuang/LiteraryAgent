"""Project-owned, append-only dialogue assets."""

from __future__ import annotations

import fcntl
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path


class ConversationStore:
    def __init__(self, workspace: Path):
        self.root = workspace / ".literarygiant" / "conversations"

    def path(self, conversation: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", conversation):
            raise ValueError("Invalid conversation ID")
        return self.root / f"{conversation}.jsonl"

    def create(self) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        conversation = (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-")
            + uuid.uuid4().hex[:12]
        )
        fd = os.open(
            self.path(conversation), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
        os.close(fd)
        return conversation

    def read(self, conversation: str) -> list[dict]:
        records = []
        with self.path(conversation).open(encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_SH)
            for line in stream:
                record = json.loads(line)
                if not isinstance(record, dict) or record.get("role") not in {
                    "user",
                    "assistant",
                    "error",
                }:
                    raise ValueError(f"Invalid conversation record: {conversation}")
                if not isinstance(record.get("text"), str):
                    raise TypeError(f"Invalid conversation text: {conversation}")
                records.append(record)
        return records

    def append(self, conversation: str, role: str, text: str, **metadata) -> None:
        if role not in {"user", "assistant", "error"}:
            raise ValueError("Invalid conversation role")
        record = {
            **metadata,
            "role": role,
            "text": text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        # Refuse to recreate a missing project/session file.
        with self.path(conversation).open("r+", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.seek(0, os.SEEK_END)
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def list(self) -> list[tuple[str, str]]:
        result = []
        for path in sorted(
            self.root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
        ):
            records = self.read(path.stem)
            title = next(
                (r["text"] for r in records if r["role"] == "user"), "New conversation"
            )
            result.append((path.stem, " ".join(title.split())[:80]))
        return result

    def context(self, conversation: str, limit: int = 10000) -> str:
        parts = []
        remaining = limit
        for record in reversed(self.read(conversation)):
            if record["role"] == "error":
                continue
            prefix = record["role"].upper() + ": "
            if remaining <= len(prefix) + 14:
                break
            text = record["text"]
            if len(text) + len(prefix) > remaining:
                text = "[excerpt] " + text[-(remaining - len(prefix) - 12) :]
            part = prefix + text
            parts.append(part)
            remaining -= len(part) + 2
        return "\n\n".join(reversed(parts))
