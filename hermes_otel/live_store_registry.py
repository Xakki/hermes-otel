"""The process owns live writers; disk readers are shared only by file path."""

from __future__ import annotations

import atexit
import os
import threading
from pathlib import Path
from typing import Dict, Optional, Tuple

from . import live_store as _ls
from .live_store import MEMORY_DB, LiveStore
from .plugin_config import HermesOtelConfig, hermes_home, load_config_with_sources

MEMORY_ONLY_REASON = (
    "The in-memory live store is only available in the recording process. "
    "For cross-process queries set dashboard_live_storage: disk and restart the recorder, "
    "or query a configured backend."
)
DISABLED_REASON = "Local live telemetry is disabled (dashboard_live: false)."

_LIVE_LOCK = threading.Lock()
_LIVE_STORES: Dict[Path, LiveStore] = {}
# Keep one writer per profile and target across pipeline rebuilds, until the
# profile unloads or the process exits.
_WRITERS: Dict[Tuple[Path, str], LiveStore] = {}
_STORES_BY_PATH: Dict[str, LiveStore] = {}
MAX_READERS = 16
_CREATE_ERRORS: Dict[str, str] = {}


def _config() -> HermesOtelConfig:
    return load_config_with_sources(warn=False)[0]


def configured_db_path(config: HermesOtelConfig, home: Optional[Path] = None) -> Optional[str]:
    """Explicit legacy override, configured disk, private memory, or disabled."""
    if not config.dashboard_live:
        return None
    path = os.environ.get("HERMES_OTEL_LIVE_DB")
    if path:
        return path
    if config.dashboard_live_storage == "disk":
        return _ls._default_db_path() if home is None else _ls.default_db_path_for(home)
    return MEMORY_DB


def _configured_limits(config: Optional[HermesOtelConfig] = None) -> dict:
    cfg = config if config is not None else _config()
    return {
        "max_rows": cfg.dashboard_live_max_spans,
        "retention_hours": cfg.dashboard_live_retention_hours,
    }


def get_live_store_for_home(create: bool = True) -> Optional[LiveStore]:
    """Reuse this profile's writer or open only its explicitly selected file."""
    config = _config()
    path = configured_db_path(config)
    if path is None:
        return None
    home = hermes_home()
    with _LIVE_LOCK:
        current = _LIVE_STORES.get(home)
        if current is not None and current.db_path == path:
            return current
        if path == MEMORY_DB:
            return None
        _evict_stale_readers(keep=path)
        store = _STORES_BY_PATH.get(path)
        if store is None and create:
            try:
                store = LiveStore(db_path=path, **_configured_limits(config))
            except Exception as exc:
                _CREATE_ERRORS[path] = f"{type(exc).__name__}: {exc}"
                return None
            _CREATE_ERRORS.pop(path, None)
            _STORES_BY_PATH[path] = store
            _evict_stale_readers(keep=path)
        return store


def live_unavailable_reason() -> Optional[str]:
    path = configured_db_path(_config())
    if path is None:
        return DISABLED_REASON
    return MEMORY_ONLY_REASON if path == MEMORY_DB else None


def create_error_for_home() -> Optional[Tuple[str, str]]:
    path = configured_db_path(_config())
    if path is None or path == MEMORY_DB:
        return None
    with _LIVE_LOCK:
        error = _CREATE_ERRORS.get(path)
    return (path, error) if error else None


def _evict_stale_readers(keep: str) -> None:
    """Keep the reader cache bounded. Caller holds ``_LIVE_LOCK``."""
    for path in list(_STORES_BY_PATH):
        if path != keep and not os.path.exists(path):
            _STORES_BY_PATH.pop(path).close()
    while len(_STORES_BY_PATH) > MAX_READERS:
        oldest = next((path for path in _STORES_BY_PATH if path != keep), None)
        if oldest is None:
            break
        _STORES_BY_PATH.pop(oldest).close()


def get_live_store(
    create: bool = False,
    db_path: Optional[str] = None,
    max_rows: Optional[int] = None,
    retention_hours: Optional[float] = None,
    *,
    config: Optional[HermesOtelConfig] = None,
    home: Optional[Path] = None,
) -> Optional[LiveStore]:
    """Return the writer owned by this profile; no discovery on the hot path."""
    owner = home if home is not None else hermes_home()
    if not create:
        with _LIVE_LOCK:
            return _LIVE_STORES.get(owner)
    cfg = config if config is not None else _config()
    path = db_path or configured_db_path(cfg, home=home)
    if path is None:
        return None
    with _LIVE_LOCK:
        key = (owner, path)
        store = _WRITERS.get(key)
        if store is None:
            store = LiveStore(
                db_path=path,
                max_rows=cfg.dashboard_live_max_spans if max_rows is None else max_rows,
                retention_hours=(
                    cfg.dashboard_live_retention_hours
                    if retention_hours is None
                    else retention_hours
                ),
            )
            _WRITERS[key] = store
        _LIVE_STORES[owner] = store
        return store


def close_live_writers(home: Path) -> None:
    """Release this profile's writers, including inactive storage targets."""
    with _LIVE_LOCK:
        _LIVE_STORES.pop(home, None)
        for key in list(_WRITERS):
            if key[0] == home:
                _WRITERS.pop(key).close()


@atexit.register
def close_live_stores() -> None:
    """The registry, not an individual tracer, owns process-level shutdown."""
    with _LIVE_LOCK:
        stores = [*_WRITERS.values(), *_STORES_BY_PATH.values()]
        _LIVE_STORES.clear()
        _WRITERS.clear()
        _STORES_BY_PATH.clear()
        _CREATE_ERRORS.clear()
    for store in stores:
        store.close()
