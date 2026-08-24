from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from .project_store import DocumentRecord, ProjectStore, ReviewIssue, SceneCard


@dataclass(frozen=True)
class ConsistencyReport:
    document: DocumentRecord | None
    issues: tuple[ReviewIssue, ...]
    checked_documents: int
    checked_facts: int

    @property
    def errors(self) -> int:
        return sum(issue.severity == "error" for issue in self.issues)

    @property
    def warnings(self) -> int:
        return sum(issue.severity == "warning" for issue in self.issues)


@dataclass(frozen=True)
class ChapterQualityReport:
    document: DocumentRecord
    word_count: int
    paragraph_count: int
    dialogue_ratio: float
    ending_hook_signal: bool
    scene_count: int
    accepted_scene_count: int
    issues: tuple[ReviewIssue, ...]


def run_consistency_check(
    store: ProjectStore,
    *,
    document: str | int | None = None,
    persist: bool = True,
) -> ConsistencyReport:
    target = store.get_document(document) if document is not None else None
    documents = [target] if target is not None else store.list_documents(limit=1000)
    facts = store.list_facts(state="canonical", limit=1000)
    issues: list[dict[str, Any]] = []

    issues.extend(_duplicate_canonical_fact_issues(facts))
    issues.extend(_scene_card_issues(store, target, documents))
    issues.extend(_forbidden_content_issues(facts, documents))
    issues.extend(_character_availability_issues(facts, documents))
    issues.extend(_timeline_issues(store, target))
    issues.extend(_foreshadowing_issues(store, target))

    reported = (
        tuple(store.replace_review_issues(document=document, issues=issues))
        if persist
        else _transient_issues(issues, target.id if target is not None else None)
    )
    return ConsistencyReport(
        document=target,
        issues=reported,
        checked_documents=len(documents),
        checked_facts=len(facts),
    )


def chapter_quality_report(store: ProjectStore, chapter: str | int, *, persist: bool = True) -> ChapterQualityReport:
    document = store.get_document(chapter, kind="chapter")
    scenes = store.list_scenes(chapter=document.id)
    accepted = [scene for scene in scenes if scene.status in {"accepted", "final"}]
    text = _chapter_text(document, scenes)
    word_count = _word_count(text)
    paragraphs = [line.strip() for line in text.splitlines() if line.strip()]
    dialogue_chars = sum(len(match.group(0)) for match in re.finditer(r"[\"“][^\"”]{1,300}[\"”]", text))
    dialogue_ratio = round(dialogue_chars / max(len(text), 1), 3)
    ending = next((line for line in reversed(paragraphs) if line), "")
    hook = _has_hook(ending)
    issues: list[dict[str, Any]] = []
    if scenes and not accepted:
        issues.append(
            _issue(
                "warning",
                "workflow",
                "本章已有场景卡，但尚无已接受的场景正文。",
                "所有场景仍处于计划、草稿或候选状态。",
                "接受一个经过确认的场景版本，或继续保留本章为规划状态。",
            )
        )
    if word_count < 200 and (document.content.strip() or accepted):
        issues.append(
            _issue(
                "warning",
                "length",
                "本章正文较短，可能还没有形成完整的场景推进。",
                f"当前字数估计为 {word_count}。",
                "检查本章是否包含目标、阻力、变化和结尾状态。",
            )
        )
    if text.strip() and not hook:
        issues.append(
            _issue(
                "info",
                "hook",
                "章节结尾没有检测到明显的悬念或状态转折信号。",
                ending[-180:] or "正文为空。",
                "确认这是否是刻意的平缓收束；若不是，为下一章留下问题、代价、揭示或未完成行动。",
            )
        )
    reported = (
        tuple(store.replace_review_issues(document=document.id, issues=issues))
        if persist
        else _transient_issues(issues, document.id)
    )
    return ChapterQualityReport(
        document=document,
        word_count=word_count,
        paragraph_count=len(paragraphs),
        dialogue_ratio=dialogue_ratio,
        ending_hook_signal=hook,
        scene_count=len(scenes),
        accepted_scene_count=len(accepted),
        issues=reported,
    )


