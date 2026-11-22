"""Integration: the live span processor + record_metric tap feed the store.

Mirrors conftest's in-memory plugin wiring but adds a ``_LiveSpanProcessor`` and
flips ``_live_active`` on — the same shape the zero-config (no-backend) path
produces in ``_init_otlp_pipeline``.
"""

import sqlite3
from dataclasses import replace
from unittest.mock import Mock

import pytest

from hermes_otel import hooks
from hermes_otel.live_store_registry import get_live_store
from hermes_otel.plugin_config import HermesOtelConfig


@pytest.fixture()
def live_plugin(tmp_path):
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider

    import hermes_otel.live_store_registry as ls
    import hermes_otel.tracer as tracer_mod
    from hermes_otel.pipeline import _LiveSpanProcessor
    from hermes_otel.testing import attach_pipeline
    from hermes_otel.tracer import HermesOTelPlugin

    get_live_store(create=True, db_path=str(tmp_path / "live.db"))  # fresh tmp store
    store = get_live_store()

    provider = TracerProvider(resource=Resource.create({"service.name": "live-test"}))
    provider.add_span_processor(_LiveSpanProcessor(store))

    plugin = HermesOTelPlugin()
    attach_pipeline(plugin, tracer=provider.get_tracer("live-test"), live_active=True)
    plugin.config = HermesOtelConfig(dashboard_live=True)

    prev = tracer_mod._tracer
    tracer_mod._tracer = plugin
    try:
        yield store, plugin
    finally:
        tracer_mod._tracer = prev
        ls.close_live_stores()
        provider.shutdown()


def test_rebuild_flushes_and_keeps_the_same_writer_and_history(tmp_path, monkeypatch):
    from hermes_otel.tracer import HermesOTelPlugin

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = HermesOTelPlugin(HermesOtelConfig(dashboard_live=True, host_metrics=False))
    assert plugin.init()
    pipeline = plugin.pipeline
    assert pipeline is not None
    writer = pipeline.live_store
    conn = writer._connection
    writer.flush_interval_s = 3600
    writer.add_span({"name": "before-rebuild"})

    assert plugin.init() and plugin.pipeline is pipeline
    plugin.config = replace(plugin.config, preview_max_chars=1800)
    assert plugin.init()
    rebuilt = plugin.pipeline
    assert rebuilt is not None and rebuilt is not pipeline
    assert rebuilt.live_store is writer is get_live_store()
    assert writer._connection is conn
    assert conn.execute("SELECT name FROM events WHERE kind = 'span'").fetchall() == [
        ("before-rebuild",)
    ]
    with rebuilt.tracer.start_as_current_span("after-rebuild"):
        pass
    assert {span["name"] for span in writer.spans()} == {"before-rebuild", "after-rebuild"}


def test_registered_unload_releases_only_its_profile_and_all_its_targets(tmp_path, monkeypatch):
    from hermes_otel import register, tracer

    home, other_home = tmp_path / "one", tmp_path / "two"
    home.mkdir()
    other_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    plugin = tracer.HermesOTelPlugin(HermesOtelConfig(dashboard_live=True, host_metrics=False))
    monkeypatch.setattr(tracer, "_tracer", plugin)
    ctx = Mock()
    register(ctx)
    ctx.on_unload.assert_called_once()
    unload = ctx.on_unload.call_args.args[0]
    assert plugin.pipeline is not None
    writer = plugin.pipeline.live_store
    writer.add_span({"name": "discard-on-unload"})
    disk_path = home / "live.db"
    monkeypatch.setenv("HERMES_OTEL_LIVE_DB", str(disk_path))
    plugin.config = replace(plugin.config, dashboard_live_storage="disk")
    assert plugin.init()
    disk_writer = plugin.pipeline.live_store
    assert disk_writer is not writer and disk_writer.db_path == str(disk_path)
    disk_writer.add_span({"name": "persist-on-unload"})
    connections = (writer._connection, disk_writer._connection)

    monkeypatch.delenv("HERMES_OTEL_LIVE_DB")
    monkeypatch.setenv("HERMES_HOME", str(other_home))
    other = get_live_store(create=True)
    assert other is not None
    other_conn = other._connection
    other.add_span({"name": "other-profile"})
    unload()
    assert plugin.pipeline is None
    assert get_live_store() is other and other._connection is other_conn
    assert [s["name"] for s in other.spans()] == ["other-profile"]
    for conn in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")
    assert writer.spans() == []

    monkeypatch.setenv("HERMES_HOME", str(home))
    assert get_live_store() is None
    plugin.config = replace(plugin.config, dashboard_live_storage="memory")
    assert plugin.init()
    fresh = get_live_store()
    assert fresh is not None and fresh is not writer and fresh.spans() == []
    reopened = get_live_store(create=True, db_path=str(disk_path))
    assert reopened is not None and reopened is not disk_writer
    assert [s["name"] for s in reopened.spans()] == ["persist-on-unload"]


