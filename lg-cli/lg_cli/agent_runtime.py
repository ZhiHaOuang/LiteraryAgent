"""Product-owned execution contract; providers and wire protocols stay in adapters."""
from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol

from .session_runtime import SessionRuntime


class RuntimePolicyError(RuntimeError):
    """A product-owned policy refusal whose fixed message is safe to show the author."""


@dataclass(frozen=True)
class RuntimeResult:
    status: Literal['completed', 'cancelled', 'failed']
    result_id: str | None = None
    text: str | None = None


class RuntimeContext(Protocol):
    """Host-owned input preparation, evaluated only when an executor needs it."""

    def instructions(self, *, control_socket: str | None = None,
                     review_delegation: bool = False, core_delegation: bool | None = None) -> str: ...
    def history(self, text: str) -> str: ...


class RuntimeLaunchPlan(Protocol):
    """Frozen JSON-valued Core overrides and their effective-config validator."""
    @property
    def overrides(self) -> tuple[tuple[str, str], ...]: ...
    def validate(self, reply: dict) -> None: ...


class RuntimeLaunchPolicy(Protocol):
    def prepare(self, config, *, control_socket: str | None = None,
                review_only: bool = False, allow_core_delegation: bool = True) -> RuntimeLaunchPlan: ...


@dataclass(frozen=True)
class ExpertInput:
    name: str
    model_profile: str
    instructions: str
    prompt: str
    output_schema_json: str


@dataclass(frozen=True)
class ReviewInput:
    task_id: str
    task_fingerprint: str
    source_fingerprint: str
    experts: tuple[ExpertInput, ...]


class ExpertPolicy(Protocol):
    """Host preparation and source validation for bounded, tool-free reviews."""
    tool: dict

    def available(self, scope: dict | None) -> bool: ...
    def prepare(self, scope: dict, arguments: dict, *, context_limit: int) -> ReviewInput: ...
    def unchanged(self, scope: dict, record: dict) -> bool: ...
    def output_schema(self, name: str) -> dict: ...


class AgentRuntime(Protocol):
    runtime: SessionRuntime | None
    control_socket: str | None
    thread_id: str | None
    turn_id: str | None
    async def start(self, *, context: str = '') -> None: ...
    async def run(self, text: str, *, output_schema: dict | None = None) -> RuntimeResult: ...
    async def steer(self, text: str) -> Any: ...
    async def interrupt(self) -> None: ...
    async def close(self) -> None: ...


def runtime_factory(config, event: Callable, *, agent_factory=None,
                    context_provider: RuntimeContext | None = None,
                    expert_policy: ExpertPolicy | None = None,
                    launch_policy: RuntimeLaunchPolicy | None = None, **kwargs) -> AgentRuntime:
    codex = agent_factory is not None or config.runtime_engine == 'codex'
    if context_provider is None:
        from .runtime_context import LiteraryContext
        context_provider = LiteraryContext(config, delegation=codex, review_only=kwargs.get('review_only', False))
    if codex:
        if expert_policy is None:
            from .review_policy import LiteraryReviewPolicy
            expert_policy = LiteraryReviewPolicy(config.workspace)
        from .codex_runtime import CodexRuntime
        if launch_policy is None:
            from .codex_policy import WritingLaunchPolicy
            launch_policy = WritingLaunchPolicy()
        return CodexRuntime(config, event, agent_factory=agent_factory, context_provider=context_provider,
                            launch_policy=launch_policy, expert_policy=expert_policy, **kwargs)
    if config.runtime_engine == 'native':
        if launch_policy is not None:
            raise ValueError('Injected Core launch policy is supported only by Codex runtime')
        from .native_runtime import NativeRuntime
        if expert_policy is None:
            from .review_policy import LiteraryReviewPolicy
            expert_policy = LiteraryReviewPolicy(config.workspace)
        return NativeRuntime(config, event, context_provider=context_provider, expert_policy=expert_policy, **kwargs)
    raise ValueError('Unknown LG execution runtime')