def _transient_issues(issues: Iterable[dict[str, Any]], document_id: int | None) -> tuple[ReviewIssue, ...]:
    return tuple(
        ReviewIssue(
            id=0,
            document_id=document_id,
            severity=str(issue.get("severity") or "warning"),
            category=str(issue.get("category") or "general"),
            message=str(issue.get("message") or ""),
            evidence=str(issue.get("evidence") or ""),
            suggested_actions=tuple(str(item) for item in issue.get("suggested_actions", [])),
            status="transient",
            created_at="",
        )
        for issue in issues
    )


def _duplicate_canonical_fact_issues(facts: Iterable[Any]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[Any]] = {}
    for fact in facts:
        groups.setdefault((fact.category.lower(), fact.key.lower()), []).append(fact)
    issues: list[dict[str, Any]] = []
    for (category, key), items in groups.items():
        values = {item.value.strip() for item in items}
        if len(values) <= 1:
            continue
        issues.append(
            _issue(
                "error",
                "canonical-conflict",
                f"Story Bible 中 `{category}/{key}` 存在多个互相冲突的 Canonical facts。",
                " | ".join(sorted(values)),
                "保留一个事实，将其余事实归档或改成 Ideas；不要让 Agent 自行选择。",
            )
        )
    return issues


def _scene_card_issues(
    store: ProjectStore,
    target: DocumentRecord | None,
    documents: Iterable[DocumentRecord],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    scenes: list[SceneCard] = []
    if target is not None and target.kind == "scene":
        scenes = [store.get_scene(target.id)]
    elif target is not None and target.kind == "chapter":
        scenes = store.list_scenes(chapter=target.id)
    elif target is None:
        scenes = store.list_scenes(limit=1000)
    else:
        scenes = [store.get_scene(document.id) for document in documents if document.kind == "scene"]

    for scene in scenes:
        if scene.status in {"approved", "drafted", "reviewed"} and not scene.plan.strip():
            issues.append(
                _issue(
                    "error",
                    "scene-plan",
                    f"场景 `{scene.document.slug}` 已进入 {scene.status} 状态，但没有已确认的场景计划。",
                    "scene card.plan is empty",
                    "补充场景目标、冲突、信息揭示、情绪变化和结尾状态，再继续写作。",
                )
            )
        if scene.status in {"approved", "drafted", "reviewed"} and scene.plan_state != "approved":
            issues.append(
                _issue(
                    "warning",
                    "scene-plan",
                    f"场景 `{scene.document.slug}` 尚未确认计划，但状态已是 {scene.status}。",
                    f"plan_state={scene.plan_state}",
                    "在写正文前执行 `literary scene approve <scene>`，或把场景退回规划阶段。",
                )
            )
        count = _word_count(scene.document.content)
        if scene.status in {"accepted", "final"} and scene.word_min is not None and count < scene.word_min:
            issues.append(
                _issue(
                    "warning",
                    "scene-length",
                    f"场景 `{scene.document.slug}` 的已接受正文低于场景卡字数下限。",
                    f"预计 {count} 字，目标至少 {scene.word_min} 字。",
                    "确认这是刻意的短场景，或补足冲突、反应和状态变化。",
                )
            )
        if scene.status in {"accepted", "final"} and scene.word_max is not None and count > scene.word_max:
            issues.append(
                _issue(
                    "warning",
                    "scene-length",
                    f"场景 `{scene.document.slug}` 的已接受正文超过场景卡字数上限。",
                    f"预计 {count} 字，目标最多 {scene.word_max} 字。",
                    "检查是否应拆分场景，或调整已确认的字数范围。",
                )
            )
    return issues


def _forbidden_content_issues(facts: Iterable[Any], documents: Iterable[DocumentRecord]) -> list[dict[str, Any]]:
    prohibitions = [fact for fact in facts if fact.category.lower() in {"forbidden", "do-not-change", "constraint"}]
    if not prohibitions:
        return []
    issues: list[dict[str, Any]] = []
    for document in documents:
        content = document.content.lower()
        if not content:
            continue
        for fact in prohibitions:
            needle = fact.value.strip().lower()
            if len(needle) < 3 or needle not in content:
                continue
            issues.append(
                _issue(
                    "warning",
                    "forbidden-content",
                    f"`{document.slug}` 可能触及不可变约束 `{fact.key}`。",
                    fact.value,
                    "确认这段文字是否只是讨论、回忆或否定；若是正式设定变化，先由作者更新 Canonical facts。",
                )
            )
    return issues


def _character_availability_issues(facts: Iterable[Any], documents: Iterable[DocumentRecord]) -> list[dict[str, Any]]:
    unavailable: list[Any] = []
    for fact in facts:
        if fact.category.lower() not in {"character-status", "character"}:
            continue
        value = fact.value.lower()
        tags = {tag.lower() for tag in fact.tags}
        if value in {"dead", "deceased", "missing", "absent", "离场", "死亡", "失踪"} or "dead" in tags or "deceased" in tags:
            unavailable.append(fact)
    issues: list[dict[str, Any]] = []
    for document in documents:
        content = document.content
        if not content:
            continue
        for fact in unavailable:
            if fact.key and fact.key in content:
                issues.append(
                    _issue(
                        "warning",
                        "character-availability",
                        f"`{document.slug}` 提到当前状态为 `{fact.value}` 的角色 `{fact.key}`。",
                        f"Canonical fact {fact.id}: {fact.category}/{fact.key} = {fact.value}",
                        "确认是否为回忆、幻觉、转述或时间线早于该状态；否则修改正文或 Canonical fact。",
                    )
                )
    return issues


def _timeline_issues(store: ProjectStore, target: DocumentRecord | None) -> list[dict[str, Any]]:
    if target is not None and target.kind not in {"scene", "chapter"}:
        return []
    entries = store.list_timeline(state="canonical", limit=1000)
    seen: dict[str, set[str]] = {}
    issues: list[dict[str, Any]] = []
    for entry in entries:
        if not entry.sort_key:
            continue
        bucket = seen.setdefault(entry.sort_key, set())
        normalized = entry.event.strip()
        if bucket and normalized not in bucket:
            issues.append(
                _issue(
                    "warning",
                    "timeline",
                    f"时间线排序键 `{entry.sort_key}` 关联了多个不同事件。",
                    " | ".join(sorted({*bucket, normalized})),
                    "确认这些事件是否应同时发生；若不是，调整 sort_key 或补充更精细的时间标签。",
                )
            )
        bucket.add(normalized)
    return issues


def _foreshadowing_issues(store: ProjectStore, target: DocumentRecord | None) -> list[dict[str, Any]]:
    if target is not None:
        return []
    open_items = [
        item
        for item in store.list_foreshadowing(limit=1000)
        if item.state in {"open", "planned"}
    ]
    if len(open_items) <= 12:
        return []
    return [
        _issue(
            "info",
            "foreshadowing",
            f"当前有 {len(open_items)} 条未回收伏笔。",
            ", ".join(item.title for item in open_items[:12]),
            "在下一次大纲或章节规划中，决定哪些伏笔继续、回收或废弃。",
        )
    ]


def _chapter_text(document: DocumentRecord, scenes: Iterable[SceneCard]) -> str:
    parts = [document.content.strip()]
    parts.extend(scene.document.content.strip() for scene in scenes if scene.status in {"accepted", "final"})
    return "\n\n".join(part for part in parts if part)


def _word_count(text: str) -> int:
    latin = re.findall(r"[A-Za-z0-9]+(?:['-][A-Za-z0-9]+)?", text)
    chinese = re.findall(r"[\u4e00-\u9fff]", text)
    return len(latin) + len(chinese)


def _has_hook(text: str) -> bool:
    lowered = text.lower()
    return any(marker in text for marker in ("？", "！", "?", "!", "却", "然而", "未完", "门外")) or any(
        marker in lowered for marker in ("but", "however", "suddenly", "unknown")
    )


def _issue(
    severity: str,
    category: str,
    message: str,
    evidence: str,
    *actions: str,
) -> dict[str, Any]:
    return {
        "severity": severity,
        "category": category,
        "message": message,
        "evidence": evidence,
        "suggested_actions": list(actions),
    }
