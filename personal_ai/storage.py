import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .memory import MemoryStore


def now():
    return datetime.now(timezone.utc).isoformat()


class Store(MemoryStore):
    def __init__(self, path: Path):
        self.db = sqlite3.connect(str(path), timeout=1.0)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA foreign_keys = ON;
            PRAGMA secure_delete = ON;
            CREATE TABLE IF NOT EXISTS state (
                id INTEGER PRIMARY KEY CHECK (id = 1), epoch INTEGER NOT NULL
            );
            INSERT OR IGNORE INTO state VALUES (1, 0);
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY, epoch INTEGER NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL,
                source TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS operations (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL,
                metadata TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL,
                finished_at TEXT
            );
        """)

        self.init_memory()

    def close(self):
        self.db.close()

    def epoch(self):
        return self.db.execute("SELECT epoch FROM state WHERE id=1").fetchone()[0]

    def message(self, role, content, epoch=None, *, conversation_ids=(), memory_ids=()):
        with self.db:
            if role == "user" and re.search(r"(?i)保存(?:しない|禁止|しないで)|覚えないで|do not (?:store|remember)|don.t (?:store|remember)", content):
                self.db.execute("UPDATE memory_policy SET automatic_disabled=1 WHERE id=1")
            current = self.epoch()
            version = current if epoch is None else epoch
            stale = version != current or self.suppressed(content)
            for table, refs in (('conversations', conversation_ids), ('memories', memory_ids)):
                for ref in refs:
                    parent = self.db.execute('SELECT status FROM ' + table + ' WHERE id=?', (ref,)).fetchone()
                    stale = stale or parent is None or parent['status'] != 'active'
            cursor = self.db.execute(
                "INSERT INTO conversations(epoch,role,content,created_at,status) VALUES (?,?,?,?,?)",
                (version, role, content, now(), 'stale' if stale else 'active'),
            )
            self.link_sources('conversation', cursor.lastrowid,
                              conversation_ids=conversation_ids, memory_ids=memory_ids)
            return cursor.lastrowid

    def history_rows(self, limit=12):
        rows = self.db.execute(
            "SELECT id,role,content FROM conversations WHERE status='active' AND epoch=? AND role IN ('user','assistant') ORDER BY id DESC LIMIT ?",
            (self.epoch(), limit),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def history(self, limit=12):
        return [{k: row[k] for k in ('role', 'content')} for row in self.history_rows(limit)]

    def start_operation(self, name, metadata=None):
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO operations(name,status,metadata,created_at) VALUES (?,?,?,?)",
                (name, "started", json.dumps(metadata or {}, ensure_ascii=False), now()),
            )
            return cursor.lastrowid

    def operation_metadata(self, operation_id, metadata):
        with self.db:
            self.db.execute("UPDATE operations SET metadata=? WHERE id=?",
                            (json.dumps(metadata, ensure_ascii=False), operation_id))

    def finish_operation(self, operation_id, status, error=None):
        with self.db:
            self.db.execute(
                "UPDATE operations SET status=?,error=?,finished_at=? WHERE id=?",
                (status, error, now(), operation_id),
            )

    def operations(self, limit=30):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM operations ORDER BY id DESC LIMIT ?", (limit,)
        )]
