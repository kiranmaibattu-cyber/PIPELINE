"""Persistent, process-safe at-least-once delivery queue for management records."""
import json
from pathlib import Path
import sqlite3


class SyncStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "sync.sqlite"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, kind TEXT, body TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, request TEXT, result TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")

    def bind_edge(self, edge_id):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('edge_id',?)", (edge_id,))
            current = db.execute("SELECT value FROM metadata WHERE key='edge_id'").fetchone()[0]
            if current != edge_id:
                raise ValueError("sync volume belongs to a different edge_id")

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def enqueue(self, record_id, kind, payload):
        body = json.dumps(payload, allow_nan=False, sort_keys=True)
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO outbox VALUES (?,?,?)", (record_id, kind, body))

    def pending(self, limit=20):
        with self.connect() as db:
            return [(i, k, json.loads(b)) for i, k, b in db.execute(
                "SELECT id,kind,body FROM outbox ORDER BY rowid LIMIT ?", (limit,))]

    def ack(self, record_id):
        with self.connect() as db:
            db.execute("DELETE FROM outbox WHERE id=?", (record_id,))

    def count(self):
        with self.connect() as db:
            return db.execute("SELECT count(*) FROM outbox").fetchone()[0]
