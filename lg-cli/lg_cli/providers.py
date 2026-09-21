"""Provider presets are separate from credentials and model entitlements."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Provider:
    name: str
    protocol: str
    base_url: str
    default_model: str = ""
    key_envs: tuple[str, ...] = ()


PROVIDERS = {
    "deepseek": Provider(
        "DeepSeek",
        "anthropic",
        "https://api.deepseek.com/anthropic",
        "deepseek-flash",
        ("DEEPSEEK_API_KEY",),
    ),
    "glm": Provider(
        "GLM",
        "anthropic",
        "https://open.bigmodel.cn/api/anthropic",
        key_envs=("GLM_API_KEY", "ZHIPU_API_KEY"),
    ),
    "stepfun": Provider(
        "StepFun",
        "anthropic",
        "https://api.stepfun.com",
        key_envs=("STEPFUN_API_KEY", "STEP_API_KEY"),
    ),
    "anthropic": Provider(
        "Anthropic",
        "anthropic",
        "https://api.anthropic.com",
        key_envs=("ANTHROPIC_API_KEY",),
    ),
    "openai": Provider(
        "OpenAI API",
        "responses",
        "https://api.openai.com/v1",
        key_envs=("OPENAI_API_KEY", "CODEX_API_KEY"),
    ),
    "responses-compatible": Provider(
        "Responses-compatible proxy", "responses", "", key_envs=("LG_PROXY_API_KEY",)
    ),
    "anthropic-compatible": Provider(
        "Messages-compatible proxy", "anthropic", "", key_envs=("LG_PROXY_API_KEY",)
    ),
}


def provider_id(value: str) -> str:
    return "deepseek" if value == "deepseek-anthropic" else value


def validate_model(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 200
        or any(c.isspace() or ord(c) < 32 for c in value)
    ):
        raise ValueError("A nonempty model ID without whitespace is required")
    return value


def validate_endpoint(value: str) -> str:
    try:
        parsed = urlsplit(value)
        _ = parsed.port  # Access validates malformed/out-of-range ports.
        local_http = parsed.scheme == "http" and parsed.hostname in {
            "127.0.0.1",
            "::1",
            "localhost",
        }
        if (
            not parsed.hostname
            or (parsed.scheme != "https" and not local_http)
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or any(c.isspace() or ord(c) < 32 for c in value)
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError(
            "Base URL must use HTTPS (HTTP only on loopback), without credentials, query or fragment"
        ) from None
    return value.rstrip("/")


def profile_settings(
    provider: str, model: str | None = None, base_url: str | None = None
) -> dict:
    provider = provider_id(provider)
    if provider not in PROVIDERS:
        raise ValueError("Unknown provider; run literary auth providers")
    preset = PROVIDERS[provider]
    return {
        "provider": provider,
        "auth_mode": "api_key",
        "protocol": preset.protocol,
        "model": validate_model(model or preset.default_model),
        "base_url": validate_endpoint(base_url or preset.base_url),
    }


def normalize_profile(value: dict) -> dict:
    if not isinstance(value, dict):
        raise TypeError("Invalid profile")
    if value.get("provider") == "codex" and value.get("auth_mode") == "chatgpt":
        if (
            value.get("key")
            or value.get("base_url")
            or value.get("protocol", "codex") != "codex"
        ):
            raise ValueError(
                "Subscription credentials must stay in the official Codex auth store"
            )
        model = value.get("model", "")
        if model:
            validate_model(model)
        elif not isinstance(model, str):
            raise ValueError("Invalid model")
        return {
            "provider": "codex",
            "auth_mode": "chatgpt",
            "protocol": "codex",
            "model": model,
            "base_url": "",
        }
    if value.get("auth_mode", "api_key") != "api_key":
        raise ValueError("Unsupported authentication mode")
    settings = profile_settings(
        value.get("provider", ""), value.get("model"), value.get("base_url")
    )
    if value.get("protocol", settings["protocol"]) != settings["protocol"]:
        raise ValueError("Provider protocol mismatch")
    key = value.get("key")
    if (
        not isinstance(key, str)
        or not key
        or any(c.isspace() or ord(c) < 32 for c in key)
    ):
        raise ValueError("Invalid API key")
    return {**settings, "key": key}
