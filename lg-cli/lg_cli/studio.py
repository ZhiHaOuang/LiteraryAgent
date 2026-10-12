"""Shared literary workbench operations; UI clients never own book state."""
from __future__ import annotations

import json
import jsonschema
from dataclasses import asdict
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema

from .book_analysis import BookAnalysisStore, CATEGORIES, fingerprint, response_schema, validate_report, resolve_evidence, source_spans
from .book_home import snapshot
from .config import LGConfig
from .output_writer import write_workflow_output
from .project_store import ProjectStore, ProjectStoreError

PROFILE_SLUG = 'lg-studio-profile'
ROUTES = {'position': 'plan', 'plan': 'outline', 'world': 'world', 'scene': 'scene-plan', 'review': 'check'}


def review_reports_schema() -> dict:
    properties = {}
    for category in CATEGORIES:
        schema = response_schema(category)

        def inline(value):
            if isinstance(value, list):
                return [inline(item) for item in value]
            if not isinstance(value, dict):
                return value
            if '$ref' in value:
                # Report schemas use local definitions; embedding must not leave
                # references pointing at the surrounding MCP parameter root.
                prefix = '#/$defs/'
                reference = value['$ref']
                if not reference.startswith(prefix):
                    raise ValueError('Unsupported report schema reference')
                return inline(schema['$defs'][reference[len(prefix):]])
            return {key: inline(item) for key, item in value.items() if key not in {'$schema', '$defs'}}

        properties[category] = inline(schema)
    return {'type': 'object', 'minProperties': 1, 'additionalProperties': False, 'properties': properties}


ReviewReports = Annotated[dict, WithJsonSchema(review_reports_schema())]


class StudioProfile(BaseModel):
    model_config = ConfigDict(extra='forbid')
    positioning: str = ''
    targetWords: int = Field(default=1_000_000, ge=1, le=100_000_000)
    volumes: list[str] = Field(default_factory=list)
    characters: list[str] = Field(default_factory=list)
    relationships: list[str] = Field(default_factory=list)
    settings: list[str] = Field(default_factory=list)
    foreshadowing: list[str] = Field(default_factory=list)
    tasks: list[str] = Field(default_factory=list)
    pending: list[str] = Field(default_factory=list)


def profile(store: ProjectStore):
    if not store.initialized:
        return None, StudioProfile()
    documents = [d for d in store.list_documents(kind='book') if d.slug == PROFILE_SLUG]
    if not documents:
        return None, StudioProfile()
    document = documents[0]
    return document, StudioProfile.model_validate_json(document.content)


def dashboard(config: LGConfig) -> dict:
    data = snapshot(config)
    store = ProjectStore(config.workspace)
    _, planning = profile(store)
    names = {c['id']: c['name'] for c in data['characters']}
    chapters = []
    accepted_words = 0
    for chapter in data['chapters']:
        # Count chapter and scene bodies once, without Markdown export framing.
        bodies = [chapter, *[asdict(d) for d in store.list_documents(kind='scene', parent=chapter['id']) if d.state != 'archived']]
        words = sum(len(''.join(d['content'].split())) for d in bodies)
        accepted_words += sum(len(''.join(d['content'].split())) for d in bodies if d['state'] in {'accepted', 'final'})
        chapters.append({'id': str(chapter['id']), 'title': chapter['title'], 'state': chapter['state'], 'words': words})
    result = dict(planning.model_dump(), name=data['name'], overview=data.get('overview') or '', chapters=chapters,
        writtenWords=sum(c['words'] for c in chapters), acceptedWords=accepted_words, source='backend')
    result['characters'] = [c['name'] + '：' + c.get('summary', '') for c in data['characters']] + ['规划：' + s for s in planning.characters]
    result['relationships'] = [names.get(r['source'], r['source']) + ' → ' + names.get(r['target'], r['target']) + '：' + r['label'] for r in data['relationships']] + ['规划：' + s for s in planning.relationships]
    if store.initialized:
        result['settings'] = [f'{f.key}：{f.value}（{f.state}）' for f in store.list_facts()] + ['规划：' + s for s in planning.settings]
        result['foreshadowing'] = [f'{f.title}（{f.state}）' for f in store.list_foreshadowing()] + ['规划：' + s for s in planning.foreshadowing]
        result['pending'] += [f'设定提案：{p.key}' for p in store.list_proposals()]
        for document in store.list_documents(kind='chapter'):
            result['pending'] += [f'{document.title} · v{v.version_number} 待审阅/采用' for v in store.list_versions(document.id) if v.state == 'candidate']
    return result


