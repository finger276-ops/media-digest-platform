"""Мини-приложение для проверки границы отказа разделов.

Запускается через AppTest из tests/test_section_boundary.py. Нужен именно
настоящий рантайм Streamlit: контракт границы держится на том, что st.rerun()
и st.stop() бросают исключения не от Exception, а от BaseException, — вне
рантайма эту разницу не проверить.

Сценарий выбирается через session_state["boundary_mode"].
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")

import streamlit as st  # noqa: E402

from app import render_section_safely  # noqa: E402


def _ok() -> None:
    st.write("раздел отрисован")


def _crashing() -> None:
    st.write("строка до падения")
    raise RuntimeError("раздел сломался")


def _rerunning() -> None:
    """Кнопка внутри раздела: типовой случай, ради которого важен BaseException."""
    passes = int(st.session_state.get("boundary_passes", 0)) + 1
    st.session_state["boundary_passes"] = passes
    st.write(f"проход {passes}")
    if passes == 1:
        st.rerun()


def _stopping() -> None:
    st.write("строка до stop")
    st.stop()
    st.write("строка после stop")


SECTIONS = {
    "ok": _ok,
    "crash": _crashing,
    "rerun": _rerunning,
    "stop": _stopping,
}

mode = str(st.session_state.get("boundary_mode", "ok"))
details = bool(st.session_state.get("boundary_details", False))

result = render_section_safely(
    "Инфоповоды", SECTIONS[mode], _details=details
)
st.session_state["boundary_result"] = result

# Маркер конца скрипта: если st.stop() внутри раздела был проглочен границей,
# эта строка появится на странице — и тест это заметит.
st.write("скрипт дошёл до конца")
