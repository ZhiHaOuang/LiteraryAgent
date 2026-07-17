from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


SUPPORTED_EXTENSIONS = {".md", ".txt", ".json", ".jsonl", ".yaml", ".yml"}
DOMAIN_TERMS = [
    "都市",
    "重生",
    "爽文",
    "网恋",
    "修罗场",
    "盗图",
    "妹妹",
    "女生",
    "女主",
    "男主",
    "掉马",
    "误会",
    "反击",
    "打脸",
    "复仇",
    "系统",
    "异能",
    "世界观",
    "大纲",
]
CHINESE_STOP_TERMS = {
    "写一",
    "一个",
    "多个",
    "个女",
    "个男",
    "这个",
    "那个",
    "需要",
    "设计",
    "帮我",
    "可以",
    "作为",
    "进行",
    "整体",
}


@dataclass(frozen=True)
class ReferenceResult:
    path: Path
    score: int
    matched_terms: list[str]
    snippet: str


@dataclass(frozen=True)
class ReferenceContext:
    results: list[ReferenceResult]
    warnings: list[str]
    roots_checked: list[Path]
    files_scanned: int

    @property
    def found_count(self) -> int:
        return len(self.results)

    def to_prompt_text(self) -> str:
        if not self.results:
            base = "No reference snippets were selected."
        else:
            chunks: list[str] = []
            for index, result in enumerate(self.results, start=1):
                terms = ", ".join(result.matched_terms) or "path/content match"
                chunks.append(
                    f"### Reference {index}: {result.path}\n"
                    f"- score: {result.score}\n"
                    f"- matched_terms: {terms}\n"
                    f"- snippet:\n{result.snippet.strip()}"
                )
            base = "\n\n".join(chunks)
        if self.warnings:
            base += "\n\n### Reference Warnings\n" + "\n".join(f"- {warning}" for warning in self.warnings)
        return base

    def to_metadata(self) -> dict[str, object]:
        return {
            "roots_checked": [str(path) for path in self.roots_checked],
            "files_scanned": self.files_scanned,
            "results": [
                {
                    "path": str(item.path),
                    "score": item.score,
                    "matched_terms": item.matched_terms,
                    "snippet_chars": len(item.snippet),
                }
                for item in self.results
            ],
            "warnings": self.warnings,
        }


def search_references(
    workspace: Path,
    query: str,
    *,
    top_k: int = 6,
    max_files_per_root: int = 300,
    max_file_chars: int = 120000,
    max_snippet_chars: int = 900,
) -> ReferenceContext:
    terms = _query_terms(query)
    roots = _candidate_roots(workspace)
    warnings: list[str] = []
    roots_checked: list[Path] = []
    scored: list[ReferenceResult] = []
    files_scanned = 0
    for root in roots:
        roots_checked.append(root)
        if not root.exists() or not root.is_dir():
            warnings.append(f"Reference root missing: {root}")
            continue
        count_for_root = 0
        for path in _iter_supported_files(root):
            if count_for_root >= max_files_per_root:
                warnings.append(f"Reference scan capped at {max_files_per_root} files for {root}")
                break
            count_for_root += 1
            files_scanned += 1
            result = _score_file(path, terms=terms, max_file_chars=max_file_chars, max_snippet_chars=max_snippet_chars)
            if result is not None:
                scored.append(result)
    scored.sort(key=lambda item: (-item.score, str(item.path)))
    return ReferenceContext(results=scored[:top_k], warnings=warnings, roots_checked=roots_checked, files_scanned=files_scanned)


def _candidate_roots(workspace: Path) -> list[Path]:
    raw = [
        workspace / "ReferenceLibrary",
        workspace / "AbstractLibrary",
        workspace / "Bridges",
        workspace / "TaciturnRaw",
        workspace / "Library" / "AbstractLibrary",
        workspace / "Library" / "Bridges",
        workspace / "Library" / "TaciturnRaw",
        workspace / ".literarygiant" / "memory",
        workspace / ".learnings",
    ]
    parent = workspace.parent
    if workspace.name == "LiteraryAgent":
        raw.extend(
            [
                parent / "ReferenceLibrary",
                parent / "Library" / "AbstractLibrary",
                parent / "Library" / "Bridges",
                parent / "Library" / "TaciturnRaw",
                parent / ".learnings",
            ]
        )
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in raw:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def _iter_supported_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        if any(part.startswith(".") and part not in {".literarygiant", ".learnings"} for part in path.parts):
            continue
        yield path


def _score_file(path: Path, *, terms: list[str], max_file_chars: int, max_snippet_chars: int) -> ReferenceResult | None:
    path_text = str(path).lower()
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    content = raw[:max_file_chars]
    lower = content.lower()
    matched: list[str] = []
    score = 0
    for term in terms:
        if not term:
            continue
        term_lower = term.lower()
        path_hits = path_text.count(term_lower)
        content_hits = lower.count(term_lower)
        heading_hits = len(re.findall(rf"(?m)^#+ .*{re.escape(term_lower)}", lower))
        if path_hits or content_hits or heading_hits:
            matched.append(term)
            score += path_hits * 8 + min(content_hits, 8) * 3 + heading_hits * 6
    if score <= 0:
        return None
    snippet = _snippet(content, matched, max_chars=max_snippet_chars)
    return ReferenceResult(path=path, score=score, matched_terms=matched, snippet=snippet)


def _snippet(content: str, terms: list[str], *, max_chars: int) -> str:
    if not content:
        return ""
    lower = content.lower()
    hit_positions = [lower.find(term.lower()) for term in terms if term and lower.find(term.lower()) >= 0]
    center = min(hit_positions) if hit_positions else 0
    start = max(0, center - max_chars // 3)
    end = min(len(content), start + max_chars)
    snippet = content[start:end]
    if start > 0:
        snippet = "..." + snippet
    if end < len(content):
        snippet += "..."
    return snippet


def _query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for term in DOMAIN_TERMS:
        if term in query:
            terms.append(term)
    for item in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", query):
        if len(item) <= 1:
            continue
        if item not in CHINESE_STOP_TERMS:
            terms.append(item)
        if re.fullmatch(r"[\u4e00-\u9fff]+", item):
            terms.extend(
                term
                for term in (item[index : index + 2] for index in range(0, max(0, len(item) - 1)))
                if term not in CHINESE_STOP_TERMS
            )
    deduped: list[str] = []
    seen: set[str] = set()
    for term in terms:
        if term in seen:
            continue
        seen.add(term)
        deduped.append(term)
    return deduped[:32]


def reference_summary_json(context: ReferenceContext) -> str:
    return json.dumps(context.to_metadata(), ensure_ascii=False, indent=2)
