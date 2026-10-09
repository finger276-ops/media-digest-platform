# -*- coding: utf-8 -*-
"""Найти периоды, загруженные с неверными датами. Только читает базу.

До версии 4.12.8 даты из ячеек Excel и даты вида «2026-04-05» разбирались
как «год-день-месяц»: у периода появлялось название вроде
«04.01.2026–04.12.2026», у многих сообщений не было даты. Скрипт проходит по
периодам всех проектов (или одного) и печатает те, у которых видны такие
признаки (services/period_date_check.py). Ничего не меняет — что делать с
найденными, написано в README, раздел «Версия 4.12.8».

Нужен тот же доступ к базе, что и платформе (SUPABASE_URL и ключ в
окружении или в .streamlit/secrets.toml).

    python scripts/check_period_dates.py
    python scripts/check_period_dates.py --project my_project --messages

--messages дополнительно читает сообщения каждого периода: так точнее, но
дольше — у большого проекта это десятки тысяч строк. Только так видны
периоды из текстовых файлов, где у части дат было время, а у части нет:
границы у них правдоподобные, а даты потеряны у многих сообщений.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

import platform_store as store  # noqa: E402
from services.formatting import fmt_period  # noqa: E402
from services.period_date_check import (  # noqa: E402
    messages_date_problems,
    period_date_problems,
)


def find_suspicious_periods(
    project_ids: list[str] | None = None, *, with_messages: bool = False
) -> list[dict]:
    """Подозрительные периоды: проект, период, даты и признаки."""
    if project_ids is None:
        projects = store.list_projects(include_inactive=True)
        project_ids = (
            projects["project_id"].astype(str).tolist() if not projects.empty else []
        )
    found = []
    for project_id in project_ids:
        periods = store.list_periods(project_id, include_inactive=True)
        for _, row in periods.iterrows():
            period = row.to_dict()
            problems = period_date_problems(period)
            if with_messages:
                messages = store.load_table(
                    project_id, [str(period.get("period_id"))], "messages"
                )
                problems += messages_date_problems(messages)
            if problems:
                found.append(
                    {
                        "project_id": project_id,
                        "period_id": str(period.get("period_id") or ""),
                        "period_name": str(period.get("period_name") or ""),
                        "dates": fmt_period(row),
                        "problems": problems,
                    }
                )
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--project", action="append", help="проверить только этот проект")
    parser.add_argument(
        "--messages", action="store_true", help="читать и сообщения периодов"
    )
    args = parser.parse_args(argv)
    found = find_suspicious_periods(args.project, with_messages=args.messages)
    if not found:
        print("Периодов с признаками неверных дат не найдено.")
        return 0
    print(f"Периодов с признаками неверных дат: {len(found)}")
    for item in found:
        print(
            f"\n{item['project_id']} / «{item['period_name']}» ({item['dates']})"
            f"\n  ID периода: {item['period_id']}"
        )
        for problem in item["problems"]:
            print(f"  - {problem}")
    print(
        "\nЧто делать: README, «Версия 4.12.8». Скрипт ничего не изменил."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
