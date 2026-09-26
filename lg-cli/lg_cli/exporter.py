from __future__ import annotations

import os
import re
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from .project_store import DocumentRecord, Foreshadowing, ProjectStore, StoryFact, TimelineEntry


EXPORT_TARGETS = {"manuscript", "bible", "timeline", "characters", "world", "foreshadowing"}
EXPORT_FORMATS = {"md", "txt", "docx"}


@dataclass(frozen=True)
class ExportResult:
    target: str
    format: str
    path: Path
    document_count: int


def export_project(
    store: ProjectStore,
    *,
    target: str,
    format: str = "md",
    output_path: Path | None = None,
    include_drafts: bool = False,
) -> ExportResult:
    normalized_target = target.strip().lower()
    normalized_format = format.strip().lower()
    if normalized_target not in EXPORT_TARGETS:
        raise ValueError(f"Export target must be one of: {', '.join(sorted(EXPORT_TARGETS))}.")
    if normalized_format not in EXPORT_FORMATS:
        raise ValueError(f"Export format must be one of: {', '.join(sorted(EXPORT_FORMATS))}.")
    markdown, document_count = render_export(store, target=normalized_target, include_drafts=include_drafts)
    path = output_path or _default_output_path(store, normalized_target, normalized_format)
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if normalized_format == "md":
        _atomic_write(path, markdown.rstrip() + "\n")
    elif normalized_format == "txt":
        _atomic_write(path, markdown_to_text(markdown).rstrip() + "\n")
    else:
        write_docx(path, markdown)
    return ExportResult(normalized_target, normalized_format, path, document_count)


def render_export(store: ProjectStore, *, target: str, include_drafts: bool = False) -> tuple[str, int]:
    project = store.project_info()
    if target == "manuscript":
        return _render_manuscript(store, project.name, include_drafts=include_drafts)
    if target == "bible":
        facts = store.list_facts(state=None if include_drafts else "canonical", limit=1000)
        return _render_bible(project.name, facts), len(facts)
    if target == "timeline":
        entries = store.list_timeline(state=None if include_drafts else "canonical", limit=1000)
        return _render_timeline(project.name, entries), len(entries)
    if target == "characters":
        facts = [fact for fact in store.list_facts(state=None if include_drafts else "canonical", limit=1000) if _is_character_fact(fact)]
        return _render_bible(project.name, facts, heading="Characters"), len(facts)
    if target == "world":
        facts = [fact for fact in store.list_facts(state=None if include_drafts else "canonical", limit=1000) if _is_world_fact(fact)]
        return _render_bible(project.name, facts, heading="World Bible"), len(facts)
    items = store.list_foreshadowing(limit=1000)
    return _render_foreshadowing(project.name, items), len(items)


def markdown_to_text(markdown: str) -> str:
    output: list[str] = []
    for line in markdown.splitlines():
        cleaned = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)
        cleaned = re.sub(r"^\s*[-*+]\s+", "- ", cleaned)
        cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
        cleaned = re.sub(r"\*\*([^*]+)\*\*", r"\1", cleaned)
        cleaned = re.sub(r"\*([^*]+)\*", r"\1", cleaned)
        output.append(cleaned)
    return "\n".join(output)


def write_docx(path: Path, markdown: str) -> None:
    """Write a minimal Office Open XML document without a third-party dependency."""
    paragraphs = _docx_paragraphs(markdown)
    document_xml = _document_xml(paragraphs)
    files = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "_rels/.rels": _ROOT_RELS,
        "docProps/core.xml": _core_properties(),
        "docProps/app.xml": _APP_PROPERTIES,
        "word/document.xml": document_xml,
        "word/styles.xml": _STYLES,
        "word/_rels/document.xml.rels": _DOCUMENT_RELS,
    }
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content.encode("utf-8"))
    os.replace(temporary, path)


def _render_manuscript(store: ProjectStore, project_name: str, *, include_drafts: bool) -> tuple[str, int]:
    chapters = store.list_documents(kind="chapter", limit=1000)
    lines = [f"# {project_name}", ""]
    count = 0
    for chapter in chapters:
        scenes = store.list_scenes(chapter=chapter.id, limit=1000)
        accepted_scenes = [
            scene
            for scene in scenes
            if include_drafts or scene.status in {"accepted", "final"}
        ]
        chapter_text = chapter.content.strip() if include_drafts or chapter.state in {"accepted", "final"} else ""
        if not chapter_text and not accepted_scenes:
            continue
        count += 1
        lines.extend([f"# {chapter.title}", ""])
        if chapter_text:
            lines.extend([chapter_text, ""])
        for scene in accepted_scenes:
            if scene.document.content.strip():
                lines.extend([f"## {scene.document.title}", "", scene.document.content.strip(), ""])
                count += 1
    if count == 0:
        lines.extend(["_No accepted manuscript content yet._", ""])
    return "\n".join(lines).rstrip() + "\n", count


