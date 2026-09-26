"""Chapter reading and explicitly confirmed author edits, without a model call."""
from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass

from .chapter_sync import ChapterSync
from .output_writer import write_workflow_output
from .project_store import DocumentRecord, ProjectStore, ProjectStoreError


@dataclass(frozen=True)
class ChapterDraft:
    project_id: str
    chapter: str
    documents: tuple[DocumentRecord, ...]


def read_chapter(store: ProjectStore, reference: str) -> ChapterDraft:
    chapter = store.get_document(reference, kind="chapter")
    scenes = store.list_documents(kind="scene", parent=chapter.id, limit=1000)
    if len(scenes) >= 1000:
        raise ProjectStoreError("Chapter exceeds the editor limit; refusing a partial edit.")
    return ChapterDraft(store.project_info().project_id, chapter.slug, (chapter, *scenes))


def save_part(store: ProjectStore, draft: ChapterDraft, document_id: int, text: str) -> str:
    if draft.project_id != store.project_info().project_id:
        raise ProjectStoreError("This edit belongs to another book.")
    if document_id not in {doc.id for doc in draft.documents}:
        raise ProjectStoreError("This document is not part of the chapter snapshot.")
    versions = store.edit_chapter_documents(
        draft.chapter,
        expected_versions={doc.id: doc.active_version_id for doc in draft.documents},
        contents={doc.id: text if doc.id == document_id else doc.content for doc in draft.documents},
        reason="Author confirmed edits in the novel editor", origin="novel-editor",
    )
    if not versions:
        return "没有内容变化，未创建新版本。"
    message = "已保存回数据库主文本，修改前版本已保留。"
    try:
        ChapterSync(store).export(draft.chapter)
    except (OSError, ValueError) as exc:
        message += f"\n\n章节文件未同步：{exc}\n外部文件修改未被覆盖，可用 chapter diff/import 处理。"
    return message


async def _choose(session, title: str, text: str, values):
    try:
        return await session.ask(kind="choice", title=title, text=text, values=values, record=False)
    finally:
        session.close_dialog()


async def _edit_part(session, store: ProjectStore, draft: ChapterDraft, doc: DocumentRecord) -> None:
    edited = doc.content
    while True:
        edited = await session.edit_text(f"编辑 · {doc.title}", edited)
        if edited == doc.content:
            return
        description = f"{doc.title} · {len(doc.content)} → {len(edited)} 字符"
        if not edited.strip():
            description += " · 该部分正文将被清空"
        choice = await _choose(session, "保存章节修改？", description,
            [("save", "保存回主文本（保留旧版本）"), ("continue", "继续编辑"), ("discard", "放弃修改并返回")])
        if choice == "discard":
            return
        if choice != "save":
            continue
        try:
            message = await asyncio.to_thread(save_part, store, draft, doc.id, edited)
        except (OSError, ProjectStoreError, sqlite3.Error) as exc:
            choice = await _choose(session, "未保存，主文本未被覆盖", str(exc),
                [("continue", "继续编辑"), ("draft", "另存草稿并返回"), ("discard", "放弃修改并返回")])
            if choice == "discard":
                return
            if choice == "draft":
                try:
                    result = write_workflow_output(store.workspace, "candidate", edited,
                        run_id=f"editor-{doc.id}-v{doc.active_version_number}")
                except (OSError, ValueError) as error:
                    await session.view_text("草稿未能保存，编辑内容仍保留", str(error))
                    continue
                await session.view_text("草稿已保留", f"未替换主文本。\n\n{result.output_path}")
                return
            continue
        await session.view_text("保存完成", message)
        return


async def browse_novel(session, store: ProjectStore, reference: str | None = None) -> None:
    while True:
        if reference is None:
            chapters = [doc for doc in store.list_documents(kind="chapter", limit=1000) if doc.state != "archived"]
            selected = await _choose(session, store.project_info().name, f"小说正文 · {len(chapters)} 章",
                [(doc.slug, f"{index}. {doc.title}") for index, doc in enumerate(chapters, 1)] + [(None, "返回对话")])
            if selected is None:
                return
        else:
            selected = reference
        while True:
            draft = await asyncio.to_thread(read_chapter, store, selected)
            chapter = draft.documents[0]
            content = "\n\n".join(doc.content for doc in draft.documents if doc.content)
            action = await session.view_text(chapter.title, content or "（本章暂无正文）", allow_edit=True)
            if action != "edit":
                break
            editable = [doc for doc in draft.documents if doc.kind == "scene" or doc.content]
            if not editable:
                editable = [chapter]
            if len(editable) == 1:
                doc = editable[0]
            else:
                part = await _choose(session, chapter.title, "选择要编辑的部分",
                    [(doc.id, doc.title + (" · 章前正文" if doc.kind == "chapter" else "")) for doc in editable])
                if part is None:
                    continue
                doc = next(doc for doc in editable if doc.id == part)
            await _edit_part(session, store, draft, doc)
        if reference is not None:
            return
