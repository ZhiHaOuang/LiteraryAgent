from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .project_store import ProjectStore

TOPICS = {
    "characters": "Characters",
    "events": "Events",
    "storylines": "Storylines",
    "timeline": "Timeline",
    "world": "World rules",
    "chapters": "Chapters",
    "analyses": "Book analyses",
    "bible": "Canonical facts",
}


@dataclass(frozen=True)
class BrowseEntry:
    key: str
    title: str
    text: str


def evidence_text(items: list[dict]) -> str:
    return "\n\n".join(
        f"> {item['quote']}\n\nSource: document {item['document_id']} / version {item['version_id']}"
        for item in items
    )


def analysis_text(record: dict) -> str:
    report = record['report']
    lines = [f"# {record['chapter']} / {record['library']}",
             f"Status: {record['status']} | Run: {record['run_id']}",
             "## Summary", report['summary']]
    for entry in report['entries']:
        lines.extend([f"## {entry['label']}", entry['analysis'], evidence_text(entry['evidence'])])
    lines.append("## Review findings")
    decisions = {item['finding_index']: item for item in record.get('adjudications', [])}
    for number, finding in enumerate(report['findings'], 1):
        decision = decisions.get(number)
        lines.extend([f"### {number}. {finding['kind']}" + (" [adjudicated]" if decision else ""),
                      finding['explanation'], evidence_text(finding['evidence'])])
        if finding.get('fact_quote'):
            lines.append(f"Canonical #{finding['fact_id']}: {finding['fact_quote']}")
        if decision:
            lines.append(f"Decision: {decision['reviewer']}\n\n{decision['reason']}")
    if not report['findings']:
        lines.append("No findings.")
    return "\n\n".join(lines)


def project_entries(store: ProjectStore, topic: str) -> list[BrowseEntry]:
    from .book_analysis import BookAnalysisStore

    if topic not in TOPICS:
        raise ValueError(f"Unknown project view: {topic}")
    entries: list[BrowseEntry] = []
    if topic == "analyses":
        for record in [*BookAnalysisStore(store).list(), *BookAnalysisStore(store).list(volume=True)]:
            title = f"{record['chapter']} | {record['library']} | {record['status']}"
            entries.append(BrowseEntry(f"analysis:{record['chapter']}:{record['library']}", title,
                analysis_text(record)))
        return entries
    if topic in {"characters", "events", "world", "storylines", "bible"}:
        terms = {
            "characters": ("character", "cast", "人物", "角色"),
            "events": ("event", "事件"),
            "world": ("world", "rule", "世界", "规则", "设定"),
            "storylines": ("plot", "storyline", "thread", "主线", "支线", "故事线"),
            "bible": (),
        }[topic]
        for fact in store.list_facts(state="canonical", limit=1000):
            category = " ".join((fact.category, *fact.tags)).lower()
            if topic == "bible" or any(term in category for term in terms):
                source = f"document {fact.source_document_id}, version {fact.source_version_id}"
                entries.append(BrowseEntry(f"fact:{fact.id}", fact.key,
                    f"# {fact.key}\n\nStatus: Canonical | Category: {fact.category}\n\n{fact.value}\n\nSource: {source}"))
    if topic in {"events", "timeline"}:
        for entry in store.list_timeline(state="canonical", limit=1000):
            entries.append(BrowseEntry(f"timeline:{entry.id}", entry.label,
                f"# {entry.label}\n\nOrder: {entry.sort_key} | Status: Canonical\n\n{entry.event}\n\nSource: {entry.source_document_id}"))
    if topic == "timeline":
        for chapter in store.list_documents(kind="chapter", limit=1000):
            for scene in store.list_scenes(chapter=chapter.id, limit=1000):
                if not scene.time_label:
                    continue
                title = f"{scene.time_label} | {scene.document.title} [{scene.status}]"
                entries.append(BrowseEntry(f"scene-time:{scene.document.id}", title,
                    f"{title}\n\nScene-card schedule, not an inferred Canonical event.\n"
                    f"Goal: {scene.goal}\nEnd state (planned): {scene.end_state}\n"
                    f"Source: {scene.document.slug} v{scene.document.active_version_number}"))
    if topic == "storylines":
        for thread in store.list_foreshadowing(limit=1000):
            entries.append(BrowseEntry(f"thread:{thread.id}", thread.title,
                f"# {thread.title}\n\nStatus: {thread.state}\n\n## Setup\n\n{thread.setup_note}\n\n## Payoff\n\n{thread.payoff_note}"))
    if topic in {"characters", "world", "storylines", "chapters"}:
        kind = {"characters": "character", "world": "world", "storylines": "outline", "chapters": "chapter"}[topic]
        for doc in store.list_documents(kind=kind, limit=1000):
            if doc.state == "archived":
                continue
            content = doc.content
            if topic == "chapters":
                content = "\n\n".join([content, *[
                    scene.document.content for scene in store.list_scenes(chapter=doc.id, limit=1000)
                ]]).strip()
            entries.append(BrowseEntry(f"document:{doc.id}", doc.title,
                f"# {doc.title}\n\nStatus: {doc.state} | {doc.slug} v{doc.active_version_number}\n\n{content}"))
    category = {"characters": "CharacterArc", "events": "EventsLibrary", "world": "Worldview"}.get(topic)
    if category:
        for record in BookAnalysisStore(store).list():
            if record["status"] != "current" or record["library"] != category:
                continue
            for item in record["report"]["entries"]:
                title = f"{item['label']} [{record['chapter']} analysis]"
                entries.append(BrowseEntry(f"analysis:{record['chapter']}:{category}:{item['key']}", title,
                    "# " + title + "\n\n" + item["analysis"] + "\n\n## Evidence\n\n"
                    + evidence_text(item["evidence"])))
    return entries


async def browse_project(session, store: ProjectStore, topic: str | None = None) -> None:
    selected_topic = topic
    snapshots: dict[str, list[BrowseEntry]] = {}
    while True:
        if selected_topic is None:
            try:
                selected_topic = await session.ask(kind="choice", title=store.project_info().name,
                    text="Browse project", values=list(TOPICS.items()), record=False)
            finally:
                session.close_dialog()
            if selected_topic is None:
                return
        if selected_topic not in snapshots:
            snapshots[selected_topic] = await asyncio.to_thread(project_entries, store, selected_topic)
        entries = snapshots[selected_topic]
        try:
            selected = await session.ask(kind="choice", title=TOPICS[selected_topic],
                text=f"{store.project_info().name} | {len(entries)} entries",
                values=[(entry.key, entry.title) for entry in entries] + [("back", "Back")], record=False)
        finally:
            session.close_dialog()
        if selected is None or selected == "back":
            if topic is not None:
                return
            selected_topic = None
            continue
        entry = next(entry for entry in entries if entry.key == selected)
        await session.view_text(entry.title, entry.text)
