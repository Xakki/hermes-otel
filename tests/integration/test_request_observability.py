"""Request events and counters at the host-hook and live/OTLP boundaries."""

import json
from unittest.mock import Mock

import pytest

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
