"""The pipeline's data layer: content items and their state machine, the transactional outbox, audit
events, publications and attempts, runs, topic fingerprints, quota and spend ledgers, and runtime flags.

Table layout (Azure Table Storage or SQLite):

  items     PK=content_id   RK=item | trend | script:0001 | policy | render | pub:YOUTUBE | pub:INSTAGRAM
                             | attempt:YOUTUBE:0001 | outbox:<message id> | audit:<time>:<n>
  runs      PK=run          RK=<idempotency key, e.g. 2026-11-07-slot2>
  topics    PK=topic        RK=<fingerprint>
  ledger    PK=<YYYY-MM-DD> RK=youtube_units | spend_inr | ig_posts
  flags     PK=flags        RK=<name>
  metrics   PK=<content_id> RK=<platform>

Every row for one content item shares its partition, so a state change, its audit event and its outbox
message commit atomically (LLD Section 5.4, "transactional outbox").
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .models import TRANSITIONS, PubState, State
from .queues import Queues
from .store import Conflict, Entity, Op, Store

log = logging.getLogger(__name__)

ITEMS, RUNS, TOPICS, LEDGER, FLAGS, METRICS = "items", "runs", "topics", "ledger", "flags", "metrics"
PACIFIC = ZoneInfo("America/Los_Angeles")  # YouTube quota resets at midnight Pacific Time


class StaleState(Exception):
    """Another worker moved the item first, or it is not in an expected state. Drop the message."""


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    return (value or now()).isoformat(timespec="seconds")


def quota_day(at: datetime | None = None) -> str:
    return (at or now()).astimezone(PACIFIC).strftime("%Y-%m-%d")


def ist_day(at: datetime | None = None) -> str:
    return (at or now()).astimezone(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")


def short_ref(content_id: str) -> str:
    return "ref-" + content_id.replace("-", "")[:10]


class Repo:
    def __init__(self, store: Store, queues: Queues):
        self.store = store
        self.queues = queues

    # ----- items -----

    def item(self, content_id: str) -> Entity | None:
        return self.store.get(ITEMS, content_id, "item")

    def row(self, content_id: str, rk: str) -> Entity | None:
        return self.store.get(ITEMS, content_id, rk)

    def json_row(self, content_id: str, rk: str) -> dict | None:
        row = self.row(content_id, rk)
        return json.loads(row["data"]) if row and row.get("data") else None

    def items(self, limit: int | None = None) -> list[Entity]:
        rows = self.store.query(ITEMS, rk_prefix="item", where=None)
        rows = [r for r in rows if r["RowKey"] == "item"]
        rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return rows[:limit] if limit else rows

    def create_item(self, trend: dict, run_key: str, platforms: list[str]) -> str:
        content_id = str(uuid.uuid4())
        at = iso()
        item = {
            "PartitionKey": content_id, "RowKey": "item", "state": State.DISCOVERED.value, "version": 1,
            "title": trend["title"][:200], "topic": trend["title"][:120], "category_id": trend["category_id"],
            "source_video_id": trend["source_video_id"], "fingerprint": trend.get("fingerprint", ""),
            "velocity": float(trend.get("velocity_score", 0)), "run_key": run_key, "platforms": ",".join(platforms),
            "approved_by": "", "last_error": "", "created_at": at, "updated_at": at, "ref": short_ref(content_id),
        }
        ops: list[Op] = [
            ("insert", item),
            ("insert", {"PartitionKey": content_id, "RowKey": "trend", "data": json.dumps(trend, default=str)}),
            ("insert", self._audit_row(content_id, "pipeline:discovery", "created", {"run": run_key})),
        ]
        msg = self._outbox_row(content_id, "q-script", "script", 1, {"content_id": content_id})
        ops.append(("insert", msg))
        self.store.transact(ITEMS, ops)
        self._relay([msg])
        return content_id

    def transition(self, content_id: str, expected: State | Iterable[State], new: State, actor: str,
                   details: dict | None = None, fields: dict | None = None, rows: list[Op] | None = None,
                   enqueue: list[tuple[str, str, dict]] | None = None) -> Entity:
        """Moves an item from one of `expected` to `new` with optimistic concurrency.

        `rows` are extra writes that commit in the same transaction (a script version, a publication...).
        `enqueue` is a list of (queue, stage, body) outbox messages committed with the change and relayed
        straight after it; the sweeper relays any that were committed but not sent.
        """
        expected_set = {expected} if isinstance(expected, State) else set(expected)
        item = self.item(content_id)
        if item is None:
            raise StaleState(f"{content_id} not found")
        current = State(item["state"])
        if current not in expected_set:
            raise StaleState(f"{content_id} is {current}, expected {sorted(expected_set)}")
        if new not in TRANSITIONS[current]:
            raise ValueError(f"illegal transition {current} -> {new}")
        version = int(item["version"]) + 1
        updated = {**item, **(fields or {}), "state": new.value, "version": version, "updated_at": iso()}
        ops: list[Op] = [("update", updated)]
        ops += rows or []
        ops.append(("insert", self._audit_row(content_id, actor, f"{current}->{new}", details or {})))
        messages = [self._outbox_row(content_id, q, stage, version, body) for q, stage, body in (enqueue or [])]
        ops += [("insert", m) for m in messages]
        try:
            self.store.transact(ITEMS, ops)
        except Conflict as exc:
            raise StaleState(f"{content_id} changed concurrently") from exc
        log.info("%s %s -> %s by %s", content_id, current, new, actor)
        self._relay(messages)
        return updated

    def update_fields(self, content_id: str, fields: dict, rows: list[Op] | None = None) -> None:
        """A change that is not a state transition (e.g. last_error), still guarded by the ETag."""
        for _ in range(5):
            item = self.item(content_id)
            if item is None:
                return
            try:
                self.store.transact(ITEMS, [("update", {**item, **fields, "updated_at": iso()})] + (rows or []))
                return
            except Conflict:
                continue
        raise StaleState(f"{content_id} kept changing")

    # ----- audit and outbox -----

    def _audit_row(self, content_id: str, actor: str, action: str, details: dict) -> Entity:
        return {"PartitionKey": content_id, "RowKey": f"audit:{iso()}:{uuid.uuid4().hex[:6]}", "actor": actor,
                "action": action, "details": json.dumps(details, default=str)[:30000], "at": iso()}

    def audit(self, content_id: str, actor: str, action: str, details: dict | None = None) -> None:
        self.store.insert(ITEMS, self._audit_row(content_id, actor, action, details or {}))

    def audit_events(self, content_id: str) -> list[Entity]:
        return self.store.query(ITEMS, pk=content_id, rk_prefix="audit:")

    def _outbox_row(self, content_id: str, queue: str, stage: str, version: int, body: dict) -> Entity:
        message_id = f"{content_id}:{stage}:{version}"
        return {"PartitionKey": content_id, "RowKey": f"outbox:{message_id}", "queue": queue,
                "message_id": message_id, "payload": json.dumps({**body, "message_id": message_id}),
                "sent": False, "created_at": iso()}

    def _relay(self, rows: list[Entity]) -> None:
        for row in rows:
            try:
                self.queues.send(row["queue"], json.loads(row["payload"]))
                current = self.store.get(ITEMS, row["PartitionKey"], row["RowKey"])
                if current:
                    self.store.update(ITEMS, {**current, "sent": True, "sent_at": iso()})
            except Exception as exc:  # noqa: BLE001 - the sweeper relays it later
                log.warning("outbox relay deferred for %s: %s", row["message_id"], exc)

    def relay_outbox(self, older_than_s: int = 60) -> int:
        """Sends committed outbox rows that were never marked sent (crash between commit and send)."""
        cutoff = iso(now() - timedelta(seconds=older_than_s))
        pending = [r for r in self.store.query(ITEMS, where={"sent": False})
                   if r["RowKey"].startswith("outbox:") and r.get("created_at", "") <= cutoff]
        self._relay(pending)
        return len(pending)

    # ----- publications -----

    def pub(self, content_id: str, platform: str) -> Entity | None:
        return self.row(content_id, f"pub:{platform}")

    def pubs(self, content_id: str) -> dict[str, Entity]:
        return {r["RowKey"].split(":", 1)[1]: r for r in self.store.query(ITEMS, pk=content_id, rk_prefix="pub:")}

    def new_pub_row(self, content_id: str, platform: str) -> Entity:
        return {"PartitionKey": content_id, "RowKey": f"pub:{platform}", "platform": platform,
                "state": PubState.PENDING.value, "upload_session": "", "ig_container_id": "", "platform_media_id": "",
                "url": "", "attempts": 0, "last_error": "", "error_class": "", "updated_at": iso()}

    def save_pub(self, pub: Entity, **fields) -> Entity:
        """Persists platform identifiers the moment they are known (LLD 5.2)."""
        merged = {**pub, **fields, "updated_at": iso()}
        return self.store.update(ITEMS, merged)

    def add_attempt(self, content_id: str, platform: str, number: int, outcome: str, http_status: int = 0,
                    error_code: str = "", detail: str = "") -> None:
        self.store.upsert(ITEMS, {"PartitionKey": content_id, "RowKey": f"attempt:{platform}:{number:04d}",
                                  "platform": platform, "attempt_no": number, "outcome": outcome,
                                  "http_status": http_status, "error_code": error_code, "detail": detail[:1000],
                                  "at": iso()})

    def attempts(self, content_id: str) -> list[Entity]:
        return self.store.query(ITEMS, pk=content_id, rk_prefix="attempt:")

    # ----- runs and topics -----

    def claim_run(self, key: str, kind: str = "scheduled") -> bool:
        try:
            self.store.insert(RUNS, {"PartitionKey": "run", "RowKey": key, "status": "RUNNING", "kind": kind,
                                     "started_at": iso(), "finished_at": "", "found": 0, "created": 0,
                                     "skipped": 0, "error": ""})
            return True
        except Conflict:
            return False

    def finish_run(self, key: str, status: str, **fields) -> None:
        run = self.store.get(RUNS, "run", key)
        if run:
            self.store.upsert(RUNS, {**run, **fields, "status": status, "finished_at": iso()})

    def runs(self, limit: int = 30) -> list[Entity]:
        rows = self.store.query(RUNS, pk="run")
        rows.sort(key=lambda r: r.get("started_at", ""), reverse=True)
        return rows[:limit]

    def topic_seen(self, fingerprint: str, days: int) -> bool:
        row = self.store.get(TOPICS, "topic", fingerprint)
        return bool(row and row.get("seen_at", "") >= iso(now() - timedelta(days=days)))

    def mark_topic(self, fingerprint: str, content_id: str) -> None:
        self.store.upsert(TOPICS, {"PartitionKey": "topic", "RowKey": fingerprint, "seen_at": iso(),
                                   "content_id": content_id})

    # ----- ledgers -----

    def ledger_add(self, day: str, name: str, amount: float) -> float:
        for _ in range(10):
            row = self.store.get(LEDGER, day, name)
            try:
                if row is None:
                    self.store.insert(LEDGER, {"PartitionKey": day, "RowKey": name, "value": float(amount)})
                    return float(amount)
                value = float(row.get("value", 0)) + amount
                self.store.update(LEDGER, {**row, "value": value})
                return value
            except Conflict:
                continue
        raise StaleState("ledger contention")

    def ledger(self, day: str, name: str) -> float:
        row = self.store.get(LEDGER, day, name)
        return float(row["value"]) if row else 0.0

    # ----- runtime flags (editable from the console) -----

    def flag(self, name: str, default: Any = None) -> Any:
        row = self.store.get(FLAGS, "flags", name)
        return json.loads(row["value"]) if row else default

    def set_flag(self, name: str, value: Any, actor: str) -> None:
        self.store.upsert(FLAGS, {"PartitionKey": "flags", "RowKey": name, "value": json.dumps(value),
                                  "changed_by": actor, "changed_at": iso()})
        log.info("flag %s set to %r by %s", name, value, actor)

    def flags(self) -> dict[str, Any]:
        return {r["RowKey"]: json.loads(r["value"]) for r in self.store.query(FLAGS, pk="flags")}

    # ----- metrics -----

    def save_metrics(self, content_id: str, platform: str, **values) -> None:
        self.store.upsert(METRICS, {"PartitionKey": content_id, "RowKey": platform, **values, "updated_at": iso()})

    def all_metrics(self) -> list[Entity]:
        return self.store.query(METRICS)
