"""SQLite storage backend optimized for low-resource hosts (e.g. Raspberry Pi)."""

from __future__ import annotations

import json
import sqlite3
from email.utils import parsedate_to_datetime
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

DB_FILE = "newsmonitor.db"


def parse_time(value: str):
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        elif "+" not in value and len(value) > 10 and value.count("-") <= 2:
            value += "+00:00"
        return datetime.fromisoformat(value)
    except Exception:
        try:
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None


class Storage:
    def __init__(self, path: str = DB_FILE):
        self.path = path
        self._init_db()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self):
        with self.connect() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS kv (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS news_items (
                  id TEXT PRIMARY KEY,
                  time TEXT,
                  source TEXT,
                  source_id TEXT,
                  type TEXT,
                  title TEXT,
                  text TEXT,
                  url TEXT,
                  summary TEXT,
                  category TEXT,
                  importance INTEGER,
                  is_duplicate INTEGER,
                  matched_keywords TEXT
                )
                """
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_news_time ON news_items(time DESC)")
            c.execute("CREATE TABLE IF NOT EXISTS seen_ids (id TEXT PRIMARY KEY)")
            c.execute("CREATE TABLE IF NOT EXISTS read_ids (id TEXT PRIMARY KEY)")

    def set_kv(self, key: str, value: Any):
        with self.connect() as c:
            c.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def get_kv(self, key: str, default: Any):
        with self.connect() as c:
            row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def load_seen_ids(self) -> set[str]:
        with self.connect() as c:
            rows = c.execute("SELECT id FROM seen_ids").fetchall()
        return {r["id"] for r in rows}

    def save_seen_ids(self, ids: set[str], limit: int = 10000):
        final = list(ids)[-limit:]
        with self.connect() as c:
            c.execute("DELETE FROM seen_ids")
            c.executemany("INSERT OR IGNORE INTO seen_ids(id) VALUES(?)", [(x,) for x in final])

    def load_read_ids(self) -> set[str]:
        with self.connect() as c:
            rows = c.execute("SELECT id FROM read_ids").fetchall()
        return {r["id"] for r in rows}

    def save_read_ids(self, ids: set[str]):
        with self.connect() as c:
            c.execute("DELETE FROM read_ids")
            c.executemany("INSERT OR IGNORE INTO read_ids(id) VALUES(?)", [(x,) for x in ids])

    def upsert_items(self, items: list[dict]):
        with self.connect() as c:
            c.executemany(
                """
                INSERT INTO news_items(id,time,source,source_id,type,title,text,url,summary,category,importance,is_duplicate,matched_keywords)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  time=excluded.time,
                  source=excluded.source,
                  source_id=excluded.source_id,
                  type=excluded.type,
                  title=excluded.title,
                  text=excluded.text,
                  url=excluded.url,
                  summary=excluded.summary,
                  category=excluded.category,
                  importance=excluded.importance,
                  is_duplicate=excluded.is_duplicate,
                  matched_keywords=excluded.matched_keywords
                """,
                [
                    (
                        it.get("id", ""),
                        it.get("time", ""),
                        it.get("source", ""),
                        it.get("source_id", ""),
                        it.get("type", ""),
                        it.get("title", ""),
                        it.get("text", ""),
                        it.get("url", ""),
                        it.get("summary", ""),
                        it.get("category", ""),
                        int(it.get("importance", 5) or 5),
                        1 if it.get("is_duplicate", False) else 0,
                        json.dumps(it.get("matched_keywords", []), ensure_ascii=False),
                    )
                    for it in items
                    if it.get("id")
                ],
            )

    def append_item(self, item: dict):
        self.upsert_items([item])

    def load_items(self) -> list[dict]:
        with self.connect() as c:
            rows = c.execute("SELECT * FROM news_items").fetchall()
        data = []
        for r in rows:
            data.append(
                {
                    "id": r["id"],
                    "time": r["time"],
                    "source": r["source"],
                    "source_id": r["source_id"],
                    "type": r["type"],
                    "title": r["title"],
                    "text": r["text"],
                    "url": r["url"],
                    "summary": r["summary"],
                    "category": r["category"],
                    "importance": int(r["importance"] or 5),
                    "is_duplicate": bool(r["is_duplicate"]),
                    "matched_keywords": json.loads(r["matched_keywords"] or "[]"),
                }
            )
        data.sort(
            key=lambda x: parse_time(x.get("time", "")) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return data

    def cleanup(self, keep_days: int, max_items: int):
        cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
        items = self.load_items()
        filtered = []
        for it in items:
            t = parse_time(it.get("time", ""))
            if t is None or t >= cutoff:
                filtered.append(it)
        filtered = filtered[:max_items]
        keep_ids = {x["id"] for x in filtered}
        with self.connect() as c:
            if keep_ids:
                marks = ",".join("?" for _ in keep_ids)
                c.execute(f"DELETE FROM news_items WHERE id NOT IN ({marks})", tuple(keep_ids))
            else:
                c.execute("DELETE FROM news_items")
        return filtered

    def export_news_payload(self, ai_enabled: bool = False, new_count: int = 0):
        items = self.load_items()
        return {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "ai_enabled": ai_enabled,
            "total": len(items),
            "new_count": new_count,
            "items": items,
        }
