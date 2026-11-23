"""The default live buffer is isolated; only an explicit opt-in reads disk."""

import io
import json
import os
import sqlite3
import threading
import time
from contextlib import closing

import pytest

from hermes_otel.dashboard import live_api
from hermes_otel.live_store import LiveStore, default_db_path_for
from hermes_otel.live_store_registry import get_live_store, get_live_store_for_home
from hermes_otel.plugin_config import load_config
from hermes_otel.query_cli import main


def _cli(*args):
    out, err = io.StringIO(), io.StringIO()
    code = main(list(args), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_memory_default_uses_no_sqlite_files_and_close_does_not_reopen(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store = LiveStore()
    try:
        assert store.db_path == ":memory:"
        assert store._read(None, lambda c: c.execute("PRAGMA temp_store").fetchone()[0]) == 2
        worker = threading.Thread(target=lambda: store.add_span({"name": "thread-span"}))
        worker.start()
        worker.join()
        assert [s["name"] for s in store.spans()] == ["thread-span"]
        assert not list(tmp_path.iterdir())
    finally:
        store.close()
    store.add_span({"name": "after-close"})
    assert store.flush() == 0 and store.spans() == []
    assert not list(tmp_path.iterdir())


def test_memory_store_is_per_profile_and_only_served_in_process(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(a))
    one = get_live_store(create=True)
    assert one is not None
    one.add_span({"name": "a"})
    assert live_api.live_status()["spans"] == 1
    monkeypatch.setenv("HERMES_HOME", str(b))
    assert get_live_store_for_home() is None
    status = live_api.live_status()
    assert status["live"] is False and "dashboard_live_storage: disk" in status["reason"]
    two = get_live_store(create=True)
    assert two is not None
    two.add_span({"name": "b"})
    assert one is not two
    assert get_live_store_for_home() is two
    assert [s["name"] for s in two.spans()] == ["b"]
    monkeypatch.setenv("HERMES_HOME", str(a))
    assert get_live_store_for_home() is one
    assert [s["name"] for s in one.spans()] == ["a"]
    assert not list(a.iterdir()) and not list(b.iterdir())


def test_sql_and_queries_never_fall_back_to_stale_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_otel import live_store

    path = default_db_path_for(tmp_path)
    monkeypatch.setattr(live_store, "_default_db_path", lambda: path)
    store = LiveStore(db_path=path)
    store.add_span({"name": "old", "trace_id": "aaa"})
    store.close()
    before = (tmp_path / "hermes_otel_live.db").read_bytes()
    assert live_api.live_status()["live"] is False
    assert "dashboard_live_storage: disk" in live_api.live_status()["reason"]
    for args in (("traces",), ("sql", "SELECT * FROM events")):
        code, out, err = _cli(*args)
        assert code == 1 and not out and "dashboard_live_storage: disk" in err
    code, out, _ = _cli("status", "--json")
    live = json.loads(out)["live_store"]
    assert code == 0 and live["storage"] == "memory" and not live["exists"]
    assert (tmp_path / "hermes_otel_live.db").read_bytes() == before
    assert set(p.name for p in tmp_path.iterdir()) == {"hermes_otel_live.db"}
    code, out, _ = _cli("sql", "SELECT name FROM events", "--db", path, "--json")
    assert code == 0 and json.loads(out) == [{"name": "old"}]


def test_disk_opt_in_persists_and_cli_reopens(tmp_path, monkeypatch):
    from hermes_otel import live_store

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_OTEL_DASHBOARD_LIVE_STORAGE", "disk")
    path = default_db_path_for(tmp_path)
    monkeypatch.setattr(live_store, "_default_db_path", lambda: path)
    store = get_live_store(create=True)
    assert store is not None
    store.add_span({"name": "persisted", "trace_id": "abc"})
    store.close()
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    cli_code, out, _ = _cli("sql", "SELECT name FROM events", "--json")
    assert cli_code == 0 and json.loads(out) == [{"name": "persisted"}]
    reader = LiveStore(db_path=path)
    try:
        assert [s["name"] for s in reader.spans()] == ["persisted"]
    finally:
        reader.close()


def test_configured_positive_cap_is_global_and_exact_on_small_flushes(tmp_path):
    config = tmp_path / "otel.yaml"
    config.write_text("dashboard_live_max_spans: 3\n", encoding="utf-8")
    cfg = load_config(config)
    store = LiveStore(db_path=":memory:", max_rows=cfg.dashboard_live_max_spans)
    try:
        store.add_span({"name": "old-span"})
        store.add_log({"body": "old-log"})
        store.flush()
        store.add_metric("hermes.token.usage", 1, {}, time.time_ns())
        store.add_metric("hw.gpu.utilization", 2, {}, time.time_ns())
        store.add_log({"body": "new-log"})
        assert store.spans() == []
        assert [m["name"] for m in store.metrics()] == [
            "hermes.token.usage",
            "hw.gpu.utilization",
        ]
        assert [record["body"] for record in store.logs()] == ["new-log"]
        for i in range(8):
            store.add_metric("hw.gpu.utilization", i, {}, time.time_ns())
            store.flush()
            stats = store.stats()
            assert stats["spans"] + stats["metrics"] + stats["logs"] == 3
    finally:
        store.close()


@pytest.mark.parametrize("bad", ["0", "-1", "bad", "1.2", "true", "9223372036854775808"])
def test_invalid_row_cap_uses_the_finite_default(tmp_path, bad):
    config = tmp_path / "otel.yaml"
    config.write_text(f"dashboard_live_max_spans: {bad}\n", encoding="utf-8")
    assert load_config(config).dashboard_live_max_spans == 1000


def test_storage_mode_validates_yaml_and_environment(tmp_path, monkeypatch):
    config = tmp_path / "otel.yaml"
    config.write_text("dashboard_live_storage: DiSk\n", encoding="utf-8")
    assert load_config(config).dashboard_live_storage == "disk"
    monkeypatch.setenv("HERMES_OTEL_DASHBOARD_LIVE_STORAGE", "invalid")
    assert load_config(config).dashboard_live_storage == "disk"
    config.write_text("dashboard_live_storage: invalid\n", encoding="utf-8")
    assert load_config(config).dashboard_live_storage == "memory"


def test_pending_and_failed_batch_are_bounded_and_rollback_is_atomic():
    store = LiveStore(db_path=":memory:", max_rows=5)
    store.flush_interval_s = 3600
    store.batch_rows = 1000
    try:
        store._read(
            None,
            lambda c: c.execute(
                "CREATE TRIGGER reject BEFORE INSERT ON events WHEN NEW.name = 'reject' "
                "BEGIN SELECT RAISE(ABORT, 'rejected row'); END"
            ).close(),
        )
        for i in range(25):
            store.add_span({"name": str(i)})
        store.add_span({"name": "reject"})
        assert store.flush() == 0
        stats = store.stats()
        assert stats["spans"] == 0 and stats["pending"] == 5
        assert "rejected row" in stats["write_error"]
        store._read(None, lambda c: c.execute("DROP TRIGGER reject").close())
        assert [s["name"] for s in store.spans()] == ["21", "22", "23", "24", "reject"]
        assert "write_error" not in store.stats()
        assert store.cursor() == 5
    finally:
        store.close()


def test_disk_close_waits_for_active_read_and_commits_new_arrivals(tmp_path):
    path = str(tmp_path / "live.db")
    store = LiveStore(db_path=path)
    store.flush_interval_s = 3600
    store.add_span({"name": "before-read"})
    reading = threading.Event()
    release = threading.Event()
    close_started = threading.Event()
    closed = threading.Event()
    seen = []

    def read(conn):
        with closing(conn.cursor()) as cursor:
            cursor.execute("SELECT name FROM events")
            reading.set()
            assert release.wait(10)
            return cursor.fetchall()

    def close_store():
        close_started.set()
        store.close()
        closed.set()

    reader = threading.Thread(target=lambda: seen.append(store._read(None, read)))
    closer = threading.Thread(target=close_store)
    reader.start()
    try:
        assert reading.wait(10)
        store.add_span({"name": "last-batch"})
        closer.start()
        assert close_started.wait(10) and not closed.wait(0.05)
    finally:
        release.set()
        reader.join(10)
        if closer.ident is not None:
            closer.join(10)
        store.close()

    assert seen == [[("before-read",)]] and closed.is_set()
    store.add_span({"name": "too-late"})
    assert store.flush() == 0 and store.spans() == []
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT name FROM events ORDER BY seq").fetchall() == [
            ("before-read",),
            ("last-batch",),
        ]


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="Linux FD paths required")
def test_disk_sqlite_fds_stay_constant_with_live_workers_and_close(tmp_path):
    path = tmp_path / "telemetry.db"
    store = LiveStore(db_path=str(path))

    def fds():
        paths = []
        for fd in os.listdir("/proc/self/fd"):
            try:
                target = os.readlink(f"/proc/self/fd/{fd}")
            except FileNotFoundError:
                continue
            if target in {str(path), f"{path}-wal", f"{path}-shm"}:
                paths.append(target)
        return sorted(paths)

    count = 20
    barrier = threading.Barrier(count + 1, timeout=15)
    release = threading.Event()
    seen = []

    def use_store(i):
        store.add_span({"name": str(i)})
        seen.append(store.cursor())
        barrier.wait()
        release.wait(15)
        store.spans()

    store.add_span({"name": "warm"})
    assert store.flush() == 1
    before = fds()
    workers = [threading.Thread(target=use_store, args=(i,)) for i in range(count)]
    for worker in workers:
        worker.start()
    try:
        barrier.wait()
        assert len(seen) == count and len(store.spans()) == count + 1
        assert before and fds() == before
    finally:
        release.set()
        for worker in workers:
            worker.join(16)
        store.close()
    assert not fds()
