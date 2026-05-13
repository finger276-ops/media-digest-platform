from __future__ import annotations

import pandas as pd


def message_text_column(df: pd.DataFrame) -> str | None:
    for col in ["text", "text_clean", "message_text", "message_raw", "Сообщение"]:
        if col in df.columns:
            return col
    return None


def message_link_column(df: pd.DataFrame) -> str | None:
    for col in ["url", "message_link", "Ссылка"]:
        if col in df.columns:
            return col
    return None
