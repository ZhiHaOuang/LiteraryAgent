"""Process-local Responses transport for an Anthropic Messages provider."""

from __future__ import annotations

import copy
import hashlib
import json
import secrets
import threading
import uuid
from collections.abc import Callable, Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any

from typing_extensions import Self

from .config import LGConfig


class ProtocolError(ValueError):
    pass


def _content(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        return [{"type": "text", "text": value}] if value else []
    if not isinstance(value, list):
        raise ProtocolError("expected text or a content array")
    result = []
    for part in value:
        if part.get("type") not in {"input_text", "output_text", "text"}:
            raise ProtocolError("only text content is supported by this writing bridge")
        result.append({"type": "text", "text": part["text"]})
    return result


def translate_request(request: dict[str, Any], max_tokens: int) -> tuple[dict, dict]:
    if request.get("previous_response_id"):
        raise ProtocolError("send full conversation input, not previous_response_id")
    if (request.get("reasoning") or {}).get("effort") not in {None, "none"}:
        raise ProtocolError(
            "thinking is not enabled in this bridge; use reasoning effort none"
        )
    tools: list[dict] = []
    mapping: dict[str, dict] = {}

    def add_tool(tool: dict, namespace: str | None = None) -> None:
        kind = tool.get("type")
        if kind == "namespace":
            for child in tool.get("tools", []):
                add_tool(child, tool["name"])
            return
        if kind not in {"function", "custom"}:
            raise ProtocolError(f"unsupported tool type: {kind}")
        # Stable wire names also avoid collisions between namespaces.
        name = f"lg_tool_{len(mapping)}"
        mapping[name] = {"name": tool["name"], "namespace": namespace, "type": kind}
        schema = tool.get("parameters", {"type": "object", "properties": {}})
        if kind == "custom":
            schema = {
                "type": "object",
                "properties": {"input": {"type": "string"}},
                "required": ["input"],
                "additionalProperties": False,
            }
        tools.append(
            {
                "name": name,
                "description": tool.get("description", tool["name"]),
                "input_schema": schema,
            }
        )

    for tool in request.get("tools") or []:
        add_tool(tool)
    messages: list[dict] = []
    system = _content(request.get("instructions", ""))

    def append(role: str, content: list[dict]) -> None:
        if not content:
            return
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].extend(content)
        else:
            messages.append({"role": role, "content": content})

    inputs = request.get("input", [])
    if isinstance(inputs, str):
        inputs = [{"role": "user", "content": inputs}]
    for item in inputs:
        kind = item.get("type", "message")
        if kind == "message":
            role = item.get("role")
            content = _content(item.get("content", ""))
            if role in {"system", "developer"}:
                system.extend(content)
            elif role in {"user", "assistant"}:
                append(role, content)
            else:
                raise ProtocolError("unsupported message role")
        elif kind in {"function_call", "custom_tool_call"}:
            name = next(
                (
                    key
                    for key, spec in mapping.items()
                    if spec["name"] == item["name"]
                    and spec["namespace"] == item.get("namespace")
                ),
                None,
            )
            if name is None:
                raise ProtocolError("history refers to a tool absent from this request")
            arguments = (
                json.loads(item["arguments"])
                if kind == "function_call"
                else {"input": item["input"]}
            )
            if not isinstance(arguments, dict):
                raise ProtocolError("tool arguments must be an object")
            append(
                "assistant",
                [
                    {
                        "type": "tool_use",
                        "id": item["call_id"],
                        "name": name,
                        "input": arguments,
                    }
                ],
            )
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            append(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": item["call_id"],
                        "content": _content(item["output"]),
                    }
                ],
            )
        else:
            raise ProtocolError(f"unsupported history item: {kind}")
    if not messages:
        raise ProtocolError("at least one conversation message is required")
    payload: dict[str, Any] = {
        "model": request["model"],
        "max_tokens": max_tokens,
        "messages": messages,
        "stream": True,
        "thinking": {"type": "disabled"},
    }
    if system:
        payload["system"] = system
    if tools:
        payload["tools"] = tools
        choice = request.get("tool_choice", "auto")
        if choice not in {"auto", "none", "required"}:
            raise ProtocolError("unsupported tool_choice")
        payload["tool_choice"] = {"type": "any" if choice == "required" else choice}
    format_spec = (request.get("text") or {}).get("format") or {}
    if format_spec.get("type") == "json_schema":
        # DeepSeek does not support Anthropic output_config.format. Use a schema
        # tool, and translate its arguments into the final JSON response.
        name = "lg_structured_output"
        mapping[name] = {"type": "structured", "schema": format_spec["schema"]}
        payload.setdefault("tools", []).append(
            {
                "name": name,
                "description": "Return the requested final result.",
                "input_schema": format_spec["schema"],
            }
        )
        payload["tool_choice"] = (
            {"type": "auto"} if tools else {"type": "tool", "name": name}
        )
        payload.setdefault("system", []).append(
            {
                "type": "text",
                "text": "Use lg_structured_output for your final answer after any necessary tool calls. Do not combine that final answer with other tool calls.",
            }
        )
    elif format_spec.get("type") not in {None, "text"}:
        raise ProtocolError("unsupported response format")
    return payload, mapping


