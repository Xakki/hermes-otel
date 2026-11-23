"""Request dashboard selectors over points emitted by real API hooks."""

import json
import re
from pathlib import Path

import pytest
from _helpers import get_metric, metric_points

from hermes_otel import hooks


def _attempt(provider, *, failed=False):
    identity = {
        "task_id": "dashboard-turn",
        "session_id": "dashboard-session",
        "platform": "cli",
        "model": "m",
        "provider": provider,
        "account_name": "test-account",
    }
    request = {
        **identity,
        "base_url": "",
        "api_mode": "chat",
        "api_call_count": 1,
        "message_count": 1,
    }
    hooks.on_pre_api_request(
        **request, tool_count=0, approx_input_tokens=10, request_char_count=20, max_tokens=10
    )
    if failed:
        hooks.on_api_request_error(**identity, api_duration=0.25, status_code=429)
    else:
        hooks.on_post_api_request(
            **request,
            api_duration=0.5,
            finish_reason="stop",
            response_model="m",
            usage={"input_tokens": 10, "output_tokens": 2, "cache_read_tokens": 3},
            assistant_content_chars=20,
            assistant_tool_call_count=0,
        )


def _selected(points, query, metric, provider):
    # Evaluate the panels' vector-selector label matchers, not PromQL rate or
    # histogram math. OTLP's default Prometheus translation replaces dots.
    for var, value in {"provider": provider, "model": "m", "account": "test-account"}.items():
        query = query.replace("${" + var + ":regex}", value)
    (selector,) = re.findall(re.escape(metric) + r"\{([^}]+)\}", query)
    matchers = []
    for matcher in selector.split(","):
        match = re.fullmatch(r'(\w+)=~"([^"]+)"', matcher)
        assert match is not None, matcher
        matchers.append(match.groups())
    selected = []
    for point in points:
        labels = {key.replace(".", "_"): value for key, value in point.attributes.items()}
        if all(re.fullmatch(regex, labels.get(label, "")) for label, regex in matchers):
            selected.append(point)
    return selected


@pytest.mark.parametrize("suffix", ["", "-provisioned"], ids=["portable", "provisioned"])
def test_dashboard_provider_selection_includes_codex_latency(inmemory_otel_with_metrics, suffix):
    _, reader, _ = inmemory_otel_with_metrics
    _attempt("openai-codex")
    _attempt("openai-codex", failed=True)
    _attempt("openai")
    _attempt("anthropic")
    path = Path(__file__).resolve().parents[2] / "website" / "static" / "dashboards"
    dashboard = json.loads((path / f"hermes-request-observability{suffix}.json").read_text())
    variables = {var["name"]: var for var in dashboard["templating"]["list"]}
    panels = {panel["title"]: panel["targets"][0]["expr"] for panel in dashboard["panels"]}

    options_query = re.fullmatch(r"label_values\((\w+), (\w+)\)", variables["provider"]["query"])
    assert options_query is not None
    counter_name, provider_label = options_query.groups()
    assert counter_name == "hermes_api_request_count_total"
    requests = metric_points(get_metric(reader, "hermes.api.request.count"))
    assert {p.attributes[provider_label]: p.value for p in requests} == {
        "openai": 3,
        "anthropic": 1,
    }
    chosen_provider = next(p.attributes[provider_label] for p in requests if p.value == 3)
    assert [
        p.value
        for p in _selected(
            requests, panels["API requests · last hour"], counter_name, chosen_provider
        )
    ] == [3]

    durations = metric_points(get_metric(reader, "gen_ai.client.operation.duration"))
    query = panels["p95 provider latency"]
    bucket = "gen_ai_client_operation_duration_seconds_bucket"
    raw = "gen_ai.provider.name"
    assert sorted((p.attributes[raw], p.sum) for p in durations) == [
        ("anthropic", 0.5),
        ("openai", 0.5),
        ("openai-codex", 0.25),
        ("openai-codex", 0.5),
    ]
    assert (
        sum(p.count for p in _selected(durations, query, bucket, variables["provider"]["allValue"]))
        == 4
    )
    assert [p.attributes[raw] for p in _selected(durations, query, bucket, "anthropic")] == [
        "anthropic"
    ]
    assert {
        p.attributes["provider"] for p in metric_points(get_metric(reader, "hermes.token.usage"))
    } == {"openai", "anthropic"}
    token_points = metric_points(get_metric(reader, "gen_ai.client.token.usage"))
    assert {p.attributes[raw] for p in token_points} == {"openai-codex", "openai", "anthropic"}
    assert all("provider" not in p.attributes for p in token_points)
    assert sorted(
        (p.attributes[raw], p.sum) for p in _selected(durations, query, bucket, chosen_provider)
    ) == [("openai", 0.5), ("openai-codex", 0.25), ("openai-codex", 0.5)]
