"""Entity store with optimistic concurrency: Azure Table Storage in Azure, SQLite locally and in tests.

Why Table Storage instead of PostgreSQL (LLD Section 6): at a few items a day it costs cents a month,
while the smallest PostgreSQL Flexible Server costs about $13-25 a month. Table Storage still gives what
the design needs:

- optimistic concurrency through ETags (the LLD's `UPDATE ... WHERE state = $expected`),
- atomic multi-row transactions inside one partition, which is how a state change, its audit event and
  its outbox row commit together (every row for a content item shares the item's partition),
- point reads and partition scans.

Entities are flat dicts of str, int, float and bool. Callers JSON-encode anything nested.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Iterable, Literal, Protocol

Entity = dict[str, Any]
Op = tuple[Literal["insert", "update", "upsert", "delete"], Entity]


class Conflict(Exception):
    """The row already exists, or it changed since it was read (ETag mismatch)."""


class Store(Protocol):
    def get(self, table: str, pk: str, rk: str) -> Entity | None: ...
    def query(self, table: str, pk: str | None = None, rk_prefix: str | None = None,
              where: dict[str, Any] | None = None, limit: int | None = None) -> list[Entity]: ...
    def insert(self, table: str, entity: Entity) -> Entity: ...
    def upsert(self, table: str, entity: Entity) -> Entity: ...
    def update(self, table: str, entity: Entity) -> Entity: ...   # requires entity["etag"]
    def delete(self, table: str, pk: str, rk: str) -> None: ...
    def transact(self, table: str, ops: Iterable[Op]) -> None: ...


def _clean(entity: Entity) -> Entity:
    return {k: v for k, v in entity.items() if k not in ("etag", "PartitionKey", "RowKey")}


# ----- SQLite (local development and tests) -----


class SqliteStore:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS entities (tbl TEXT, pk TEXT, rk TEXT, etag TEXT, body TEXT,"
            " PRIMARY KEY (tbl, pk, rk))")

    def _row(self, pk: str, rk: str, etag: str, body: str) -> Entity:
        return {"PartitionKey": pk, "RowKey": rk, **json.loads(body), "etag": etag}

    def get(self, table, pk, rk):
        with self._lock:
            row = self._conn.execute("SELECT pk, rk, etag, body FROM entities WHERE tbl=? AND pk=? AND rk=?",
                                     (table, pk, rk)).fetchone()
        return self._row(*row) if row else None

    def query(self, table, pk=None, rk_prefix=None, where=None, limit=None):
        sql, args = "SELECT pk, rk, etag, body FROM entities WHERE tbl=?", [table]
        if pk is not None:
            sql += " AND pk=?"
            args.append(pk)
        if rk_prefix is not None:
            sql += " AND rk >= ? AND rk < ?"
            args += [rk_prefix, rk_prefix + "￿"]
        sql += " ORDER BY pk, rk"
        with self._lock:
            rows = [self._row(*r) for r in self._conn.execute(sql, args).fetchall()]
        if where:
            rows = [r for r in rows if all(r.get(k) == v for k, v in where.items())]
        return rows[:limit] if limit else rows

    def _apply(self, table: str, op: str, entity: Entity) -> Entity:
        pk, rk = entity["PartitionKey"], entity["RowKey"]
        current = self._conn.execute("SELECT etag FROM entities WHERE tbl=? AND pk=? AND rk=?",
                                     (table, pk, rk)).fetchone()
        etag = uuid.uuid4().hex
        body = json.dumps(_clean(entity), default=str)
        if op == "insert":
            if current:
                raise Conflict(f"{table}/{pk}/{rk} exists")
            self._conn.execute("INSERT INTO entities VALUES (?,?,?,?,?)", (table, pk, rk, etag, body))
        elif op == "update":
            if not current or current[0] != entity.get("etag"):
                raise Conflict(f"{table}/{pk}/{rk} changed")
            self._conn.execute("UPDATE entities SET etag=?, body=? WHERE tbl=? AND pk=? AND rk=?",
                               (etag, body, table, pk, rk))
        elif op == "upsert":
            self._conn.execute("INSERT OR REPLACE INTO entities VALUES (?,?,?,?,?)", (table, pk, rk, etag, body))
        elif op == "delete":
            self._conn.execute("DELETE FROM entities WHERE tbl=? AND pk=? AND rk=?", (table, pk, rk))
        return {**entity, "etag": etag}

    def _single(self, table, op, entity):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                result = self._apply(table, op, entity)
                self._conn.execute("COMMIT")
                return result
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def insert(self, table, entity):
        return self._single(table, "insert", entity)

    def upsert(self, table, entity):
        return self._single(table, "upsert", entity)

    def update(self, table, entity):
        return self._single(table, "update", entity)

    def delete(self, table, pk, rk):
        self._single(table, "delete", {"PartitionKey": pk, "RowKey": rk})

    def transact(self, table, ops):
        ops = list(ops)
        if len({e["PartitionKey"] for _, e in ops}) > 1:
            raise ValueError("a transaction must stay inside one partition (as in Azure Table Storage)")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for op, entity in ops:
                    self._apply(table, op, entity)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise


# ----- Azure Table Storage -----


class TableStore:
    INT32 = 2 ** 31

    def __init__(self, account: str, credential):
        from azure.data.tables import TableServiceClient

        self.service = TableServiceClient(endpoint=f"https://{account}.table.core.windows.net", credential=credential)
        self._clients: dict[str, Any] = {}

    def _table(self, name: str):
        if name not in self._clients:
            self.service.create_table_if_not_exists(name)
            self._clients[name] = self.service.get_table_client(name)
        return self._clients[name]

    def _out(self, entity: Entity) -> Entity:
        from azure.data.tables import EdmType

        result = {}
        for key, value in entity.items():
            if key == "etag":
                continue
            if isinstance(value, int) and not isinstance(value, bool) and not -self.INT32 <= value < self.INT32:
                value = (value, EdmType.INT64)
            result[key] = value
        return result

    @staticmethod
    def _in(entity) -> Entity:
        result = {}
        for key, value in entity.items():
            result[key] = getattr(value, "value", value)
        result["etag"] = entity.metadata.get("etag")
        return result

    @staticmethod
    def _quote(value: str) -> str:
        return value.replace("'", "''")

    def get(self, table, pk, rk):
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return self._in(self._table(table).get_entity(pk, rk))
        except ResourceNotFoundError:
            return None

    def query(self, table, pk=None, rk_prefix=None, where=None, limit=None):
        parts = []
        if pk is not None:
            parts.append(f"PartitionKey eq '{self._quote(pk)}'")
        if rk_prefix is not None:
            parts.append(f"RowKey ge '{self._quote(rk_prefix)}' and RowKey lt '{self._quote(rk_prefix)}￿'")
        for key, value in (where or {}).items():
            if isinstance(value, bool):
                parts.append(f"{key} eq {str(value).lower()}")
            elif isinstance(value, (int, float)):
                parts.append(f"{key} eq {value}")
            else:
                parts.append(f"{key} eq '{self._quote(str(value))}'")
        client = self._table(table)
        rows = client.query_entities(" and ".join(parts)) if parts else client.list_entities()
        out = []
        for row in rows:
            out.append(self._in(row))
            if limit and len(out) >= limit:
                break
        return out

    def _errors(self, call, *args, **kwargs):
        from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceModifiedError

        try:
            return call(*args, **kwargs)
        except (ResourceExistsError, ResourceModifiedError) as exc:
            raise Conflict(str(exc)) from exc
        except HttpResponseError as exc:
            if exc.status_code in (409, 412):
                raise Conflict(str(exc)) from exc
            raise

    def insert(self, table, entity):
        meta = self._errors(self._table(table).create_entity, self._out(entity))
        return {**entity, "etag": meta.get("etag")}

    def upsert(self, table, entity):
        from azure.data.tables import UpdateMode

        meta = self._errors(self._table(table).upsert_entity, self._out(entity), mode=UpdateMode.REPLACE)
        return {**entity, "etag": meta.get("etag")}

    def update(self, table, entity):
        from azure.core import MatchConditions
        from azure.data.tables import UpdateMode

        meta = self._errors(self._table(table).update_entity, self._out(entity), mode=UpdateMode.REPLACE,
                            etag=entity["etag"], match_condition=MatchConditions.IfNotModified)
        return {**entity, "etag": meta.get("etag")}

    def delete(self, table, pk, rk):
        self._table(table).delete_entity(pk, rk)

    def transact(self, table, ops):
        from azure.core import MatchConditions
        from azure.data.tables import TableTransactionError, UpdateMode

        batch = []
        for op, entity in ops:
            if op == "insert":
                batch.append(("create", self._out(entity)))
            elif op == "upsert":
                batch.append(("upsert", self._out(entity), {"mode": UpdateMode.REPLACE}))
            elif op == "update":
                batch.append(("update", self._out(entity), {"mode": UpdateMode.REPLACE, "etag": entity["etag"],
                                                             "match_condition": MatchConditions.IfNotModified}))
            elif op == "delete":
                batch.append(("delete", {"PartitionKey": entity["PartitionKey"], "RowKey": entity["RowKey"]}))
        try:
            self._errors(self._table(table).submit_transaction, batch)
        except TableTransactionError as exc:
            raise Conflict(str(exc)) from exc
