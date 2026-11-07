"""Work queues: Azure Storage Queues in Azure, SQLite locally and in tests.

Why Storage Queues instead of Service Bus (LLD Section 4): Service Bus Standard (needed for duplicate
detection) is about $10 a month and Premium (private endpoints) about $670; Storage Queues cost
fractions of a cent at this volume. What Service Bus would have provided is covered here:

- duplicate detection: consumers check the item's state before acting, so a redelivered or duplicated
  message is acknowledged and dropped (LLD Section 5.3); message ids are content_id:stage:version;
- dead-lettering: after MAX_DELIVERIES a message moves to `<queue>-poison` and the console can replay it;
- scheduled redelivery: a deferred message is re-sent with a visibility delay.
"""
from __future__ import annotations

import base64
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

MAX_DELIVERIES = 5

Q_DISCOVER = "q-discover"
Q_SCRIPT = "q-script"
Q_RENDER = "q-render"
Q_PUBLISH = {"YOUTUBE": "q-publish-youtube", "INSTAGRAM": "q-publish-instagram"}
ALL_QUEUES = [Q_DISCOVER, Q_SCRIPT, Q_RENDER, *Q_PUBLISH.values()]


def poison(queue: str) -> str:
    return f"{queue}-poison"


@dataclass
class Message:
    id: str
    body: dict[str, Any]
    dequeue_count: int
    receipt: Any = None


class Queues(Protocol):
    def send(self, queue: str, body: dict[str, Any], delay_s: int = 0) -> None: ...
    def receive(self, queue: str, visibility_s: int, max_messages: int = 1) -> list[Message]: ...
    def delete(self, queue: str, message: Message) -> None: ...
    def depth(self, queue: str) -> int: ...
    def peek(self, queue: str, max_messages: int = 32) -> list[Message]: ...


class SqliteQueues:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, queue TEXT, body TEXT,"
                           " visible_at REAL, dequeue_count INTEGER, receipt TEXT)")

    def send(self, queue, body, delay_s=0):
        with self._lock:
            self._conn.execute("INSERT INTO messages VALUES (?,?,?,?,0,NULL)",
                               (uuid.uuid4().hex, queue, json.dumps(body), time.time() + delay_s))

    def receive(self, queue, visibility_s, max_messages=1):
        now = time.time()
        out = []
        with self._lock:
            rows = self._conn.execute("SELECT id, body, dequeue_count FROM messages WHERE queue=? AND visible_at<=?"
                                      " ORDER BY visible_at LIMIT ?", (queue, now, max_messages)).fetchall()
            for mid, body, count in rows:
                receipt = uuid.uuid4().hex
                self._conn.execute("UPDATE messages SET visible_at=?, dequeue_count=?, receipt=? WHERE id=?",
                                   (now + visibility_s, count + 1, receipt, mid))
                out.append(Message(mid, json.loads(body), count + 1, receipt))
        return out

    def delete(self, queue, message):
        with self._lock:
            self._conn.execute("DELETE FROM messages WHERE id=? AND receipt=?", (message.id, message.receipt))

    def depth(self, queue):
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM messages WHERE queue=?", (queue,)).fetchone()[0]

    def peek(self, queue, max_messages=32):
        with self._lock:
            rows = self._conn.execute("SELECT id, body, dequeue_count FROM messages WHERE queue=? LIMIT ?",
                                      (queue, max_messages)).fetchall()
        return [Message(mid, json.loads(body), count) for mid, body, count in rows]


class StorageQueues:
    def __init__(self, account: str, credential):
        from azure.storage.queue import QueueServiceClient

        self.service = QueueServiceClient(account_url=f"https://{account}.queue.core.windows.net",
                                          credential=credential)
        self._clients: dict[str, Any] = {}

    def _q(self, name: str):
        if name not in self._clients:
            client = self.service.get_queue_client(name)
            try:
                client.create_queue()
            except Exception:  # noqa: BLE001 - already exists
                pass
            self._clients[name] = client
        return self._clients[name]

    def send(self, queue, body, delay_s=0):
        text = base64.b64encode(json.dumps(body).encode()).decode()
        self._q(queue).send_message(text, visibility_timeout=min(delay_s, 7 * 86400) or None)

    def receive(self, queue, visibility_s, max_messages=1):
        out = []
        for msg in self._q(queue).receive_messages(messages_per_page=max_messages, visibility_timeout=visibility_s,
                                                   max_messages=max_messages):
            try:
                body = json.loads(base64.b64decode(msg.content))
            except ValueError:
                body = {"_unreadable": str(msg.content)[:500]}
            out.append(Message(msg.id, body, msg.dequeue_count, msg))
        return out

    def delete(self, queue, message):
        self._q(queue).delete_message(message.receipt)

    def depth(self, queue):
        return self._q(queue).get_queue_properties().approximate_message_count or 0

    def peek(self, queue, max_messages=32):
        out = []
        for msg in self._q(queue).peek_messages(max_messages=min(max_messages, 32)):
            try:
                body = json.loads(base64.b64decode(msg.content))
            except ValueError:
                body = {"_unreadable": str(msg.content)[:500]}
            out.append(Message(msg.id, body, msg.dequeue_count))
        return out
