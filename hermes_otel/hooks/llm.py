"""LLM hooks: the per-turn ``llm.<model>`` span and the per-request ``api.<model>`` spans."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from ..debug_utils import debug_log
from ..helpers import (
    coerce_bool,
    http_status_class,
    redact_export_text,
    sanitize_export_value,
    serialize_full,
    to_optional_int,
    truncate_string,
)
from ._common import (
    _as_dict,
    _fail_open,
    _mark_truncated,
    _preview_for,
    _preview_marked,
    get_tracer,
)
from .attributes import (
    _correlation_attributes,
    _credential_identity_attributes,
    _gen_ai_attributes,
    _gen_ai_request_param_attributes,
    _message_json,
    _metric_model_labels,
    _model_attributes,
    _provider_attributes,
    _response_model_attributes,
    _sender_attributes,
    _serialize_conversation_history,
    _session_context_attributes,
    _session_identity_attributes,
    _turn_attributes,
)
from .request_logs import emit_api_request_log
from .session import _start_session_span
from .usage import (
    _USAGE_FIELDS,
    _estimate_cost,
    _genai_metric_dims,
    _normalize_usage,
    _record_genai_token_usage,
    _record_prompt_cache_metrics,
    _record_usage_metrics,
    _usage_attributes,
)


def _llm_input_attributes(tracer, user_message: Any) -> Dict[str, Any]:
    """``input.value`` + ``gen_ai.input.messages`` for the latest user message."""
    attrs: Dict[str, Any] = {}
    preview, original = _preview_marked(tracer, "llm_input", user_message)
    if preview is not None:
        attrs["input.value"] = preview
        messages = _message_json("user", preview)
        if messages is not None:
            attrs["gen_ai.input.messages"] = messages
        _mark_truncated(attrs, "input", original)
    return attrs


@_fail_open
def on_pre_llm_call(
    session_id: str,
    user_message: str,
    conversation_history: list,
    is_first_turn: bool,
    model: str,
    platform: str,
    **kwargs,
):
    """Start an LLM span before the model is called."""
    debug_log(f"pre_llm_call fired: model={model}, session={session_id}")
    tracer = get_tracer()
    debug_log(f"  tracer.is_enabled={tracer.is_enabled}")
    if not tracer.is_enabled:
        return None
    tracer.sweep_expired_turns()
    if session_id and not tracer.spans.has_span(f"session:{session_id}"):
        _start_session_span(session_id, model, platform, kwargs, synthesized=True)
    key = f"llm:{session_id}"
    tracer.sessions.next_turn(session_id, reset=bool(is_first_turn))
    if session_id:
        ps = tracer.sessions.get_or_create(session_id)
        if not ps.io_captured:
            ps.io["input"] = _preview_for(tracer, "llm_input", user_message) or ""
            ps.io_captured = True
    known = tracer.sessions.peek(session_id) if session_id else None
    attributes: Dict[str, Any] = {}
    attributes.update(_session_identity_attributes(session_id))
    attributes.update(_model_attributes(model, known.provider if known else None))
    attributes.update(_gen_ai_attributes(session_id, "chat"))
    attributes.update(_correlation_attributes(tracer, session_id, kwargs))
    attributes.update(_turn_attributes(tracer, session_id))
    if tracer.config.capture_sender_id:
        sender_id = truncate_string(kwargs.get("sender_id"), 200) if kwargs.get("sender_id") else ""
        if sender_id:
            sender_attrs = _sender_attributes(sender_id, truncate_string(platform, 120))
            attributes.update(sender_attrs)
            if session_id:
                ps = tracer.sessions.get_or_create(session_id)
                ps.sender_id = sender_id
                ps.user_id = sender_attrs["user.id"]
    full = None
    if tracer.config.capture_conversation_history and tracer.config.capture_previews:
        full = _serialize_conversation_history(
            conversation_history, tracer.config.conversation_history_max_chars
        )
    if full is not None:
        attributes["input.value"] = full
        attributes["input.mime_type"] = "application/json"
        attributes["gen_ai.input.messages"] = full
        attributes["hermes.conversation.message_count"] = len(conversation_history)
    else:
        attributes.update(_llm_input_attributes(tracer, user_message))
    span = tracer.start_span(
        name=f"llm.{model}",
        key=key,
        kind="llm",
        attributes=attributes,
        session_id=session_id,
    )
    tracer.spans.push_parent(span, session_id=session_id)
    debug_log(f"  LLM span started: key={key}")
    return None


@_fail_open
def on_post_llm_call(
    session_id: str,
    user_message: str,
    assistant_response: str,
    conversation_history: list,
    model: str,
    platform: str,
    **kwargs,
):
    """End the LLM span and record the response."""
    debug_log(f"post_llm_call fired: model={model}, session={session_id}")
    tracer = get_tracer()
    debug_log(f"  tracer.is_enabled={tracer.is_enabled}")
    if not tracer.is_enabled:
        return
    key = f"llm:{session_id}"
    debug_log(f"  ending span: key={key}")
    preview, original = _preview_marked(tracer, "llm_output", assistant_response)
    ps = tracer.sessions.peek(session_id) if session_id else None
    if ps is not None and ps.io_captured:
        ps.io["output"] = preview or ""
    provider = ps.provider if ps is not None else ""
    tracer.record_metric("message_count", 1, _metric_model_labels(model, provider))
    attributes: Dict[str, Any] = {}
    attributes.update(_session_identity_attributes(session_id))
    attributes.update(_response_model_attributes(ps.response_model if ps is not None else ""))
    attributes.update(_model_attributes(None, provider))
    attributes.update(_gen_ai_attributes(session_id, "chat"))
    attributes.update(_correlation_attributes(tracer, session_id, kwargs))
    if preview is not None:
        attributes["output.value"] = preview
        messages = _message_json("assistant", preview)
        if messages is not None:
            attributes["gen_ai.output.messages"] = messages
        _mark_truncated(attributes, "output", original)
    tracer.spans.pop_parent(session_id=session_id)
    tracer.end_span(key, attributes=attributes, status="ok")
    debug_log("  LLM span ended: status=ok")


def _request_body(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    request = kwargs.get("request")
    if isinstance(request, dict) and isinstance(request.get("body"), dict):
        return request["body"]
    return {}


def _system_prompt(kwargs: Dict[str, Any], messages: Any, body: Dict[str, Any]) -> Optional[str]:
    """The system prompt: an explicit kwarg, else the leading system message,
    else the Responses API ``instructions`` field."""
    explicit = kwargs.get("system_prompt")
    if explicit:
        return str(explicit)
    if isinstance(messages, list):
        for m in messages:
            if isinstance(m, dict) and m.get("role") in ("system", "developer"):
                content = m.get("content")
                if isinstance(content, list):
                    content = " ".join(
                        str(part.get("text", "")) if isinstance(part, dict) else str(part)
                        for part in content
                    )
                return str(content) if content else None
            break
    instructions = body.get("instructions") or body.get("system")
    return str(instructions) if instructions else None


@_fail_open
def on_pre_api_request(
    task_id: str,
    session_id: str,
    platform: str,
    model: str,
    provider: str,
    base_url: str,
    api_mode: str,
    api_call_count: int,
    message_count: int,
    tool_count: int,
    approx_input_tokens: int,
    request_char_count: int,
    max_tokens: int,
    **kwargs,
):
    """Fires before each individual LLM API request."""
    debug_log(f"pre_api_request fired: model={model}, provider={provider}, session={session_id}")
    tracer = get_tracer()
    debug_log(f"  tracer.is_enabled={tracer.is_enabled}")
    if not tracer.is_enabled:
        return
    tracer.sweep_expired_turns()
    key = f"api:{task_id}"
    if session_id:
        tracer.sessions.get_or_create(session_id).turn_summary.api_call_count += 1
    attributes: Dict[str, Any] = {
        "llm.api_mode": api_mode,
        "llm.request.message_count": message_count,
        "llm.request.approx_input_tokens": approx_input_tokens,
    }
    attributes.update(_session_identity_attributes(session_id))
    attributes.update(_model_attributes(model, provider))
    identity_labels = _credential_identity_attributes(kwargs)
    attributes.update(identity_labels)
    attributes.update(_gen_ai_attributes(session_id, "chat"))
    attributes.update(_gen_ai_request_param_attributes(kwargs))
    attributes.update(_session_context_attributes(tracer, session_id, kwargs))
    if max_tokens:
        attributes["llm.request.max_tokens"] = max_tokens
        attributes["gen_ai.request.max_tokens"] = max_tokens
    if tracer.config.capture_previews and tracer.config.capture_full_prompts:
        messages = kwargs.get("request_messages") or kwargs.get("messages")
        body = _request_body(kwargs)
        if not messages:
            messages = body.get("messages")
        serialized = serialize_full(messages)
        if serialized is not None:
            attributes["gen_ai.input.messages"] = serialized
            attributes["input.value"] = serialized
            attributes["input.mime_type"] = "application/json"
            attributes["hermes.content.input_chars"] = len(serialized)
        system_prompt = _system_prompt(kwargs, messages, body)
        if system_prompt:
            attributes["gen_ai.system_instructions"] = redact_export_text(system_prompt)
    span = tracer.start_span(
        name=f"api.{model}",
        key=key,
        kind="llm",
        attributes=attributes,
        session_id=session_id,
    )
    tracer.spans.push_parent(span, session_id=session_id)
    tracer.record_metric(
        "api_request_count",
        1,
        {**_metric_model_labels(model, provider), **identity_labels, "api_mode": api_mode},
    )
    emit_api_request_log(
        "request",
        task_id=task_id,
        session_id=session_id,
        provider=provider,
        model=model,
        api_mode=api_mode,
        api_call_count=api_call_count,
        **kwargs,
    )
    debug_log(f"  API span started: key={key}")


@_fail_open
def on_post_api_request(
    task_id: str,
    session_id: str,
    platform: str,
    model: str,
    provider: str,
    base_url: str,
    api_mode: str,
    api_call_count: int,
    api_duration: float,
    finish_reason: str,
    message_count: int,
    response_model: str,
    usage: dict,
    assistant_content_chars: int,
    assistant_tool_call_count: int,
    **kwargs,
):
    """Fires after each individual LLM API request with usage stats."""
    debug_log(f"post_api_request fired: model={model}, finish={finish_reason}")
    tracer = get_tracer()
    debug_log(f"  tracer.is_enabled={tracer.is_enabled}")
    if not tracer.is_enabled:
        return
    key = f"api:{task_id}"
    debug_log(f"  ending span: key={key}, usage={usage}")
    attributes: Dict[str, Any] = {}
    attributes.update(_gen_ai_attributes(session_id, "chat"))
    attributes.update(_provider_attributes(provider))
    identity_labels = _credential_identity_attributes(kwargs)
    attributes.update(identity_labels)
    attributes.update(_response_model_attributes(response_model))
    response_id = kwargs.get("response_id") or kwargs.get("id")
    if response_id:
        attributes["gen_ai.response.id"] = truncate_string(response_id, 200)
    attributes.update(_correlation_attributes(tracer, session_id, kwargs))
    model_labels = {**_metric_model_labels(model, provider), **identity_labels}
    usage = _as_dict(usage)
    if usage:
        totals = _normalize_usage(usage)
        attributes.update(_usage_attributes(totals))
        if session_id:
            ps = tracer.sessions.get_or_create(session_id)
            for field in _USAGE_FIELDS:
                ps.usage[field] += totals[field]
            ps.usage_updated = True
            if provider:
                ps.provider = provider
            if response_model:
                ps.response_model = truncate_string(response_model, 200)
        _record_usage_metrics(tracer, totals, model_labels)
        _record_prompt_cache_metrics(
            tracer,
            totals,
            usage.get("available_fields"),
            {**model_labels, "api_mode": api_mode},
        )
        _record_genai_token_usage(
            tracer,
            "gen_ai.client.token.usage",
            totals,
            {**_genai_metric_dims(model, provider, response_model), **identity_labels},
        )
        # Cost: Hermes's own estimate for this call (#252). An unknown price
        # or a subscription-included route carries a status but no amount, so
        # a missing price never reads as $0.
        cost = _estimate_cost(model, provider, base_url, usage)
        if cost is not None:
            attributes["hermes.cost.status"] = cost["status"]
            if cost["source"]:
                attributes["hermes.cost.source"] = cost["source"]
            if cost["usd"] is not None:
                attributes["hermes.cost.usage"] = cost["usd"]
                tracer.record_metric(
                    "cost_usage", cost["usd"], {**model_labels, "cost_status": cost["status"]}
                )
            if session_id:
                tracer.sessions.get_or_create(session_id).add_cost(cost["status"], cost["usd"])

        tracer.record_metric("model_usage", 1, model_labels)
    if api_duration:
        attributes["llm.response.duration_ms"] = round(api_duration * 1000, 1)
        tracer.record_metric(
            "gen_ai.client.operation.duration",
            api_duration,
            {**_genai_metric_dims(model, provider, response_model), **identity_labels},
        )
    if finish_reason:
        attributes["llm.response.finish_reason"] = finish_reason
        attributes["gen_ai.response.finish_reasons"] = [finish_reason]
    if assistant_content_chars:
        attributes["llm.response.output_chars"] = assistant_content_chars
    if assistant_tool_call_count:
        attributes["llm.response.tool_calls"] = assistant_tool_call_count
    if tracer.config.capture_previews and tracer.config.capture_full_responses:
        response_content = kwargs.get("response_content")
        response_tool_calls = kwargs.get("response_tool_calls")
        assistant_message = kwargs.get("assistant_message")
        if assistant_message is not None:
            if not response_content:
                response_content = getattr(assistant_message, "content", None)
            if not response_tool_calls:
                response_tool_calls = getattr(assistant_message, "tool_calls", None)
        message: Dict[str, Any] = {"role": "assistant"}
        if response_content:
            message["content"] = redact_export_text(str(response_content))
        tool_calls_serialized = serialize_full(response_tool_calls)
        if tool_calls_serialized is not None:
            try:
                message["tool_calls"] = json.loads(tool_calls_serialized)
            except Exception:
                message["tool_calls"] = tool_calls_serialized
        if len(message) > 1:
            attributes["gen_ai.output.messages"] = json.dumps([message], ensure_ascii=False)
            if response_content:
                attributes["output.value"] = redact_export_text(str(response_content))
                attributes["output.mime_type"] = "text/plain"
            else:
                attributes["output.value"] = tool_calls_serialized
                attributes["output.mime_type"] = "application/json"
            attributes["hermes.content.output_chars"] = len(attributes["output.value"] or "")
    tracer.spans.pop_parent(session_id=session_id)
    emit_api_request_log(
        "response",
        task_id=task_id,
        session_id=session_id,
        provider=provider,
        model=model,
        api_mode=api_mode,
        api_call_count=api_call_count,
        finish_reason=finish_reason,
        **kwargs,
    )
    tracer.end_span(key, attributes=attributes, status="ok")
    debug_log(f"  API span ended: status=ok, tokens={usage.get('total_tokens', 0) if usage else 0}")


@_fail_open
def on_api_request_error(
    task_id: str = None,
    session_id: str = None,
    platform: str = None,
    model: str = None,
    provider: str = None,
    api_duration: float = None,
    status_code: Any = None,
    retry_count: Any = None,
    max_retries: Any = None,
    retryable: Any = None,
    reason: str = None,
    error: dict = None,
    **kwargs,
):
    """Fires when a provider API request fails; closes the span and records error/retry metrics."""
    debug_log(
        f"api_request_error fired: model={model}, status_code={status_code}, "
        f"retry_count={retry_count}, retryable={retryable}"
    )
    tracer = get_tracer()
    if not tracer.is_enabled:
        return
    tracer.sweep_expired_turns()
    if isinstance(error, BaseException):
        error = {"type": type(error).__name__, "message": str(error)}
    error = _as_dict(error)
    error_type = truncate_string(error.get("type"), 200) if error.get("type") else ""
    error_message = truncate_string(redact_export_text(error.get("message") or reason or ""), 500)
    status_class = http_status_class(status_code)
    is_retryable = coerce_bool(retryable)
    identity_labels = _credential_identity_attributes(kwargs)
    attributes: Dict[str, Any] = {}
    if error_type:
        attributes["error.type"] = error_type
    sc = to_optional_int(status_code)
    if sc is not None:
        attributes["http.response.status_code"] = sc
        attributes["gen_ai.response.status_code"] = sc
    rc = to_optional_int(retry_count)
    if rc is not None:
        attributes["hermes.retry.count"] = rc
    mr = to_optional_int(max_retries)
    if mr is not None:
        attributes["hermes.max_retries"] = mr
    if is_retryable is not None:
        attributes["hermes.retryable"] = is_retryable
    if api_duration:
        attributes["llm.response.duration_ms"] = round(api_duration * 1000, 1)
    attributes.update(_gen_ai_attributes(session_id, "chat"))
    attributes.update(_provider_attributes(provider or platform))
    attributes.update(identity_labels)
    attributes.update(_correlation_attributes(tracer, session_id, kwargs))
    if session_id and error_type:
        ps = tracer.sessions.peek(session_id)
        if ps is not None:
            ps.last_error_type = error_type
    key = f"api:{task_id}" if task_id else None
    span = tracer.spans.get_span(key) if key else None
    created_fallback = False
    if span is None:
        fallback_key = key or f"api.error:{kwargs.get('api_request_id') or session_id or 'unknown'}"
        identity: Dict[str, Any] = {}
        identity.update(_session_identity_attributes(session_id))
        identity.update(_model_attributes(model, provider))
        identity.update(identity_labels)
        span = tracer.start_span(
            name=f"api.{model}" if model else "api.error",
            key=fallback_key,
            kind="llm",
            attributes=identity,
            session_id=session_id,
        )
        key = fallback_key
        created_fallback = True
    add_event = getattr(span, "add_event", None) if span is not None else None
    if callable(add_event):
        event_attrs: Dict[str, Any] = {
            "exception.type": error_type or "error",
            "exception.escaped": True,
        }
        if error_message:
            event_attrs["exception.message"] = error_message
        try:
            add_event("exception", sanitize_export_value(event_attrs))
        except Exception:
            pass
    if not created_fallback:
        tracer.spans.pop_parent(session_id=session_id)
    if key is None:
        return
    emit_api_request_log(
        "error",
        **{
            **kwargs,
            "task_id": task_id,
            "session_id": session_id,
            "provider": provider,
            "model": model,
            "api_mode": kwargs.get("api_mode"),
            "retry_count": retry_count,
            "status_code": sc,
            "status_class": status_class,
            "retryable": is_retryable,
            "max_retries": mr,
            "error_type": error_type,
            "_otel_span": span,
        },
    )
    tracer.end_span(
        key,
        attributes=attributes,
        status="error",
        error_message=error_message
        or redact_export_text(reason or error_type or "api request failed"),
    )
    model_labels = {**_metric_model_labels(model, provider), **identity_labels}
    metric_attrs: Dict[str, Any] = {
        "status_class": status_class,
        "retryable": "unknown" if is_retryable is None else str(is_retryable).lower(),
        **model_labels,
    }
    metric_attrs["error_type"] = error_type or "unknown"
    tracer.record_metric("api_error_count", 1, metric_attrs)
    if is_retryable:
        tracer.record_metric("retry_count", 1, dict(model_labels))
    if api_duration:
        duration_dims = {**_genai_metric_dims(model, provider), **identity_labels}
        duration_dims["error.type"] = error_type or "unknown"
        tracer.record_metric("gen_ai.client.operation.duration", api_duration, duration_dims)
    debug_log(f"  API error span ended: key={key}, error.type={error_type}, class={status_class}")
