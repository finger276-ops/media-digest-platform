from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"

# Streamlit Cloud запускает приложение из корня репозитория. Чтобы импорты
# работали стабильно, явно добавляем и корень, и папку src в sys.path.
for path in (ROOT, SRC):
    path_s = str(path)
    if path_s not in sys.path:
        sys.path.insert(0, path_s)

if not SRC.exists():
    st.error(
        "Не найдена папка src рядом со streamlit_app.py. "
        "Проверь, что в GitHub загружена вся папка проекта, а не только streamlit_app.py."
    )
    st.code("""
В корне репозитория должны быть:
streamlit_app.py
requirements.txt
src/app.py
src/platform_store.py
src/preprocess.py
src/import_adapters.py
sql/platform_schema.sql
""".strip())
    st.stop()

try:
    from src.app import main
except ModuleNotFoundError:
    # Fallback для случаев, когда src уже добавлен в sys.path и импорт идет как app.
    from app import main

if __name__ == "__main__":
    main()
