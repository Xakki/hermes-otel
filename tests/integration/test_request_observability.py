"""Request events and counters at the host-hook and live/OTLP boundaries."""

import json
from unittest.mock import Mock

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor

from hermes_otel import hooks, log_events, tracer
from hermes_otel.log_handler import BackendLogFilter, LogRules
from hermes_otel.plugin_config import load_config


@pytest.mark.parametrize(
    ("capture", "level", "events_content", "metrics", "expected_body"),
    [
        ("full", "debug", "full", True, "full"),
        ("full", "info", "full", True, "preview"),
        ("preview", "debug", "full", True, "preview"),
        ("off", "debug", "full", False, "off"),
        ("full", "debug", "off", True, "off"),
        ("full", "notice", "full", True, "off"),
    ],
)
def test_host_request_events_obey_capture_and_backend_rules(
    tmp_path, monkeypatch, capture, level, events_content, metrics, expected_body
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    path.write_text(
        f'content_capture: "{capture}"\nlevel: {level}\npreview_max_chars: 80\n'
        f"metrics: {{enabled: {str(metrics).lower()}}}\n"
        f'logs: {{events: {{enabled: true, content: "{events_content}"}}}}\n'
    )
    plugin = tracer.HermesOTelPlugin(load_config(path))
    monkeypatch.setattr(tracer, "_tracer", plugin)
    assert plugin.init()
    pipeline = plugin.pipeline
    assert pipeline is not None
    provider = pipeline.logger_provider
    backend = Mock()
    provider.add_log_record_processor(
        BackendLogFilter(
            backend,
            LogRules(events_enabled=True, events_content="off"),
            global_events_content=events_content,
            span_content_mode=capture,
        )
    )
    secret = "Bearer private-token-that-must-not-be-exported"
    request = "public " + secret + " longer message " * 12
    identity = {
        "task_id": "task-test",
        "session_id": "session-test",
        "api_request_id": "attempt-test",
        "platform": "cli",
        "model": "model-test",
        "provider": "test",
        "base_url": "",
        "api_mode": "chat",
        "api_call_count": 1,
        "message_count": 1,
        "credential_id": "pool-test",
        "account_name": "account-test",
    }
    hooks.on_pre_api_request(
        **identity,
        tool_count=0,
        approx_input_tokens=10,
        request_char_count=len(request),
        max_tokens=20,
        retry_count=0,
        user_message=request,
        request_messages=[{"role": "user", "content": request}],
        request_options={"authorization": "must-not-copy-options"},
    )
    hooks.on_post_api_request(
        **identity,
        api_duration=0.1,
        finish_reason="stop",
        response_model="model-test",
        usage={"prompt_tokens": 10, "completion_tokens": 2},
        assistant_content_chars=160,
        assistant_tool_call_count=0,
        assistant_message={"content": "assistant-safe " * 20},
    )
    store = pipeline.live_store
    names = {log_events.API_REQUEST, log_events.API_RESPONSE}
    events = {row["event_name"]: row for row in store.logs() if row["event_name"] in names}
    assert events.keys() == names
    before, after = events[log_events.API_REQUEST], events[log_events.API_RESPONSE]
    assert before["trace_id"] and before["trace_id"] == after["trace_id"]
    assert before["span_id"] and before["span_id"] == after["span_id"]
    for row in events.values():
        assert row["attributes"]["api_request_id"] == "attempt-test"
        assert row["attributes"]["credential_id"] == "pool-test"
        serialized = json.dumps(row)
        assert secret not in serialized and "must-not-copy-options" not in serialized
        if expected_body == "off":
            assert not log_events.has_content(row["attributes"])
        else:
            assert log_events.has_content(row["attributes"])
            content = [
                v for k, v in row["attributes"].items() if k in log_events.CONTENT_ATTRIBUTES
            ]
            if expected_body == "preview":
                assert all(len(v) <= 80 for v in content)
            else:
                assert max(map(len, content)) > 80
    backend_events = [call.args[0].log_record for call in backend.on_emit.call_args_list]
    assert names <= {record.event_name for record in backend_events}
    assert all(
        not log_events.has_content(record.attributes)
        for record in backend_events
        if record.event_name in names
    )
    rows = store.metrics()
    if metrics:
        attempts = [row for row in rows if row["name"] == "hermes.api.request.count"]
        assert len(attempts) == 1
        assert attempts[0]["attributes"]["account_name"] == "account-test"
        assert all("api_request_id" not in row["attributes"] for row in rows)
    else:
        assert rows == []


def _capture_request_logs(tmp_path, monkeypatch, config):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    path.write_text("logs: {events: {enabled: true}}\n" + config)
    plugin = tracer.HermesOTelPlugin(load_config(path))
    monkeypatch.setattr(tracer, "_tracer", plugin)
    assert plugin.init()
    pipeline = plugin.pipeline
    assert pipeline is not None
    exporter = InMemoryLogRecordExporter()
    pipeline.logger_provider.add_log_record_processor(
        BackendLogFilter(SimpleLogRecordProcessor(exporter), LogRules(events_enabled=True))
    )
    return pipeline.live_store, exporter


def _request_turn(request_messages):
    identity = {
        "task_id": "task-privacy",
        "session_id": "session-privacy",
        "api_request_id": "request-privacy",
        "platform": "cli",
        "model": "model-test",
        "provider": "test",
        "base_url": "",
        "api_mode": "chat",
        "api_call_count": 1,
        "message_count": 1,
    }
    hooks.on_pre_api_request(
        **identity,
        tool_count=0,
        approx_input_tokens=10,
        request_char_count=64,
        max_tokens=20,
        user_message="original-only-marker",
        request_messages=request_messages,
    )
    hooks.on_post_api_request(
        **identity,
        api_duration=0.1,
        finish_reason="stop",
        response_model="model-test",
        usage={},
        assistant_content_chars=20,
        assistant_tool_call_count=0,
        assistant_message={"content": "provider-reply-marker"},
    )


def _sink_events(store, exporter, names):
    live = {
        row["event_name"]: row["attributes"] for row in store.logs() if row["event_name"] in names
    }
    backend = {
        record.log_record.event_name: dict(record.log_record.attributes)
        for record in exporter.get_finished_logs()
        if record.log_record.event_name in names
    }
    assert live.keys() == names == backend.keys()
    return live, backend


@pytest.mark.parametrize("source", ["rewritten", "missing", "unstructured"])
def test_preview_uses_only_text_in_provider_request(tmp_path, monkeypatch, source):
    store, exporter = _capture_request_logs(
        tmp_path, monkeypatch, 'level: debug\ncontent_capture: "preview"\n'
    )
    if source == "rewritten":
        messages = [
            {"role": "user", "content": "older-provider-message"},
            {"role": "user", "content": "provider-only-marker"},
            {"role": "tool", "content": "tool-only-marker"},
        ]
    elif source == "unstructured":
        messages = [{"role": "user", "content": {"unknown": "no-stringify-marker"}}]
    else:
        messages = []
    _request_turn(messages)
    for events in _sink_events(store, exporter, {log_events.API_REQUEST, log_events.API_RESPONSE}):
        request = events[log_events.API_REQUEST]
        assert request["api_request_id"] == "request-privacy"
        assert events[log_events.API_RESPONSE]["output.value"] == "provider-reply-marker"
        if source == "rewritten":
            assert request["input.value"] == "provider-only-marker", request
        else:
            assert not log_events.has_content(request), request


@pytest.mark.parametrize("level", ["notice", "debug"])
def test_request_content_boundary_survives_enrichment(tmp_path, monkeypatch, level):
    store, exporter = _capture_request_logs(
        tmp_path,
        monkeypatch,
        f'level: {level}\ncontent_capture: "full"\n'
        "common_attributes: {input.value: configured-input-marker, "
        "output.value: configured-output-marker, release: safe-metadata-marker}\n",
    )
    _request_turn([{"role": "user", "content": "provider-only-marker"}])
    names = {log_events.API_REQUEST, log_events.API_RESPONSE, log_events.INFERENCE_DETAILS}
    sinks = _sink_events(store, exporter, names)
    for events in sinks:
        for attrs in events.values():
            assert attrs["release"] == "safe-metadata-marker"
    if level == "notice":
        for name in names:
            contents = [
                {k: attrs[k] for k in log_events.CONTENT_ATTRIBUTES if k in attrs}
                for events in sinks
                for attrs in [events[name]]
            ]
            assert contents == [{}, {}], (name, contents)
    else:
        for events in sinks:
            assert "provider-only-marker" in events[log_events.API_REQUEST]["input.value"]
            assert (
                "provider-reply-marker" in events[log_events.API_RESPONSE]["gen_ai.output.messages"]
            )
