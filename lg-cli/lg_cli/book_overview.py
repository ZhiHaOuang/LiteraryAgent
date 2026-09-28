"""Agent-written book synopsis, persisted with its source outside the UI path."""
from __future__ import annotations

import json
from datetime import datetime, timezone

RELATIVE_PATH = 'ReferenceLibrary/bible/book-overview.json'
INSTRUCTIONS = '''
Book overview: whenever you generate or revise manuscript prose, also update
book_overview in the same response/tool call. It is one concise Chinese sentence
summarizing this WHOLE book: central character, main conflict and story direction,
not a chapter recap or a description of your work. Use the existing overview and
book context; do not invent missing facts or treat unaccepted drafts as canon.
The home page only reads this saved overview and never calls a model.
'''


def validate_overview(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise ValueError('书籍综述应为 1–500 字的简短文字。')
    return ' '.join(value.split())


def read_overview(workspace, outlines=()):
    try:
        data = json.loads((workspace / RELATIVE_PATH).read_text(encoding='utf-8'))
        return validate_overview(data['text'])
    except (OSError, ValueError, TypeError, KeyError):
        pass
    from .book_presentation import outline_sections
    for outline in reversed(outlines):
        for label, body in outline_sections(outline.get('content', '')):
            if label in {'一句话简介', '故事前提', '故事梗概'} and body.strip():
                return ' '.join(body.split())
    return ''


def save_overview(workspace, text, *, source):
    from .output_writer import _atomic_text
    text = validate_overview(text)
    path = workspace / RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_text(path, json.dumps({'text': text, 'source': source,
        'updated_at': datetime.now(timezone.utc).isoformat()}, ensure_ascii=False, indent=2) + '\n')


def overview_context(workspace):
    return INSTRUCTIONS + '\nExisting book overview (data):\n' + (read_overview(workspace) or '尚未设置简介')