def _render_bible(project_name: str, facts: list[StoryFact], *, heading: str = "Story Bible") -> str:
    lines = [f"# {project_name} - {heading}", ""]
    if not facts:
        return "\n".join([*lines, "_No facts recorded yet._", ""])
    grouped: dict[str, list[StoryFact]] = {}
    for fact in facts:
        grouped.setdefault(fact.category, []).append(fact)
    for category, entries in sorted(grouped.items()):
        lines.extend([f"## {category}", ""])
        for fact in entries:
            tags = f"  _[{', '.join(fact.tags)}]_" if fact.tags else ""
            lines.append(f"- **{fact.key}**: {fact.value}{tags}")
            if fact.rationale:
                lines.append(f"  - Note: {fact.rationale}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_timeline(project_name: str, entries: list[TimelineEntry]) -> str:
    lines = [f"# {project_name} - Timeline", ""]
    if not entries:
        return "\n".join([*lines, "_No timeline entries recorded yet._", ""])
    for entry in entries:
        label = f"{entry.sort_key} - {entry.label}" if entry.sort_key else entry.label
        tags = f"  _[{', '.join(entry.tags)}]_" if entry.tags else ""
        lines.extend([f"## {label}", "", f"{entry.event}{tags}", ""])
    return "\n".join(lines).rstrip() + "\n"


def _render_foreshadowing(project_name: str, items: list[Foreshadowing]) -> str:
    lines = [f"# {project_name} - Foreshadowing Ledger", ""]
    if not items:
        return "\n".join([*lines, "_No foreshadowing recorded yet._", ""])
    for item in items:
        lines.extend([f"## {item.title} ({item.state})", "", f"- Setup: {item.setup_note}"])
        if item.payoff_note:
            lines.append(f"- Payoff: {item.payoff_note}")
        if item.tags:
            lines.append(f"- Tags: {', '.join(item.tags)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _is_character_fact(fact: StoryFact) -> bool:
    category = fact.category.lower()
    return category.startswith("character") or category in {"relationship", "arc", "voice"}


def _is_world_fact(fact: StoryFact) -> bool:
    category = fact.category.lower()
    return category.startswith("world") or category in {"location", "faction", "rule", "history", "resource"}


def _default_output_path(store: ProjectStore, target: str, format: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return store.workspace / "exports" / f"{target}-{stamp}.{format}"


def _atomic_write(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _docx_paragraphs(markdown: str) -> list[tuple[str, str]]:
    paragraphs: list[tuple[str, str]] = []
    for line in markdown.splitlines():
        if not line.strip():
            continue
        match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if match:
            level = min(len(match.group(1)), 3)
            paragraphs.append((f"Heading{level}", match.group(2)))
        else:
            paragraphs.append(("Normal", re.sub(r"^[-*+]\s+", "- ", markdown_to_text(line))))
    return paragraphs or [("Normal", "")]


def _document_xml(paragraphs: list[tuple[str, str]]) -> str:
    body: list[str] = []
    for style, text in paragraphs:
        body.append(
            "<w:p><w:pPr><w:pStyle w:val=\""
            + escape(style)
            + "\"/></w:pPr><w:r><w:t xml:space=\"preserve\">"
            + escape(text)
            + "</w:t></w:r></w:p>"
        )
    body.append(
        "<w:sectPr><w:pgSz w:w=\"11906\" w:h=\"16838\"/><w:pgMar w:top=\"1440\" "
        "w:right=\"1440\" w:bottom=\"1440\" w:left=\"1440\"/></w:sectPr>"
    )
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">"
        "<w:body>"
        + "".join(body)
        + "</w:body></w:document>"
    )


def _core_properties() -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:creator>LiteraryGiant</dc:creator><dc:title>LiteraryGiant Export</dc:title><dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created><dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified></cp:coreProperties>"""


_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/><Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/><Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/></Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/></Relationships>"""

_DOCUMENT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>"""

_APP_PROPERTIES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><Application>LiteraryGiant</Application></Properties>"""

_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style><w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:rPr><w:b/><w:sz w:val="32"/></w:rPr></w:style><w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style><w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/><w:rPr><w:b/><w:sz w:val="24"/></w:rPr></w:style></w:styles>"""