class TestLivePipeline:
    def test_full_turn_lands_in_store(self, live_plugin):
        store, _ = live_plugin
        hooks.on_session_start(session_id="s1", model="gpt-4", platform="telegram")
        hooks.on_pre_tool_call(
            tool_name="bash", args={"command": "ls"}, task_id="t1", session_id="s1"
        )
        hooks.on_post_tool_call(
            tool_name="bash", args={}, result="ok", task_id="t1", session_id="s1"
        )
        hooks.on_session_end(
            session_id="s1", completed=True, interrupted=False, model="gpt-4", platform="telegram"
        )

        spans = store.spans()
        names = sorted(s["name"] for s in spans)
        assert "agent" in names and "tool.bash" in names
        # span dicts carry the trace tree + attributes the dashboard renders
        agent = next(s for s in spans if s["name"] == "agent")
        assert len(agent["trace_id"]) == 32 and len(agent["span_id"]) == 16
        assert agent["status"] in ("OK", "UNSET")
        assert agent["attributes"].get("hermes.session.kind") == "session"
        # tool span is a child (has a parent in the same trace)
        tool = next(s for s in spans if s["name"] == "tool.bash")
        assert tool["parent_span_id"] is not None
        assert tool["trace_id"] == agent["trace_id"]

    def test_metrics_tapped_without_meterprovider(self, live_plugin):
        # No MeterProvider is wired (live-only), yet record_metric still feeds
        # the live store because the tap runs before the meter guard.
        store, plugin = live_plugin
        assert plugin.pipeline.meter is None
        hooks.on_session_start(session_id="s2", model="gpt-4", platform="cli")
        names = [m["name"] for m in store.metrics()]
        assert "hermes.session.count" in names  # the OTLP name, as the backends see it (#95)

    def test_incremental_cursor(self, live_plugin):
        store, _ = live_plugin
        hooks.on_session_start(session_id="s3", model="gpt-4", platform="cli")
        cur = store.cursor()
        assert store.spans(since=cur) == []
        hooks.on_session_end(
            session_id="s3", completed=True, interrupted=False, model="gpt-4", platform="cli"
        )
        fresh = store.spans(since=cur)
        assert any(s["name"] == "agent" for s in fresh)


class TestRootSpanCommitsImmediately:
    """A one-shot `hermes -z` exits without atexit, so the turn's last spans
    must be on disk the moment the root span ends, not on the writer thread's
    next tick (#215)."""

    def test_root_span_end_is_committed_without_flush(self, live_plugin):
        import sqlite3

        store, _ = live_plugin
        hooks.on_session_start(session_id="s9", model="gpt-4", platform="cli")
        hooks.on_pre_tool_call(
            tool_name="bash", args={"command": "ls"}, task_id="t9", session_id="s9"
        )
        hooks.on_post_tool_call(
            tool_name="bash", args={}, result="ok", task_id="t9", session_id="s9"
        )
        hooks.on_session_end(
            session_id="s9", completed=True, interrupted=False, model="gpt-4", platform="cli"
        )

        # A raw connection sees only committed rows; do NOT call store.flush().
        raw = sqlite3.connect(store.db_path)
        try:
            names = sorted(r[0] for r in raw.execute("SELECT name FROM events WHERE kind='span'"))
        finally:
            raw.close()
        assert "agent" in names and "tool.bash" in names

    def test_child_span_end_stays_batched(self, live_plugin):
        import sqlite3

        store, plugin = live_plugin
        hooks.on_session_start(session_id="s8", model="gpt-4", platform="cli")
        hooks.on_pre_tool_call(
            tool_name="bash", args={"command": "ls"}, task_id="t8", session_id="s8"
        )
        hooks.on_post_tool_call(
            tool_name="bash", args={}, result="ok", task_id="t8", session_id="s8"
        )
        # The tool span (a child) is queued, not necessarily committed yet: the
        # hot path pays no disk commit (#91). Either state is acceptable here;
        # the invariant is that flush() makes it visible.
        store.flush()
        raw = sqlite3.connect(store.db_path)
        try:
            names = [r[0] for r in raw.execute("SELECT name FROM events WHERE kind='span'")]
        finally:
            raw.close()
        assert "tool.bash" in names and "agent" not in names
        hooks.on_session_end(
            session_id="s8", completed=True, interrupted=False, model="gpt-4", platform="cli"
        )
