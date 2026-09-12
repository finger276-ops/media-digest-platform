"""Поддельный клиент Supabase для тестов (форма API supabase-py)."""

class Result:
    def __init__(self, data):
        self.data = data


class Query:
    def __init__(self, db, table, op, payload=None):
        self.db, self.table, self.op, self.payload = db, table, op, payload
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

    def in_(self, col, vals):
        self.filters.append(("in", col, vals))
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
            if kind == "lt" and not (current and str(current) < str(val)):
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
            key = "source_key" if self.table.endswith("sources") else "task_id"
            for row in rows:
                if row.get(key) == self.payload.get(key):
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
        return Query(self.db, self.name, "upsert", payload)

    def delete(self):
        return Query(self.db, self.name, "delete")


class FakeClient:
    def __init__(self):
        self.db = {}

    def table(self, name):
        return Table(self.db, name)


