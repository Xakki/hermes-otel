"""Bounded, identity-bearing API request lifecycle log events.

The configured ``level`` is an explicit content boundary: debug carries full
bodies up to a hard cap, info carries short previews, and notice emits no body
events. All payloads pass through the shared export redactor.
"""

from __future__ import annotations

import logging
from contextlib import nullcontext
from typing import Any, Dict, Optional, Tuple

from opentelemetry.trace import use_span

from ..helpers import serialize_full
from ._common import get_tracer

_LOG = logging.getLogger("hermes_otel.api_request")
_HARD_BODY_LIMIT = 128_000
# Graylog drops an entire OTLP log record when a single text attribute grows
# beyond its indexed-field limit. Keep each part below that limit in UTF-8 bytes.
_BODY_PART_BYTES = 8_192
_TRANSPORT_HEADER_KEYS = frozenset(
    {
        "headers",
        "extra_headers",
        "request_headers",
        "http_headers",
        "authorization",
        "proxy_authorization",
        "cookie",
        "set_cookie",
    }
)


def _without_transport_headers(values: Dict[str, Any]) -> Dict[str, Any]:
    """Exclude transport credentials; they are not model request content."""
    return {
        key: value
        for key, value in values.items()
        if str(key).lower().replace("-", "_") not in _TRANSPORT_HEADER_KEYS
    }


def _bounded_json(value: Any, limit: int) -> Tuple[Optional[str], int, bool]:
    serialized = serialize_full(value)
    if serialized is None:
        return None, 0, False
    original_chars = len(serialized)
    if original_chars <= limit:
        return serialized, original_chars, False
    marker = "...[truncated]"
    if limit <= len(marker):
        return serialized[:limit], original_chars, True
    return serialized[: max(0, limit - len(marker))] + marker, original_chars, True


def _request_payload(
    kwargs: Dict[str, Any], *, detail: bool = False
) -> Tuple[Dict[str, Any], bool]:
    request = kwargs.get("request")
    payload = _without_transport_headers(request) if isinstance(request, dict) else {}
    body = payload.get("body")
    body = dict(body) if isinstance(body, dict) else {}
    body = _without_transport_headers(body)
    source_truncated = bool(payload.get("_truncated"))
    if detail:
        options = kwargs.get("request_options")
        if isinstance(options, dict):
            source_truncated = bool(options.get("_truncated"))
            payload = {"method": payload.get("method", "POST")}
            body = (
                {"_options_preview": options.get("preview", ""), "_options_truncated": True}
                if source_truncated
                else _without_transport_headers(options)
            )
        else:
            # An older Hermes host offers only the ordinary, size-capped view.
            source_truncated = True
        for key in ("messages", "input", "system", "instructions", "system_prompt"):
            body.pop(key, None)
    messages = kwargs.get("request_messages") or kwargs.get("messages")
    if messages is not None:
        body["messages"] = messages
    system_prompt = kwargs.get("system_prompt")
    if system_prompt is not None:
        body["system_prompt"] = system_prompt
    payload["body"] = body
    return payload, source_truncated


def _base_attributes(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    attrs: Dict[str, Any] = {"hermes_api_event": True}
    for key in (
        "api_request_id",
        "credential_id",
        "account_name",
        "provider",
        "model",
        "api_mode",
        "api_call_count",
        "retry_count",
    ):
        value = kwargs.get(key)
        if value is not None and value != "":
            attrs[key] = value
    return attrs


def _emit_body_log(message: str, attributes: Dict[str, Any], body_key: str, level: int) -> None:
    body = attributes.get(body_key)
    if not isinstance(body, str) or len(body.encode("utf-8")) <= _BODY_PART_BYTES:
        _LOG.log(level, message, extra=attributes)
        return
    encoded = body.encode("utf-8")
    parts = []
    offset = 0
    while offset < len(encoded):
        end = min(offset + _BODY_PART_BYTES, len(encoded))
        while end < len(encoded) and encoded[end] & 0xC0 == 0x80:
            end -= 1
        parts.append(encoded[offset:end].decode("utf-8"))
        offset = end
    for index, part in enumerate(parts, start=1):
        _LOG.log(
            level,
            message,
            extra={
                **attributes,
                body_key: part,
                "body_part_index": index,
                "body_part_count": len(parts),
            },
        )


def emit_api_request_log(event: str, **kwargs: Any) -> None:
    """Emit one safe API request/response log event for an enabled level."""
    tracer = get_tracer()
    mode = getattr(tracer.config, "level", None)
    if mode not in ("debug", "info"):
        return

    limit = (
        _HARD_BODY_LIMIT
        if mode == "debug"
        else min(
            _HARD_BODY_LIMIT, max(1, int(getattr(tracer.config, "preview_max_chars", 1200) or 1200))
        )
    )
    attributes = _base_attributes(kwargs)
    attributes["api_event"] = event
    if event == "request":
        request, source_truncated = _request_payload(kwargs, detail=mode == "debug")
        body, original_chars, truncated = _bounded_json(request, limit)
        if body is not None:
            attributes.update(
                request_body=body,
                request_original_chars=original_chars,
                request_truncated=truncated or source_truncated,
            )
        message = "Hermes API request"
    elif event == "response":
        assistant_message = kwargs.get("assistant_message")
        response = {
            "content": kwargs.get("response_content"),
            "tool_calls": kwargs.get("response_tool_calls"),
        }
        if assistant_message is not None and all(value is None for value in response.values()):
            response["assistant_message"] = assistant_message
        body, original_chars, truncated = _bounded_json(response, limit)
        if body is not None:
            attributes.update(
                response_body=body,
                response_original_chars=original_chars,
                response_truncated=truncated,
            )
        finish_reason = kwargs.get("finish_reason")
        if finish_reason:
            attributes["finish_reason"] = finish_reason
        message = "Hermes API response"
    elif event == "error":
        for key in ("error_type", "status_code", "status_class", "retryable", "max_retries"):
            value = kwargs.get(key)
            if value is not None and value != "":
                attributes[key] = value
        message = "Hermes API error"
    else:
        return

    # Hermes dispatches the pre/post hooks independently, sometimes on other
    # threads. Attach the tracked span only for emission, then restore the
    # caller's context; keeping it attached between hooks would leak sessions.
    span = kwargs.get("_otel_span")
    if span is None and kwargs.get("task_id"):
        span = tracer.spans.get_span(f"api:{kwargs['task_id']}")
    context = (
        use_span(span, end_on_exit=False) if hasattr(span, "get_span_context") else nullcontext()
    )
    with context:
        _emit_body_log(
            message,
            attributes,
            "request_body" if event == "request" else "response_body",
            logging.DEBUG if mode == "debug" else logging.INFO,
        )
