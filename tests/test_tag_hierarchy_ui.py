# -*- coding: utf-8 -*-
"""Чтение файла структуры тегов (tag_hierarchy_ui._read_structure_file).

Докстринг модуля обещает «терпимость к битым стилям xlsx» — раньше это было
engine="calamine", а python-calamine не объявлен в requirements.txt и не
установлен: любой битый файл падал ImportError'ом вместо тихой починки.
Починка переведена на уже проверенный _open_excel_file_resilient из
import_adapters.py (тот же механизм, что чинит выгрузки мониторинга) —
никакой новой зависимости.

Мутационная проверка: вернуть engine="calamine" в _read_structure_file ->
тест "битый xlsx чинится и читается" краснеет с ImportError.
"""

import sys
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from tag_hierarchy_ui import _read_structure_file  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(("  ✓ " if condition else "  ✗ ") + label + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


class FakeUploadedFile(BytesIO):
    """Минимальная замена st.runtime.uploaded_file_manager.UploadedFile:
    нужны только .name (используется для выбора CSV/Excel) и .getvalue()
    (уже есть у BytesIO)."""

    def __init__(self, data: bytes, name: str):
        super().__init__(data)
        self.name = name


print("1. CSV читается как есть")
csv_bytes = "tag,tier,parent\nбренд,1,\nпродукт,2,бренд\n".encode("utf-8")
csv_df = _read_structure_file(FakeUploadedFile(csv_bytes, "structure.csv"))
check("CSV прочитан", list(csv_df["tag"]) == ["бренд", "продукт"], str(csv_df.to_dict("records")))

print("2. Обычный xlsx читается без починки")
buf = BytesIO()
with pd.ExcelWriter(buf, engine="openpyxl") as writer:
    pd.DataFrame([{"tag": "бренд", "tier": 1, "parent": ""}]).to_excel(writer, index=False)
xlsx_df = _read_structure_file(FakeUploadedFile(buf.getvalue(), "structure.xlsx"))
check("xlsx прочитан", list(xlsx_df["tag"]) == ["бренд"], str(xlsx_df.to_dict("records")))

print("3. Битый xl/styles.xml чинится и читается, а не падает ImportError'ом")
with TemporaryDirectory() as tmp:
    good = Path(tmp) / "ok.xlsx"
    with pd.ExcelWriter(good, engine="openpyxl") as writer:
        pd.DataFrame([{"tag": "бренд", "tier": 1, "parent": ""}]).to_excel(writer, index=False)
    broken_bytes_buf = BytesIO()
    with zipfile.ZipFile(good) as src, zipfile.ZipFile(broken_bytes_buf, "w") as dst:
        for item in src.infolist():
            payload = src.read(item.filename)
            if item.filename == "xl/styles.xml":
                payload = b"<styleSheet>"  # обрезанный, невалидный XML
            dst.writestr(item, payload)

    def leftovers():
        return set(Path(tempfile.gettempdir()).glob("xlsx_styles_repaired_*"))

    before = leftovers()
    try:
        repaired_df = _read_structure_file(FakeUploadedFile(broken_bytes_buf.getvalue(), "broken.xlsx"))
        check(
            "битый xlsx всё равно прочитан",
            list(repaired_df["tag"]) == ["бренд"],
            str(repaired_df.to_dict("records")),
        )
    except Exception as exc:  # noqa: BLE001
        check("битый xlsx всё равно прочитан", False, f"{type(exc).__name__}: {exc}")
    check(
        "чтение не оставило временную копию в temp",
        not (leftovers() - before),
        str(sorted(p.name for p in leftovers() - before)),
    )

print("4. Excel 97-2003 (.xls) читается, а не падает без xlrd")
# Загрузчик структуры принимает .xls, но читать его раньше было нечем. Берётся
# первый лист синтетической книги из tests/fixtures — «Обложка».
xls_bytes = (REPO / "tests" / "fixtures" / "ba_export_97.xls").read_bytes()
try:
    xls_df = _read_structure_file(FakeUploadedFile(xls_bytes, "structure.xls"))
    check("xls прочитан", list(xls_df.columns) == ["Отчёт Brand Analytics"], str(list(xls_df.columns)))
except Exception as exc:  # noqa: BLE001
    check("xls прочитан", False, f"{type(exc).__name__}: {exc}"[:200])

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} → {failures}")
    raise SystemExit(1)
print("Чтение файла структуры тегов работает, включая починку битых стилей xlsx.")
