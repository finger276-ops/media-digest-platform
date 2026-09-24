"""Поддельный клиент Supabase для тестов (форма API supabase-py)."""

import re


def _like_regex(pattern):
    """Шаблон SQL LIKE в регулярное выражение: % — любая строка, _ — любой символ."""
    parts = []
    for char in str(pattern):
        if char == "%":
            parts.append(".*")
        elif char == "_":
            parts.append(".")
        else:
            parts.append(re.escape(char))
    return re.compile("".join(parts), re.S)


class Result:
    def __init__(self, data):
        self.data = data


class Query:
    def __init__(self, db, table, op, payload=None, on_conflict=None):
        self.db, self.table, self.op, self.payload = db, table, op, payload
        self.on_conflict = on_conflict
        self.filters = []
        self._order = None
        self._desc = False
        self._limit = None
        self._range = None

    def eq(self, col, val):
        self.filters.append(("eq", col, val))
        return self

    def lt(self, col, val):
        self.filters.append(("lt", col, val))
        return self

    def gte(self, col, val):
        self.filters.append(("gte", col, val))
        return self

    def in_(self, col, vals):
        self.filters.append(("in", col, vals))
        return self

    def like(self, col, pattern):
        self.filters.append(("like", col, _like_regex(pattern)))
        return self

    def order(self, col, desc=False):
        self._order, self._desc = col, desc
        return self

    def limit(self, n):
        self._limit = n
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def _match(self, row):
        for kind, col, val in self.filters:
            current = row.get(col)
            if kind == "eq" and str(current) != str(val):
                return False
            if kind == "in" and current not in val:
                return False
            if kind == "like" and (current is None or not val.fullmatch(str(current))):
                return False
            if kind == "lt" and not (current and str(current) < str(val)):
                return False
            if kind == "gte" and not (current and str(current) >= str(val)):
                return False
        return True

    def execute(self):
        rows = self.db.setdefault(self.table, [])
        if self.op == "select":
            found = [dict(r) for r in rows if self._match(r)]
            if self._order:
                found.sort(key=lambda r: str(r.get(self._order) or ""), reverse=self._desc)
            if self._limit:
                found = found[: self._limit]
            if self._range:
                found = found[self._range[0] : self._range[1] + 1]
            return Result(found)
        if self.op == "update":
            changed = []
            for row in rows:
                if self._match(row):
                    row.update(self.payload)
                    changed.append(dict(row))
            return Result(changed)
        if self.op == "insert":
            rows.append(dict(self.payload))
            return Result([dict(self.payload)])
        if self.op == "upsert":
            # Ключ конфликта берём из on_conflict, как это делает PostgREST;
            # старая эвристика по имени таблицы осталась только как запасной
            # вариант для вызовов без on_conflict.
            key = self.on_conflict or (
                "source_key" if self.table.endswith("sources") else "task_id"
            )
            # PostgREST принимает составной ключ через запятую
            # ("project_id,row_key") — совпадать должны все колонки сразу.
            keys = [k.strip() for k in str(key).split(",") if k.strip()]
            for row in rows:
                if all(row.get(k) == self.payload.get(k) for k in keys):
                    # ON CONFLICT DO UPDATE обновляет только переданные колонки,
                    # остальные (например, started_at с default now()) остаются.
                    row.update(self.payload)
                    return Result([dict(row)])
            rows.append(dict(self.payload))
            return Result([dict(self.payload)])
        if self.op == "delete":
            keep = [r for r in rows if not self._match(r)]
            removed = len(rows) - len(keep)
            self.db[self.table] = keep
            return Result([{"removed": removed}])
        raise AssertionError(self.op)


class Table:
    def __init__(self, db, name):
        self.db, self.name = db, name

    def select(self, *_a, **_k):
        return Query(self.db, self.name, "select")

    def update(self, payload):
        return Query(self.db, self.name, "update", payload)

    def insert(self, payload):
        return Query(self.db, self.name, "insert", payload)

    def upsert(self, payload, on_conflict=None):
        return Query(self.db, self.name, "upsert", payload, on_conflict=on_conflict)

    def delete(self):
        return Query(self.db, self.name, "delete")


class FakeBucket:
    """Хранилище файлов: логотип проекта, исходники выгрузок."""

    def __init__(self, files: dict[str, bytes]):
        self.files = files

    def download(self, path: str) -> bytes:
        try:
            return self.files[str(path)]
        except KeyError as exc:
            raise FileNotFoundError(f"нет файла {path!r} в хранилище") from exc

    def upload(self, path: str, data, file_options=None):
        self.files[str(path)] = bytes(data)
        return {"path": str(path)}

    def remove(self, paths):
        for path in paths if isinstance(paths, (list, tuple)) else [paths]:
            self.files.pop(str(path), None)
        return []

    def get_public_url(self, path: str) -> str:
        return f"https://test.storage/{path}"


class FakeStorage:
    def __init__(self, files: dict[str, bytes]):
        self.files = files

    def from_(self, bucket: str) -> FakeBucket:
        return FakeBucket(self.files)


class FakeClient:
    def __init__(self):
        self.db = {}
        self.files: dict[str, bytes] = {}
        self.storage = FakeStorage(self.files)

    def table(self, name):
        return Table(self.db, name)


