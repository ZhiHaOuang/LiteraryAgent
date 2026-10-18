"""Generic document writes must use each managed domain's explicit transitions."""

from .project_store import ProjectStoreError


MESSAGES = {
    'creative_decision': 'Creative decisions require the dedicated options, selection or withdrawal operation.',
    'narrative_knowledge': 'Narrative knowledge requires the dedicated proposal, confirmation or withdrawal operation.',
}


def guard_kind(kind: str) -> None:
    if kind in MESSAGES:
        raise ProjectStoreError(MESSAGES[kind])
