from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import Any, Iterator

try:  # Streamlit is optional for unit tests/imports.
    import streamlit as st
except Exception:  # pragma: no cover
    st = None  # type: ignore

LOGGER = logging.getLogger("platform.perf")


def _state_get(key: str, default: Any) -> Any:
    if st is None:
        return default
    try:
        return st.session_state.get(key, default)
    except Exception:
        return default


def _state_set(key: str, value: Any) -> None:
    if st is None:
        return
    try:
        st.session_state[key] = value
    except Exception:
        pass


def perf_debug_enabled() -> bool:
    value = os.getenv("PLATFORM_PERF_DEBUG", "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    return bool(_state_get("platform_perf_debug", False))


def reset_perf_events() -> None:
    _state_set("_platform_perf_events", [])


def record_perf_event(label: str, elapsed_ms: float, **meta: Any) -> None:
    event = {"label": label, "elapsed_ms": round(float(elapsed_ms), 1)}
    for key, value in meta.items():
        if value is not None:
            event[key] = value
    LOGGER.info("%s finished in %.1f ms", label, elapsed_ms)
    events = list(_state_get("_platform_perf_events", []))
    events.append(event)
    _state_set("_platform_perf_events", events[-50:])


@contextmanager
def perf_block(label: str, **meta: Any) -> Iterator[None]:
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        # Keep lightweight timings available for the admin diagnostics panel.
        record_perf_event(label, elapsed_ms, **meta)


def render_perf_sidebar() -> None:
    if st is None or not perf_debug_enabled():
        return
    events = list(_state_get("_platform_perf_events", []))
    if not events:
        return
    try:
        import pandas as pd

        with st.sidebar.expander("Диагностика скорости", expanded=False):
            df = pd.DataFrame(events)
            if not df.empty:
                st.dataframe(df.tail(15), hide_index=True, use_container_width=True)
    except Exception:
        pass
