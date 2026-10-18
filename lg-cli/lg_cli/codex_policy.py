"""Validate the effective adapter config before Core starts a literary thread."""
import json
from dataclasses import dataclass

from .native import writing_settings
from .agent_runtime import RuntimePolicyError

ERROR='Codex 工具隔离配置不符合 LG 文学任务契约，未启动或恢复写作线程；请检查额外 MCP 或覆盖 LG 工具的配置。'
READ_TOOLS=['project_memory','read_document','reference_search','document_history']


@dataclass(frozen=True)
class WritingLaunchPlan:
    settings_json: str

    @property
    def overrides(self) -> tuple[tuple[str, str], ...]:
        return tuple((key,json.dumps(value)) for key,value in json.loads(self.settings_json).items())

    def validate(self, reply):
        _validate_expected(reply,json.loads(self.settings_json))


class WritingLaunchPolicy:
    def prepare(self, config, *, control_socket=None, review_only=False, allow_core_delegation=True):
        settings = writing_settings(config,control_socket=control_socket)
        settings['features.multi_agent'] = not review_only and allow_core_delegation
        settings['features.multi_agent_v2'] = not review_only and allow_core_delegation
        if review_only or not allow_core_delegation:
            # Feature flags alone do not override persisted/model V2 collaboration.
            settings['agents.enabled'] = False
        if review_only:
            settings['mcp_servers.lg_writing.enabled_tools'] = list(READ_TOOLS)
        return WritingLaunchPlan(json.dumps(settings))


def validate_writing_config(reply,config,*,control_socket=None,review_only=False):
    """Compatibility entry for callers without a previously prepared launch plan."""
    WritingLaunchPolicy().prepare(config,control_socket=control_socket,review_only=review_only).validate(reply)


def _validate_expected(reply,expected):
    """Never log/return the config snapshot: it may contain private header/env values."""
    try:
        effective=reply['config']
        layers=reply['layers']
        if not isinstance(layers,list):raise ValueError()
        for layer in layers:
            if not isinstance(layer,dict) or not isinstance(layer.get('config'),dict):raise ValueError()
            # Core may trust/reload project layers at thread start. Inspect these before activation.
            if layer.get('disabledReason') and layer['config'].get('mcp_servers'):raise ValueError()
        servers=effective['mcp_servers']
        if not isinstance(servers,dict) or 'lg_writing' not in servers:raise ValueError()
        for name,server in servers.items():
            if not isinstance(server,dict):raise ValueError()
            if name!='lg_writing' and server.get('enabled',True) is not False:raise ValueError()
        prefix='mcp_servers.lg_writing.';owned=servers['lg_writing']
        for key in ('command','args','cwd','enabled','required','default_tools_approval_mode'):
            if owned.get(key)!=expected[prefix+key]:raise ValueError()
        # Core omits empty env_vars from config/read; nonempty inheritance is forbidden.
        if owned.get('env_vars') not in (None,[]):raise ValueError()
        if owned.get('enabled') is not True or owned.get('required') is not True:raise ValueError()
        if owned.get('environment_id')!='local':raise ValueError()
        if owned.get('env')!={'PYTHONPATH':expected[prefix+'env.PYTHONPATH']}:raise ValueError()
        if owned.get('tools') not in (None,{}) or owned.get('disabled_tools') not in (None,[]):raise ValueError()
        tools=expected[prefix+'enabled_tools']
        if owned.get('enabled_tools')!=tools:raise ValueError()
        features=effective.get('features',{})
        if expected.get('agents.enabled') is False:
            if effective.get('agents',{}).get('enabled') is not False:raise ValueError()
        for key,value in expected.items():
            if key.startswith('features.'):
                actual=features.get(key.split('.',1)[1])
                if isinstance(actual,dict):actual=actual.get('enabled')
                if actual is not value:raise ValueError()
        if effective.get('notify')!=[] or effective.get('web_search')!='disabled':raise ValueError()
    except (KeyError,TypeError,AttributeError,ValueError):
        raise RuntimePolicyError(ERROR) from None
