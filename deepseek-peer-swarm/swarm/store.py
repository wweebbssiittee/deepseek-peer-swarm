from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_run ON events(run_id,id);
            CREATE TABLE IF NOT EXISTS checkpoints (run_id TEXT, peer TEXT, data TEXT NOT NULL, PRIMARY KEY(run_id,peer));
            CREATE TABLE IF NOT EXISTS api_calls (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS api_calls_run ON api_calls(run_id,status);
        """)

    def save(self, state):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO runs VALUES (?,?)", (state["run"]["id"], json.dumps(state)))

    def get(self, run_id):
        row = self.db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError(run_id)
        return json.loads(row[0])

    def all(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT data FROM runs ORDER BY rowid DESC")]

    def event(self, run_id, kind, agent_id=None, data=None):
        event = {"kind": kind, "agent_id": agent_id, "data": data or {}, "created_at": now()}
        with self.db:
            cursor = self.db.execute("INSERT INTO events(run_id,data) VALUES (?,?)", (run_id, json.dumps(event)))
        return {"id": cursor.lastrowid, **event}

    def events(self, run_id, limit=200):
        rows = self.db.execute("SELECT id,data FROM events WHERE run_id=? ORDER BY id DESC LIMIT ?", (run_id, limit)).fetchall()
        return [{"id": row[0], **json.loads(row[1])} for row in reversed(rows)]

    def checkpoint(self, run_id, peer, messages=None):
        if messages is None:
            row = self.db.execute("SELECT data FROM checkpoints WHERE run_id=? AND peer=?", (run_id, peer)).fetchone()
            return json.loads(row[0]) if row else []
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?,?)", (run_id, peer, json.dumps(messages)))

    def close(self):
        self.db.close()

    def record_call(self, state, call):
        """The spending ledger and its run balance always commit together."""
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO runs VALUES (?,?)", (state["run"]["id"], json.dumps(state)))
            self.db.execute("INSERT OR REPLACE INTO api_calls VALUES (?,?,?,?)", (call["id"], state["run"]["id"], call["status"], json.dumps(call)))

    def calls(self, run_id, status=None, limit=200):
        query = "SELECT data FROM api_calls WHERE run_id=?"
        parameters = [run_id]
        if status:
            query += " AND status=?"
            parameters.append(status)
        query += " ORDER BY rowid DESC"
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        return [json.loads(row[0]) for row in self.db.execute(query, parameters)]
