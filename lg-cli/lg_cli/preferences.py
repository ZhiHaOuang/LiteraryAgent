"""Local personal preferences, with a book-owned writing override."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .output_writer import _atomic_text

DETAIL_LEVELS = {'concise': '简洁', 'balanced': '适中', 'detailed': '详细'}


def personal_path() -> Path:
    return Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'literarygiant' / 'preferences.json'


def read_preferences(path: Path | None = None) -> dict:
    try:
        data = json.loads((path or personal_path()).read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if k in {'name', 'detail', 'writing'} and isinstance(v, str)}
    except (FileNotFoundError, ValueError):
        return {}


def save_preferences(values: dict, path: Path | None = None) -> None:
    target = path or personal_path()
    if not all(k in {'name', 'detail', 'writing'} and isinstance(v, str) for k, v in values.items()):
        raise ValueError('无效的个人设置。')
    if values.get('detail', 'balanced') not in DETAIL_LEVELS:
        raise ValueError('无效的回复详略。')
    if len(values.get('name', '')) > 60 or len(values.get('writing', '')) > 16000:
        raise ValueError('称呼最多 60 字，写作偏好最多 16000 字。')
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_text(target, json.dumps(values, ensure_ascii=False, indent=2) + '\n')
    target.chmod(0o600)


def book_path(workspace: Path) -> Path:
    return workspace / '.literarygiant' / 'preferences.json'


def effective_preferences(workspace: Path) -> dict:
    values = {'name': '作者', 'detail': 'balanced', 'writing': '', **read_preferences()}
    local = read_preferences(book_path(workspace))
    if 'writing' in local:
        values['writing'] = local['writing']
    values['writing_scope'] = '本书' if 'writing' in local else '个人默认'
    return values


def preference_instructions(workspace: Path) -> str:
    personal = read_preferences()
    local = read_preferences(book_path(workspace))
    if not personal and not local:
        return ''
    values = effective_preferences(workspace)
    return ('\nAuthor preferences:\nAddress the author as: ' + values['name']
        + '\nReply detail: ' + values['detail'] + '\nWriting preferences: ' + values['writing'])


async def edit_preferences(session, workspace: Path, choose_model) -> None:
    while True:
        values = effective_preferences(workspace)
        try:
            field = await session.ask(kind='choice', title='个人信息与偏好',
                text='个人设置跨书籍共用；本书写作偏好优先。', record=False,
                values=[('name', '用户名／称呼 · ' + values['name']), ('model', '默认模型'),
                    ('detail', '回复详略 · ' + DETAIL_LEVELS.get(values['detail'], '适中')),
                    ('writing', '个人默认写作偏好'), ('book', '本书写作偏好'),
                    ('inherit', '本书恢复使用个人写作偏好'), ('back', '返回')])
        finally:
            session.close_dialog()
        if field in {None, 'back'}:
            return
        if field == 'model':
            await choose_model()
            continue
        if field == 'inherit':
            save_preferences({}, book_path(workspace))
            continue
        personal = read_preferences()
        if field in {'writing', 'book'}:
            old = values['writing'] if field == 'book' else personal.get('writing', '')
            value = await session.edit_text('本书写作偏好' if field == 'book' else '个人写作偏好', old)
        else:
            try:
                value = await session.ask(kind='choice' if field == 'detail' else 'input',
                    title='回复详略' if field == 'detail' else '用户名／称呼', text='修改后保存',
                    values=list(DETAIL_LEVELS.items()), default=values[field], record=False)
            finally:
                session.close_dialog()
        if value is not None:
            if field == 'book':
                save_preferences({'writing': value}, book_path(workspace))
            else:
                personal[field] = value.strip() if field == 'name' else value
                if field == 'name' and not personal[field]:
                    personal[field] = '作者'
                save_preferences(personal)