def register_tools(server, config: LGConfig, approved):
    store = ProjectStore(config.workspace)

    @server.tool()
    def book_dashboard() -> dict:
        """Read the same local book overview used by the CLI, extended with planning and pending work."""
        return dashboard(config)

    @server.tool()
    def studio_status() -> dict:
        """Read the planning profile version before saving a stage. Zero means no profile yet."""
        document, current = profile(store)
        return {'initialized': store.initialized, 'profileVersion': document.active_version_id if document else 0,
                'profile': current.model_dump(), 'stages': list(ROUTES)}

    @server.tool()
    @approved
    def studio_initialize() -> dict:
        """Initialize only the current workspace with the existing LG initializer. Never open or alter another book."""
        from .init_project import init_workspace
        result = init_workspace(config.workspace)
        return {'created': [str(p.relative_to(config.workspace)) for p in result.created], 'initialized': store.initialized}

    @server.tool()
    @approved
    def studio_save(stage: Literal['position', 'plan', 'world', 'scene', 'review'], content: str,
                    updates: dict, expected_profile_version: int) -> dict:
        """Save a provisional planning/review artifact and update the book profile. Read studio_status first.
        Allowed updates: positioning, targetWords, volumes, characters, relationships, settings,
        foreshadowing, tasks, pending. Planning does not promote facts or accept manuscript.
        """
        if not content.strip():
            raise ValueError('An actual stage artifact is required')
        document, current = profile(store)
        actual = document.active_version_id if document else 0
        if actual != expected_profile_version:
            raise ValueError('Planning changed; reread studio_status before saving')
        next_profile = StudioProfile.model_validate({**current.model_dump(), **updates})
        serialized = next_profile.model_dump_json(indent=2)
        if document:
            version = store.edit_active_document(PROFILE_SLUG, content=serialized,
                expected_version_id=expected_profile_version, reason='Author-approved studio stage: ' + stage)
            version_id = version.id
        else:
            created = store.create_document(kind='book', slug=PROFILE_SLUG, title='创作定位与长篇规划', content=serialized)
            version_id = created.active_version_id
        output = write_workflow_output(config.workspace, ROUTES[stage],
            '# 创作工作资料\n\n状态：规划/审阅记录；不等于正式设定或已采用正文。\n\n' + content)
        return {'profileVersion': version_id, 'artifact': str(output.output_path.relative_to(config.workspace)), 'stage': stage}

    @server.tool()
    @approved
    def studio_draft(slug: str, title: str, content: str, sequence: int, expected_version_id: int,
                     reason: str, story_index: dict | None = None) -> dict:
        """Save short original prose as a candidate, never automatically accept it. For a new chapter use
        expected_version_id=0. For revision read the active document and pass its exact version id.
        """
        if not content.strip():
            raise ValueError('Candidate prose cannot be empty')
        if story_index is not None:
            from .story_index import validate_graph
            validate_graph(story_index)
        existing = [d for d in store.list_documents(kind='chapter') if d.slug == slug]
        before = existing[0].content if existing else ''
        if existing:
            document = existing[0]
            if document.active_version_id != expected_version_id:
                raise ValueError('Active manuscript changed; reread it before drafting')
            version = store.create_version(slug, content=content, state='candidate', reason=reason,
                metadata={'story_index': story_index} if story_index is not None else {}, activate=False)
        else:
            if expected_version_id != 0:
                raise ValueError('New chapters require expected_version_id=0')
            document = store.create_document(kind='chapter', slug=slug, title=title, content=content,
                state='candidate', sequence=sequence, reason=reason,
                metadata={'story_index': story_index} if story_index is not None else {})
            version = store.get_version(slug, document.active_version_number)
        output = write_workflow_output(config.workspace, 'candidate', content)
        return {'document': slug, 'version': asdict(version), 'artifact': str(output.output_path.relative_to(config.workspace)),
                'diff': {'path': str(output.output_path.relative_to(config.workspace)), 'before': before, 'after': content, 'applied': False}}

    @server.tool()
    def studio_review_context(slug: str, version_number: int, category: str | None = None) -> dict:
        """Read the exact source snapshot and existing review schemas. Review all five categories;
        evidence must quote the supplied sources. No review is valid for a different snapshot.
        """
        records = BookAnalysisStore(store)
        if category is not None and category not in CATEGORIES:
            raise ValueError('Unknown review category')
        version = store.get_version(slug, version_number)
        source = records.candidate_snapshot(slug, version)
        return {'snapshot': source, 'source_spans': source_spans(source), 'sourceFingerprint': fingerprint(source),
                'schemas': {key: response_schema(key) for key in ([category] if category else CATEGORIES)}}

    @server.tool()
    @approved
    def studio_review(slug: str, version_number: int, source_fingerprint: str, reports: ReviewReports) -> dict:
        """Save source-validated reviews produced from studio_review_context. Prefer one category per call.
        All five must be current before adoption; partial review never authorizes acceptance.
        This records review evidence; it does not approve manuscript or dismiss Canonical conflicts.
        """
        records = BookAnalysisStore(store)
        version = store.get_version(slug, version_number)
        source, base = records.candidate_snapshot(slug, version), records.snapshot(slug)
        if fingerprint(source) != source_fingerprint:
            raise ValueError('Reviewed source changed; rerun the review')
        if not reports or not set(reports).issubset(CATEGORIES):
            raise ValueError('One or more known review categories are required')
        from .reference_contracts import finalize_cards
        converted = {}
        for category, response in reports.items():
            jsonschema.validate(response, response_schema(category))
            report = resolve_evidence(source, finalize_cards(response, category, source_spans(source)))
            validate_report(source, report, category=category)
            converted[category] = report
        for category, report in converted.items():
            records.save(source, category, report, 'studio-' + source_fingerprint[:16], candidate_id=version.id, base_snapshot=base)
        return {'reviewed': list(reports), 'version': version_number, 'sourceFingerprint': source_fingerprint}

    @server.tool()
    @approved
    def studio_accept(slug: str, version_number: int, expected_version_id: int) -> dict:
        """Explicitly adopt a reviewed candidate once. Existing LG supervision rejects missing,
        stale or conflicting reviews. Requires the unchanged active manuscript version.
        """
        document = store.get_document(slug, kind='chapter')
        if document.active_version_id != expected_version_id:
            raise ValueError('Active manuscript changed; compare before accepting')
        version = store.get_version(slug, version_number)
        if version.state != 'candidate':
            raise ProjectStoreError('Only a candidate can be adopted; do not repeat a successful adoption')
        accepted = store.accept_version(slug, version_number)
        return {'document': asdict(accepted), 'accepted': True}
