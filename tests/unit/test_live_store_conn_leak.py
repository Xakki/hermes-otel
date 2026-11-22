"""The disk store does not collect SQLite descriptors from executor threads."""

import os
import threading
from pathlib import Path

import pytest

from hermes_otel.live_store import LiveStore


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="Linux FD paths required")
def test_disk_descriptors_are_stable_after_short_lived_threads(tmp_path):
    path = tmp_path / "live.db"
    store = LiveStore(db_path=str(path))

    def open_sqlite_paths():
        paths = []
        for fd in Path("/proc/self/fd").iterdir():
            try:
                target = os.readlink(fd)
            except FileNotFoundError:
                continue
            if target in {str(path), f"{path}-wal", f"{path}-shm"}:
                paths.append(target)
        return sorted(paths)

    try:
        store.add_span({"name": "a"})
        assert [row["name"] for row in store.spans()] == ["a"]
        initial = open_sqlite_paths()
        assert initial
        seen = []
        for _ in range(50):
            worker = threading.Thread(target=lambda: seen.append(store.spans(limit=1)))
            worker.start()
            worker.join()
        assert [rows[0]["name"] for rows in seen] == ["a"] * 50
        assert open_sqlite_paths() == initial
    finally:
        store.close()
    assert not open_sqlite_paths()
