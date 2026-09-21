"""Provider transport selection; real credentials remain in the LG process."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import nullcontext

import httpx
from httpx_sse import connect_sse

from .anthropic_bridge import AnthropicBridge, ProtocolError
from .config import LGConfig


class ResponsesBridge(AnthropicBridge):
    """Forward Responses events without pretending Chat Completions is Responses."""

    def open_client(self):
        return httpx.Client(
            timeout=self.config.timeout_seconds, follow_redirects=False, trust_env=False
        )

    def response_events(self, request: dict) -> Iterable[dict]:
        payload = dict(request)
        payload["store"] = False
        payload["max_output_tokens"] = self.config.max_output_tokens
        if (payload.get("reasoning") or {}).get("effort") == "none":
            # Not every Responses provider accepts an explicit `none` enum.
            payload.pop("reasoning", None)
        if self.stream_factory:
            events = self.stream_factory(payload)
            yield from self.validated_events(events, request)
            return
        with connect_sse(
            self.client,
            "POST",
            self.config.base_url.rstrip("/") + "/responses",
            headers={"Authorization": "Bearer " + (self.config.api_key or "")},
            json=payload,
        ) as stream:
            # Do not return upstream error bodies, redirect targets, or headers.
            if stream.response.status_code != 200:
                raise ProtocolError(
                    f"Responses provider returned HTTP {stream.response.status_code}; check endpoint and credentials"
                )
            yield from self.validated_events(
                (event.json() for event in stream.iter_sse() if event.data != "[DONE]"),
                request,
            )

    def validated_events(self, events: Iterable[dict], request: dict) -> Iterable[dict]:
        for event in events:
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ProtocolError("Invalid Responses event")
            kind = event["type"]
            if kind in {"error", "response.failed", "response.incomplete"}:
                raise ProtocolError(
                    "Responses provider failed or returned an incomplete generation"
                )
            if kind == "response.completed":
                response = event.get("response", {})
                if response.get("status") != "completed" or not response.get("output"):
                    raise ProtocolError(
                        "Responses provider did not return a complete result"
                    )
                self.validate_final_schema(request, response)
                yield event
                return
            if not kind.startswith("response."):
                raise ProtocolError("Unsupported Responses event")
            yield event
        raise ProtocolError("Responses stream ended before response.completed")


def provider_bridge(config: LGConfig):
    if config.uses_anthropic:
        # Resolve lazily so tests and embeddings can inject the existing adapter.
        from .anthropic_bridge import AnthropicBridge

        return AnthropicBridge(config)
    if config.uses_responses:
        return ResponsesBridge(config)
    return nullcontext()
