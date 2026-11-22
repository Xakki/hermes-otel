"""Provider request events on the existing structured OTel log pipeline."""

from __future__ import annotations

from typing import Any, Dict, Optional

from .. import log_events as EV
from ..redaction import redact_content
from ._common import _emit_event
from .api_call import HookPayload

_LEVEL_CONTENT = {"debug": "full", "info": "preview", "notice": "off"}
_REQUEST_KEYS = ("input.value", "gen_ai.system_instructions")
_RESPONSE_KEYS = ("gen_ai.output.messages",)
_HARD_CONTENT_LIMIT = 128_000


def _mode(cfg) -> Optional[str]:
    ceiling = _LEVEL_CONTENT.get(cfg.level)
    if ceiling is None:
        return None
    span_mode = EV.narrower(cfg.content_mode(), cfg.content_capture)
    events_mode = EV.resolve_content_mode(cfg.log_events_content, span_mode)
    return EV.narrower(ceiling, EV.narrower(span_mode, events_mode))


def request_event_content(tracer, attrs: Dict[str, Any]) -> Dict[str, Any]:
    """Apply the request level as a ceiling, never as permission to capture."""
    cfg = tracer.config
    mode = _mode(cfg)
    if mode is None:
        return attrs
    if mode == "off":
        return EV.apply_content_mode(attrs, mode, cfg.preview_max_chars)
    # Redact before clipping so the complete credential shape remains visible
    # to the redactor. Each backend can narrow the structured event again.
    safe = {
        key: (
            redact_content(value)[:_HARD_CONTENT_LIMIT]
            if key in EV.CONTENT_ATTRIBUTES and isinstance(value, str)
            else value
        )
        for key, value in attrs.items()
    }
    return EV.apply_content_mode(safe, mode, cfg.preview_max_chars)


def _provider_user_text(payload: HookPayload) -> Optional[str]:
    """Only preview plain text from the last user message sent to the provider."""
    messages = payload.get("request_messages")
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if not isinstance(message, dict):
            return None
        if message.get("role") == "user":
            content = message.get("content")
            return content if isinstance(content, str) and content else None
    return None


def emit_request_event(
    tracer,
    *,
    response: bool,
    payload: HookPayload,
    attributes: Dict[str, Any],
    metadata: Dict[str, Any],
    session_id: Optional[str],
    span: Any,
) -> None:
    """Reuse span content and recognized keys, not raw options or Python log extras."""
    cfg = tracer.config
    mode = _mode(cfg)
    if not cfg.log_events or mode is None:
        return
    attrs = dict(metadata)
    keys = _RESPONSE_KEYS if response else _REQUEST_KEYS
    if mode != "off":
        attrs.update({key: attributes[key] for key in keys if key in attributes})
        if not any(key in attrs for key in keys):
            # Preview-only settings never build full span bodies. Read only
            # the text field, not transport options or tool payloads.
            if response:
                message = payload.get("assistant_message")
                text = (
                    message.get("content")
                    if isinstance(message, dict)
                    else getattr(message, "content", None)
                )
            else:
                text = _provider_user_text(payload)
            if isinstance(text, str):
                key = "output.value" if response else "input.value"
                attrs[key] = EV.apply_content_mode(
                    {key: redact_content(text)}, "preview", cfg.preview_max_chars
                )[key]
    name = EV.API_RESPONSE if response else EV.API_REQUEST
    _emit_event(
        tracer, name, name, request_event_content(tracer, attrs), session_id=session_id, span=span
    )
