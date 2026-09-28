"""Read and version author edits to existing outline documents."""
import asyncio
import sqlite3
import fcntl
import os
import uuid
from pathlib import Path
from .output_writer import _atomic_text
from .novel_editor import _choose
from .project_store import ProjectStoreError


async def browse_outlines(session, store):
    while True:
        documents = [doc for doc in store.list_documents(kind='outline', limit=1000) if doc.state != 'archived']
        files = outline_files(store.workspace)
        selected = await _choose(session, '已有大纲', f'{len(documents) + len(files)} 份 · 阅读或修改，不生成新大纲',
            [(doc.id, doc.title + ' · 数据库') for doc in documents]
            + [(str(path), path.name + ' · 文件') for path in files] + [(None, '返回对话')])
        if selected is None:
            return
        if isinstance(selected, str):
            await edit_outline_file(session, store.workspace, Path(selected))
            continue
        while True:
            doc = store.get_document(selected, kind='outline')
            if await session.view_text(doc.title, doc.content or '（大纲暂无内容）', allow_edit=True) != 'edit':
                break
            edited = doc.content
            while True:
                edited = await session.edit_text('编辑大纲 · ' + doc.title, edited)
                if edited == doc.content:
                    break
                choice = await _choose(session, '保存大纲修改？', '保存为新版本，保留原有版本。',
                    [('save', '保存'), ('continue', '继续编辑'), ('discard', '放弃本次修改')])
                if choice == 'discard':
                    break
                if choice != 'save':
                    continue
                try:
                    await asyncio.to_thread(store.edit_active_document, doc.id, content=edited,
                        expected_version_id=doc.active_version_id, reason='Author confirmed outline editor changes')
                except (ProjectStoreError, sqlite3.Error, OSError) as exc:
                    await session.view_text('未保存，编辑内容仍保留', str(exc))
                    continue
                session.append(f'已保存大纲：{doc.title}，旧版本已保留。', role='assistant')
                break


def outline_files(workspace):
    candidates = set((workspace / 'ReferenceLibrary/plans/outlines').glob('*.md'))
    for directory in (workspace / '.literarygiant/output', workspace / 'ReferenceLibrary/drafts'):
        candidates.update(directory.glob('outline*.md'))
    return sorted((p for p in candidates if p.is_file() and not p.is_symlink()
        and p.resolve().is_relative_to(workspace.resolve())),
        key=lambda p: (not p.name.endswith('.latest.md'), p.name))


def save_outline_file(workspace, path, original, edited):
    if path not in outline_files(workspace):
        raise ValueError('大纲文件不在当前书籍中。')
    directory = workspace / '.literarygiant/editor-history/outlines'
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.resolve().is_relative_to(workspace.resolve()):
        raise ValueError('大纲历史目录不在当前书籍中。')
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if path.read_text(encoding='utf-8') != original:
            raise ValueError('大纲已被外部修改。当前编辑仍保留，请返回后重新读取再合并。')
        _atomic_text(directory / (path.stem + '-' + uuid.uuid4().hex + '.md'), original)
        _atomic_text(path, edited)
    finally:
        os.close(fd)


async def edit_outline_file(session, workspace, path):
    while True:
        if path not in outline_files(workspace):
            raise ValueError('大纲文件已移动或不在当前书籍中。')
        original = path.read_text(encoding='utf-8')
        if await session.view_text(path.name, original, allow_edit=True) != 'edit':
            return
        edited = original
        while True:
            edited = await session.edit_text('编辑大纲 · ' + path.name, edited)
            if edited == original:
                break
            choice = await _choose(session, '保存大纲文件？', '保留原文历史副本，再保存当前文件。',
                [('save', '保存'), ('continue', '继续编辑'), ('discard', '放弃本次修改')])
            if choice == 'discard':
                break
            if choice != 'save':
                continue
            try:
                await asyncio.to_thread(save_outline_file, workspace, path, original, edited)
            except (OSError, ValueError) as exc:
                await session.view_text('未保存，编辑内容仍保留', str(exc))
                continue
            session.append('已保存大纲文件：' + path.name + '，原文副本已保留。', role='assistant')
            break
