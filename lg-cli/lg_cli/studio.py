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


def review_source(store: ProjectStore, slug: str, version_number: int) -> dict:
    """Freeze the same chapter/candidate/Canonical evidence for MCP and host reviewers."""
    from .supervision import chapter_for
    version = store.get_version(slug, version_number)
    chapter = chapter_for(store, store.get_document(slug))
    if chapter is None:
        raise ValueError('Reviews require a chapter or a scene belonging to one')
    return BookAnalysisStore(store).candidate_snapshot(chapter, version)


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
    # This is a dashboard preview. Full CLI material now has a paged/detail surface.
    for field in ('characters', 'relationships', 'settings', 'foreshadowing', 'volumes', 'tasks', 'pending'):
        values = result[field]
        preview = [s[:500] + ('…（详情见资料库）' if len(s) > 500 else '') for s in values[:24]]
        if len(values) > 24:
            preview.append(f'另有 {len(values) - 24} 项，请进入资料库查看。')
        result[field] = preview
    return result


def register_tools(server, config: LGConfig, approved, *, observed=None):
    store = ProjectStore(config.workspace)
    observed = observed or (lambda function: function)

    @server.tool()
    @observed
    def book_dashboard() -> dict:
        """Read the same local book overview used by the CLI, extended with planning and pending work."""
        return dashboard(config)

    @server.tool()
    @observed
    def book_library(topic: str, query: str = '', offset: int = 0) -> dict:
        """Read a bounded page from the same saved material and source evidence shown by the CLI. No model call or write."""
        from .workspace_library import library
        return library(config, topic, query, offset)

    @server.tool()
    @observed
    def book_entry(topic: str, key: str) -> dict:
        """Read one CLI material detail, source and recent version labels, only when requested."""
        from .workspace_library import entry
        return entry(config, topic, key)

    @server.tool()
    @observed
    def book_action_context(topic: str, key: str, action: str, expected_fingerprint: str) -> dict:
        """Prepare an author-requested writing action from an unchanged preview. This only reads; it never starts generation or approves writes."""
        from .workspace_library import writing_context
        return writing_context(config, topic, key, action, expected_fingerprint)

    @server.tool()
    @observed
    def studio_decision(slug: str) -> dict:
        """Read a saved creative decision, all three latest options and its explicit selection.
        The active version is the latest record, not a choice. A null selection means no official direction.
        Source statuses describe declared sources, not exhaustive model knowledge or character visibility.
        """
        return store.decision_snapshot(slug)

    @server.tool()
    @observed
    def studio_knowledge(slug: str) -> dict:
        """Read one versioned character/reader knowledge record and its explicit confirmation.
        A proposal is not confirmed knowledge, Canonical truth or a Runtime visibility boundary.
        """
        return store.knowledge_snapshot(slug)

    @server.tool()
    @observed
    def studio_knowledge_view(audience: str, position: int, holder_document: str | None = None,
                              limit: int = 100) -> dict:
        """Project confirmed narrative knowledge at one reading/story position.
        audience is character or reader; character requires the exact holder document. This reads
        declared records only, never infers knowledge from Canonical facts or isolates model inputs.
        """
        return store.knowledge_view(audience=audience, position=position,
            holder_document=holder_document, limit=limit)

    @server.tool()
    @approved
    def studio_knowledge_propose(slug: str, title: str, topic: dict, statement: dict,
                                 rationale: str, expected_version_id: int) -> dict:
        """Save a provisional narrative knowledge statement; never confirm or promote Canonical facts.
        topic: audience character|reader, holder_document_id int|null, key, from_position int.
        statement: content, stance knows|believes|uncertain, until_position int|null, sources with
        exact document_id/version_id. New records require expected_version_id=0. Confirmation and
        withdrawal require separate author creative approval; these records do not isolate Runtime inputs.
        """
        return store.propose_knowledge(slug=slug, title=title, topic=topic, statement=statement,
            rationale=rationale, expected_version_id=expected_version_id)

    @server.tool()
    @approved
    def studio_knowledge_confirm(slug: str, proposal_version_number: int, expected_version_id: int) -> dict:
        """Confirm exactly one saved narrative knowledge proposal after separate creative approval.
        Bind its proposal version number and the current record head; never infer confirmation from
        saving a proposal. Character belief and reader revelation are distinct from Canonical truth.
        """
        return store.confirm_knowledge(slug, proposal_version_number=proposal_version_number,
            expected_version_id=expected_version_id)

    @server.tool()
    @approved
    def studio_knowledge_withdraw(slug: str, expected_version_id: int) -> dict:
        """Withdraw the current confirmed narrative knowledge statement with separate creative approval.
        Bind the current record head. Preserve history, manuscript and Canonical facts; this does not
        erase information from existing model conversations.
        """
        return store.withdraw_knowledge(slug, expected_version_id=expected_version_id)

    @server.tool()
    @observed
    def studio_source_impact(slug: str, version_number: int | None = None, limit: int = 100) -> dict:
        """Read known direct source relationships and captured-source diagnostics for one exact saved source.
        Version numbers belong to this document; omit one for document-wide direct associations, with the
        captured-source diagnostic defaulting to its active version. Coverage is partial
        or unknown, and an index exposure does not prove content was read. This author-global diagnostic
        does not infer complete semantic dependencies, modify facts/plans/candidates or authorize any write.
        """
        return store.source_impact(slug, version_number=version_number, limit=limit)

    @server.tool()
    @approved
    def studio_decision_options(slug: str, title: str, topic: dict, options: list[dict],
                                sources: list[dict], request: str, expected_version_id: int) -> dict:
        """Save exactly three distinct provisional creative options; never choose a direction.
        topic is {scope: work|part|chapter, document_id: int|null, key: str}; options contain unique
        id/title/content; sources contain document_id/version_id. New decisions require expected_version_id=0.
        Regeneration preserves the current selection, even when it points to an older options version.
        With no document sources, request is a saved declared request context, not verified author wording.
        Selection and withdrawal require separate creative approval.
        """
        result = store.save_decision_options(slug=slug, title=title, topic=topic, options=options,
            sources=sources, request=request, expected_version_id=expected_version_id)
        output = write_workflow_output(config.workspace, 'plan',
            '# ' + result['title'] + '\n\n创作方向记录：active 版本是最新记录，不等于正式选择。\n\n'
            + json.dumps(result, ensure_ascii=False, indent=2))
        return {**result, 'artifact': str(output.output_path.relative_to(config.workspace))}

    @server.tool()
    @approved
    def studio_select_decision(slug: str, options_version_number: int, option_id: str,
                               expected_version_id: int) -> dict:
        """Select exactly one saved option after independent author creative approval.
        Bind the options version number and option ID, plus the current record head; never select by title
        or silently choose the latest options. This does not adopt manuscript or promote Canonical facts.
        """
        return store.select_decision_option(slug, options_version_number=options_version_number,
            option_id=option_id, expected_version_id=expected_version_id)

    @server.tool()
    @approved
    def studio_withdraw_decision(slug: str, expected_version_id: int) -> dict:
        """Withdraw the current official creative choice to an empty selection with separate creative approval.
        Bind the current record head. Withdrawal remains available when declared sources have changed;
        it preserves options/history and does not roll back manuscript or Canonical facts.
        """
        return store.withdraw_decision(slug, expected_version_id=expected_version_id)

    @server.tool()
    @approved
    def studio_reference(kind: Literal['character', 'world', 'outline', 'note'], slug: str,
                         title: str, content: str, expected_version_id: int, reason: str,
                         story_index: dict | None = None) -> dict:
        """Save author-requested literary reference material as a candidate version. Never adopt automatically or promote Canonical facts.
        Read an existing document first; new material requires expected_version_id=0.
        """
        if not content.strip():
            raise ValueError('Reference candidate cannot be empty')
        if story_index is not None:
            from .story_index import validate_graph
            validate_graph(story_index)
        documents = [d for d in store.list_documents(kind=kind, limit=None) if d.slug == slug]
        metadata = {'story_index': story_index} if story_index is not None else {}
        if documents:
            document = documents[0]
            if document.active_version_id != expected_version_id:
                raise ValueError('Reference changed; reread before updating')
            version = store.create_version(slug, content=content, state='candidate', reason=reason,
                                           metadata=metadata, activate=False, expected_version_id=expected_version_id)
        else:
            if expected_version_id != 0:
                raise ValueError('New reference requires expected_version_id=0')
            document = store.create_document(kind=kind, slug=slug, title=title, content=content,
                                             state='candidate', reason=reason, metadata=metadata)
            version = store.get_version(slug, document.active_version_number)
        output = write_workflow_output(config.workspace, kind if kind != 'note' else 'bible-proposal',
            '# ' + title + '\n\n状态：候选资料，尚未采用或确认为事实。\n\n' + content)
        return {'document': slug, 'version': asdict(version), 'state': 'candidate',
                'artifact': str(output.output_path.relative_to(config.workspace))}

    @server.tool()
    @approved
    def studio_adopt_reference(slug: str, version_number: int, expected_version_id: int) -> dict:
        """Adopt the exact author-previewed literary reference candidate using the existing CLI ProjectStore. Does not promote facts to Canonical."""
        document = store.get_document(slug)
        if document.kind not in {'character', 'world', 'outline', 'note'}:
            raise ValueError('Use the existing manuscript review/adoption tools for prose')
        if document.active_version_id != expected_version_id:
            raise ValueError('Reference changed after preview')
        if store.get_version(slug, version_number).state != 'candidate':
            raise ValueError('Only a candidate can be adopted once')
        return {'document': asdict(store.accept_version(slug, version_number,
            expected_version_id=expected_version_id)), 'accepted': True}

    @server.tool()
    @approved
    def studio_accept_proposal(proposal_id: int, expected_fingerprint: str) -> dict:
        """Author-requested adoption of one previewed setting proposal. Uses the same accept_proposal as CLI bible proposal accept. Refuse stale previews."""
        from .workspace_library import _stamp
        if _stamp(config) != expected_fingerprint:
            raise ValueError('Book changed after preview; review the proposal again')
        return {'fact': asdict(store.accept_proposal(proposal_id)), 'accepted': True}

    @server.tool()
    @observed
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
                metadata={'story_index': story_index} if story_index is not None else {}, activate=False,
                expected_version_id=expected_version_id)
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
    @observed
    def studio_review_context(slug: str, version_number: int, category: str | None = None) -> dict:
        """Read the exact source snapshot and existing review schemas. Review all five categories;
        evidence must quote the supplied sources. No review is valid for a different snapshot.
        """
        if category is not None and category not in CATEGORIES:
            raise ValueError('Unknown review category')
        source = review_source(store, slug, version_number)
        return {'snapshot': source, 'source_spans': source_spans(source), 'sourceFingerprint': fingerprint(source),
                'generationSourceReview': store.generation_source_review(slug, version_number),
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
        from .supervision import chapter_for
        chapter = chapter_for(store, store.get_document(slug))
        if chapter is None:
            raise ValueError('Reviews require a chapter or a scene belonging to one')
        source, base = records.candidate_snapshot(chapter, version), records.snapshot(chapter)
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
        document = store.get_document(slug)
        if document.kind not in {'chapter', 'scene'}:
            raise ValueError('Use studio_adopt_reference for literary reference candidates')
        if document.active_version_id != expected_version_id:
            raise ValueError('Active manuscript changed; compare before accepting')
        version = store.get_version(slug, version_number)
        if version.state != 'candidate':
            raise ProjectStoreError('Only a candidate can be adopted; do not repeat a successful adoption')
        accepted = store.accept_version(slug, version_number, expected_version_id=expected_version_id)
        return {'document': asdict(accepted), 'accepted': True}
