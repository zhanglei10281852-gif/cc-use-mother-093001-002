"""SQLite 事件库：所有状态变化先落库再应用，重启后按序重放恢复。"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime

from .clock import iso, parse_iso


@dataclass(frozen=True)
class Event:
    seq: int
    storm_id: str
    event_type: str
    payload: dict
    occurred_at: datetime


class EventStore:
    """追加式事件存储，附带 meta 表保存虚拟时钟偏移等元数据。"""

    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS events(
                  seq INTEGER PRIMARY KEY AUTOINCREMENT,
                  storm_id TEXT NOT NULL,
                  event_type TEXT NOT NULL,
                  payload TEXT NOT NULL,
                  occurred_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meta(
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );
                """
            )
            self._conn.commit()

    def append(self, storm_id: str, event_type: str, payload: dict,
               occurred_at: datetime) -> Event:
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events(storm_id,event_type,payload,occurred_at)"
                " VALUES(?,?,?,?)",
                (storm_id, event_type, data, iso(occurred_at)),
            )
            self._conn.commit()
            seq = cur.lastrowid
        # 经 JSON 往返，断开与调用方对象的可变引用
        return Event(seq, storm_id, event_type, json.loads(data), occurred_at)

    def load_all(self) -> list[Event]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq,storm_id,event_type,payload,occurred_at"
                " FROM events ORDER BY seq"
            ).fetchall()
        return [
            Event(row["seq"], row["storm_id"], row["event_type"],
                  json.loads(row["payload"]), parse_iso(row["occurred_at"]))
            for row in rows
        ]

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
