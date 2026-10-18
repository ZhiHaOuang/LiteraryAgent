"""Project-owned, append-only dialogue assets."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path


class ConversationStore:
    def __init__(self, workspace: Path):
        self.workspace = workspace
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
            result.append((path.stem, self.title(path.stem, fallback=title)))
        return result

    def title(self, conversation: str, *, fallback: str = '新对话') -> str:
        path = self.path(conversation).with_suffix('.title.json')
        try:
            title = json.loads(path.read_text(encoding='utf-8')).get('title')
            if isinstance(title, str) and title.strip():
                return title
        except (FileNotFoundError, ValueError, AttributeError):
            pass
        return ' '.join(fallback.split())[:40]

    def rename(self, conversation: str, title: str) -> None:
        from .output_writer import _atomic_text
        self.read(conversation)
        title = ' '.join(title.split())
        if not title or len(title) > 80:
            raise ValueError('对话名称需为 1–80 个字符。')
        _atomic_text(self.path(conversation).with_suffix('.title.json'),
            json.dumps({'title': title}, ensure_ascii=False) + '\n')

    def choices(self) -> list[tuple[str, str]]:
        result = []
        for key, title in self.list():
            stamp = datetime.fromtimestamp(self.path(key).stat().st_mtime).strftime('%m-%d %H:%M')
            result.append((key, f'{title}  ·  {stamp}'))
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

    def legacy_items(self,conversation):
        path=self.path(conversation).with_suffix('.legacy.json')
        if not path.exists():return []
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError('Imported history must remain inside conversation storage')
        value=json.loads(path.read_text())
        self._validate_legacy(value)
        return value['items']

    @staticmethod
    def _validate_legacy(value):
        if not isinstance(value,dict) or value.get('schema')!='lg.legacy-history.v1' or not isinstance(value.get('items'),list):
            raise ValueError('Invalid imported history projection')
        source=value.get('source')
        if not isinstance(source,dict) or source.get('kind')!='codex-history' or not isinstance(source.get('threadId'),str):
            raise ValueError('Invalid imported history source')
        seen=set()
        for item in value['items']:
            if not isinstance(item,dict) or set(item)!={'id','runId','kind','title','text','state'} or any(not isinstance(field,str) for field in item.values()):
                raise ValueError('Invalid imported display item')
            if item['kind'] not in {'user','assistant','notice'} or item['state'] not in {'completed','cancelled'} or item['id'] in seen:
                raise ValueError('Invalid imported display state')
            seen.add(item['id'])
        digest=hashlib.sha256(json.dumps({'items':value['items'],'sourceThread':source['threadId']},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        if source.get('sha256')!=digest:raise ValueError('Imported history fingerprint changed')

    def import_history(self,conversation,snapshot):
        from .output_writer import _atomic_text
        from .legacy_sessions import _safe_under
        if not _safe_under(self.root,self.workspace):raise ValueError('Imported history storage cannot be redirected')
        metadata={'schema':'lg.legacy-history.v1','source':snapshot['source'],'items':snapshot['items']}
        self._validate_legacy(metadata)
        dialogue=[(item['kind'],item['text'],item['runId'],item['id']) for item in snapshot['items'] if item['kind'] in {'user','assistant'}]
        records=[(row['role'],row['text'],row['run_id'],row['item_id']) for row in snapshot['records']]
        if records!=dialogue:raise ValueError('Imported dialogue does not match its verified projection')
        self.root.mkdir(parents=True,exist_ok=True)
        path=self.path(conversation)
        if path.is_symlink():raise ValueError('Import target cannot be a symlink')
        lock=path.with_suffix('.import.lock')
        fd=os.open(lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX)
            if path.exists():
                self.read(conversation)
                if not path.with_suffix('.legacy.json').exists():raise ValueError('Import target is an existing independent conversation')
                self.legacy_items(conversation)
                source=json.loads(path.with_suffix('.legacy.json').read_text())['source']
                if any(source.get(key)!=snapshot['source'].get(key) for key in ('home','threadId')):
                    raise ValueError('Imported source identity changed')
                return conversation
            if path.with_suffix('.runtime.json').exists():raise ValueError('Import target has pre-existing runtime permissions')
            sidecar=path.with_suffix('.legacy.json')
            _atomic_text(sidecar,json.dumps(metadata,ensure_ascii=False)+'\n')
            sidecar.chmod(0o600)
            _atomic_text(path.with_suffix('.title.json'),json.dumps({'title':snapshot['title']},ensure_ascii=False)+'\n')
            path.with_suffix('.title.json').chmod(0o600)
            for asset in (sidecar,path.with_suffix('.title.json')):
                with asset.open('rb') as stream:os.fsync(stream.fileno())
            temporary=path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.tmp')
            output=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(output,'w',encoding='utf-8') as stream:
                for record in snapshot['records']:
                    stream.write(json.dumps(record,ensure_ascii=False)+'\n')
                stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,path)
            directory=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(directory)
            finally:os.close(directory)
            return conversation
        finally:os.close(fd)
