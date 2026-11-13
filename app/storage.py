import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from app.classification import CATEGORIES, PRIORITIES

SCHEMA = """
CREATE TABLE IF NOT EXISTS gmail_messages (
    account TEXT NOT NULL,
    message_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    subject TEXT NOT NULL,
    received_at TEXT NOT NULL,
    category TEXT NOT NULL,
    priority TEXT NOT NULL,
    summary TEXT NOT NULL,
    reason TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    method TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    gmail_link TEXT NOT NULL,
    search_link TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    edited INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account, message_id)
);
CREATE TABLE IF NOT EXISTS gmail_sync (
    account TEXT PRIMARY KEY,
    synced_at TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path):
        self.path = path
        with self.connection() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def contains(self, account, message_id):
        with self.connection() as db:
            return db.execute(
                "SELECT 1 FROM gmail_messages WHERE account=? AND message_id=?",
                (account, message_id),
            ).fetchone() is not None

    def add(self, account, mail, result):
        value = result.classification
        with self.connection() as db:
            cursor = db.execute("""
                INSERT INTO gmail_messages (
                    account, message_id, thread_id, sender, subject, received_at,
                    category, priority, summary, reason, recommendation, method,
                    model, prompt_version, gmail_link, search_link, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account, message_id) DO NOTHING
                """, (account, mail.message_id, mail.thread_id, mail.sender, mail.subject,
                      mail.received_at, value.category, value.priority, value.summary,
                      value.reason, value.recommendation, result.method, result.model,
                      result.prompt_version, mail.gmail_link, mail.search_link,
                      datetime.now(timezone.utc).isoformat()))
            return cursor.rowcount == 1

    def update(self, account, message_id, category, priority):
        if category not in CATEGORIES or priority not in PRIORITIES:
            raise ValueError("無効なカテゴリまたは優先度")
        with self.connection() as db:
            db.execute("""
                UPDATE gmail_messages SET category=?, priority=?, edited=1
                WHERE account=? AND message_id=?
                """, (category, priority, account, message_id))

    def rows(self, account):
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM gmail_messages WHERE account=? ORDER BY received_at DESC, message_id",
                (account,),
            )]

    def finish(self, account):
        with self.connection() as db:
            db.execute("""
                INSERT INTO gmail_sync VALUES (?, ?)
                ON CONFLICT(account) DO UPDATE SET synced_at=excluded.synced_at
                WHERE excluded.synced_at > gmail_sync.synced_at
                """, (account, datetime.now(timezone.utc).isoformat()))

    def last_sync(self, account):
        with self.connection() as db:
            row = db.execute("SELECT synced_at FROM gmail_sync WHERE account=?", (account,)).fetchone()
            return row["synced_at"] if row else None