def translate_stream(events: Iterable[dict], mapping: dict) -> Iterable[dict]:
    response_id = "resp_" + uuid.uuid4().hex
    output: list[dict] = []
    blocks: dict[int, dict] = {}
    usage: dict = {}
    stop_reason = None
    started = False

    def event(kind: str, **fields: Any) -> dict:
        return {"type": "response." + kind, **fields}

    for source in events:
        kind = source["type"]
        if kind == "ping":
            continue
        if kind == "error":
            raise ProtocolError("upstream returned a streaming error")
        if kind == "message_start":
            started = True
            usage.update(
                {
                    k: v
                    for k, v in source["message"].get("usage", {}).items()
                    if v is not None
                }
            )
            yield event(
                "created",
                response={
                    "id": response_id,
                    "object": "response",
                    "status": "in_progress",
                    "output": [],
                },
            )
        elif kind == "content_block_start":
            block = source["content_block"]
            index = len(output)
            item_id = "item_" + uuid.uuid4().hex
            if block["type"] == "text":
                item = {
                    "type": "message",
                    "id": item_id,
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [
                        {
                            "type": "output_text",
                            "text": block.get("text", ""),
                            "annotations": [],
                        }
                    ],
                }
                spec = {"type": "text"}
            elif block["type"] == "tool_use":
                if block["name"] not in mapping:
                    raise ProtocolError("upstream returned an unknown tool")
                spec = mapping[block["name"]]
                if spec["type"] == "structured":
                    item = {
                        "type": "message",
                        "phase": "final_answer",
                        "id": item_id,
                        "role": "assistant",
                        "status": "in_progress",
                        "content": [
                            {"type": "output_text", "text": "", "annotations": []}
                        ],
                    }
                else:
                    item = {
                        "type": "custom_tool_call"
                        if spec["type"] == "custom"
                        else "function_call",
                        "id": item_id,
                        "call_id": block["id"],
                        "name": spec["name"],
                        "status": "in_progress",
                    }
                    if spec["namespace"]:
                        item["namespace"] = spec["namespace"]
                    item["input" if spec["type"] == "custom" else "arguments"] = ""
            else:
                raise ProtocolError("unsupported upstream content block")
            output.append(item)
            blocks[source["index"]] = {
                "item": item,
                "spec": spec,
                "index": index,
                "json": "",
                "initial": block.get("input", {}),
                "closed": False,
            }
            yield event(
                "output_item.added", output_index=index, item=copy.deepcopy(item)
            )
        elif kind == "content_block_delta":
            state = blocks[source["index"]]
            item = state["item"]
            delta = source["delta"]
            if delta["type"] == "text_delta" and state["spec"]["type"] == "text":
                item["content"][0]["text"] += delta["text"]
                yield event(
                    "output_text.delta",
                    item_id=item["id"],
                    output_index=state["index"],
                    content_index=0,
                    delta=delta["text"],
                )
            elif (
                delta["type"] == "input_json_delta" and state["spec"]["type"] != "text"
            ):
                state["json"] += delta["partial_json"]
                if state["spec"]["type"] == "function":
                    yield event(
                        "function_call_arguments.delta",
                        item_id=item["id"],
                        output_index=state["index"],
                        delta=delta["partial_json"],
                    )
            else:
                raise ProtocolError("unsupported upstream delta")
        elif kind == "content_block_stop":
            state = blocks[source["index"]]
            item, spec = state["item"], state["spec"]
            if spec["type"] != "text":
                args = json.loads(state["json"]) if state["json"] else state["initial"]
                if not isinstance(args, dict):
                    raise ProtocolError("upstream tool input must be an object")
                if spec["type"] == "structured":
                    from jsonschema import Draft202012Validator

                    if not Draft202012Validator(spec["schema"]).is_valid(args):
                        raise ProtocolError(
                            "structured output does not match the requested schema"
                        )
                    item["content"][0]["text"] = json.dumps(args, ensure_ascii=False)
                    yield event(
                        "output_text.delta",
                        item_id=item["id"],
                        output_index=state["index"],
                        content_index=0,
                        delta=item["content"][0]["text"],
                    )
                elif spec["type"] == "custom":
                    if not isinstance(args.get("input"), str):
                        raise ProtocolError("custom tool requires a string input")
                    item["input"] = args["input"]
                else:
                    item["arguments"] = json.dumps(args, ensure_ascii=False)
            item["status"] = "completed"
            state["closed"] = True
            yield event(
                "output_item.done",
                output_index=state["index"],
                item=copy.deepcopy(item),
            )
        elif kind == "message_delta":
            usage.update(
                {k: v for k, v in source.get("usage", {}).items() if v is not None}
            )
            stop_reason = source.get("delta", {}).get("stop_reason", source.get("stop_reason", stop_reason))
        elif kind == "message_stop":
            if (
                not started
                or not output
                or any(not s["closed"] for s in blocks.values())
            ):
                raise ProtocolError("incomplete upstream message")
            if stop_reason not in {"end_turn", "tool_use", "stop_sequence"}:
                raise ProtocolError(
                    "upstream generation stopped without a complete result"
                )
            input_tokens = (
                usage.get("input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
            )
            output_tokens = usage.get("output_tokens", 0)
            yield event(
                "completed",
                response={
                    "id": response_id,
                    "object": "response",
                    "status": "completed",
                    "output": output,
                    "usage": {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "total_tokens": input_tokens + output_tokens,
                        "input_tokens_details": {
                            "cached_tokens": usage.get("cache_read_input_tokens", 0)
                        },
                    },
                },
            )
            return
        else:
            raise ProtocolError(f"unsupported upstream event: {kind}")
    raise ProtocolError("upstream stream ended before message_stop")


class AnthropicBridge:
    def __init__(
        self, config: LGConfig, *, stream_factory: Callable | None = None
    ) -> None:
        self.config = config
        self.token = secrets.token_urlsafe(32)
        self.stream_factory = stream_factory
        self.client = None
        self.server = None
        self.thread = None

    def __enter__(self) -> Self:
        if self.stream_factory is None:
            self.client = self.open_client()
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                self.connection.settimeout(bridge.config.timeout_seconds)
                if not secrets.compare_digest(
                    self.headers.get("Authorization", ""), "Bearer " + bridge.token
                ):
                    self.send_error(401, "LG bridge authentication required")
                    return
                if self.path not in {"/responses", "/v1/responses"}:
                    self.send_error(404, "Unsupported LG bridge endpoint")
                    return
                streaming = False
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 8 * 1024 * 1024:
                        raise ProtocolError("invalid request size")
                    request = json.loads(self.rfile.read(size))
                    if (
                        not isinstance(request, dict)
                        or request.get("stream") is not True
                    ):
                        raise ProtocolError(
                            "only streaming Responses requests are supported"
                        )
                    models = {
                        bridge.config.default_model,
                        bridge.config.writer_model,
                        bridge.config.critic_model,
                        bridge.config.coder_model,
                    }
                    models.discard("")
                    if models and request.get("model") not in models:
                        raise ProtocolError(
                            "requested model is not configured for this LG provider"
                        )
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    streaming = True
                    for event in bridge.response_events(request):
                        self.emit(event)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as exc:  # noqa: BLE001 -- sanitize every provider error at the HTTP boundary
                    # Provider exception bodies may contain credentials or user text.
                    message = (
                        str(exc)
                        if isinstance(exc, ProtocolError)
                        else "Provider upstream request failed; check credentials, endpoint, and provider availability"
                    )
                    if streaming:
                        try:
                            self.emit(
                                {
                                    "type": "response.failed",
                                    "response": {
                                        "error": {
                                            "code": "invalid_prompt",
                                            "message": message,
                                        }
                                    },
                                }
                            )
                        except OSError:
                            pass
                    else:
                        self.send_error(400, "Invalid or unsupported bridge request")

            def emit(self, event: dict) -> None:
                data = json.dumps(event, ensure_ascii=False)
                self.wfile.write(f"event: {event['type']}\ndata: {data}\n\n".encode())
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"
        return self

    def open_client(self):
        from anthropic import Anthropic, DefaultHttpxClient

        return Anthropic(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            timeout=self.config.timeout_seconds,
            max_retries=0,
            http_client=DefaultHttpxClient(follow_redirects=False, trust_env=False),
        )

    def response_events(self, request: dict) -> Iterable[dict]:
        payload, mapping = translate_request(request, self.config.max_output_tokens)
        if self.config.provider == "stepfun":
            # Only documented StepFun Messages fields may cross this boundary.
            payload.pop("thinking", None)
            payload.pop("tool_choice", None)
            if request.get("tool_choice", "auto") != "auto":
                raise ProtocolError("StepFun Messages does not document forced tool_choice")
        for event in translate_stream(self.events(payload), mapping):
            if event["type"] == "response.completed":
                self.validate_final_schema(request, event["response"])
            yield event

    @staticmethod
    def validate_final_schema(request: dict, response: dict) -> None:
        spec = (request.get("text") or {}).get("format") or {}
        output = response.get("output", [])
        if spec.get("type") != "json_schema" or any(i.get("type") in {"function_call", "custom_tool_call"} for i in output):
            return
        from jsonschema import Draft202012Validator

        # A schema tool result may follow a separate progress message.
        finals = [i for i in output if i.get("phase") == "final_answer"]
        content = "".join(p.get("text", "") for i in (finals or output) if i.get("type") == "message" for p in i.get("content", []) if p.get("type") == "output_text")
        try:
            value = json.loads(content)
        except (ValueError, TypeError):
            raise ProtocolError("Provider did not return the required structured final result") from None
        if not Draft202012Validator(spec["schema"]).is_valid(value):
            raise ProtocolError("Provider final result does not match the requested schema")

    def events(self, payload: dict) -> Iterable[dict]:
        if self.stream_factory is not None:
            yield from self.stream_factory(payload)
        else:
            with self.client.messages.create(**payload) as stream:
                for event in stream:
                    yield event.model_dump(exclude_none=True)

    def __exit__(self, *args: object) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.client:
            self.client.close()
        if self.thread:
            self.thread.join(timeout=2)

    def codex_args(self) -> list[str]:
        settings = {
            "model_provider": "lg_anthropic",
            "model_providers.lg_anthropic.name": f"LiteraryGiant / {self.config.provider}",
            "model_providers.lg_anthropic.base_url": self.base_url,
            "model_providers.lg_anthropic.env_key": "LG_BRIDGE_TOKEN",
            "model_providers.lg_anthropic.wire_api": "responses",
            "model_providers.lg_anthropic.requires_openai_auth": False,
            "model_providers.lg_anthropic.supports_websockets": False,
            "model_providers.lg_anthropic.request_max_retries": 0,
            "model_providers.lg_anthropic.stream_max_retries": 0,
            "model_reasoning_effort": "none",
            "model_supports_reasoning_summaries": False,
            "web_search": "disabled",
            "analytics.enabled": False,
            "feedback.enabled": False,
            "check_for_update_on_startup": False,
            "features.plugins": False,
            "features.apps": False,
            "features.recommended_plugins": False,
            "model_catalog_json": str(self.model_catalog()),
        }
        return [
            arg
            for key, value in settings.items()
            for arg in ("-c", f"{key}={json.dumps(value)}")
        ]

    def model_catalog(self):
        instructions = (
            resources.files("lg_cli.resources")
            .joinpath("native-writing.txt")
            .read_text(encoding="utf-8")
        )
        names = list(
            dict.fromkeys(
                filter(
                    None,
                    [
                        self.config.default_model,
                        self.config.writer_model,
                        self.config.critic_model,
                        self.config.coder_model,
                    ],
                )
            )
        )
        models = [
            {
                "slug": name,
                "display_name": name,
                "description": f"LG writing model / {self.config.provider}",
                "default_reasoning_level": "none",
                "supported_reasoning_levels": [
                    {"effort": "none", "description": "Standard writing"}
                ],
                "shell_type": "unified_exec"
                if self.config.enable_shell
                else "disabled",
                "visibility": "list",
                "supported_in_api": True,
                "priority": index,
                "availability_nux": None,
                "upgrade": None,
                "support_verbosity": False,
                "default_verbosity": None,
                "apply_patch_tool_type": None,
                "truncation_policy": {"mode": "tokens", "limit": 10000},
                # A conservative local working budget, not a provider capacity claim.
                "context_window": 64000,
                "auto_compact_token_limit": 50000,
                "experimental_supported_tools": [],
                "input_modalities": ["text"],
                "supports_reasoning_summary_parameter": False,
                "supports_search_tool": False,
                "include_apps_usage_instructions": False,
                "tool_mode": "direct",
                "base_instructions": instructions,
            }
            for index, name in enumerate(names)
        ]
        content = json.dumps({"models": models}, ensure_ascii=False)
        identity = hashlib.sha256(content.encode()).hexdigest()[:16]
        directory = self.config.workspace / ".literarygiant" / "codex-home"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / f"lg-models-{identity}.json"
        if not path.exists():
            # File identity is content-addressed; concurrent sessions use the same bytes.
            import os
            import tempfile

            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=directory, delete=False
            ) as output:
                output.write(content)
                temporary = output.name
            os.replace(temporary, path)
        return path
