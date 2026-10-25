"""Persistent memory in SQLite with FTS5 full-text search.

What gets stored: things you or the assistant explicitly save (notes, preferences, project
facts, decisions), task plans, and — if memory.save_summaries is on — short summaries made when
a long conversation is compressed. Raw transcripts and audio are never stored.

Why no embeddings: FTS5 with BM25 ranking is instant, costs no VRAM, and is good enough for
"what did I say about my ROS 2 workspace". Embeddings would need a second model resident next
to the LLM on a 4 GB card. Revisit if recall quality becomes the bottleneck.

Durability: WAL journal + synchronous=NORMAL, so a crash or power loss cannot corrupt the DB
(at worst the last transaction is lost).
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path

KINDS = ("note", "preference", "project", "decision", "summary", "fact")

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    project TEXT DEFAULT '',
    pinned INTEGER DEFAULT 0,
    source TEXT DEFAULT 'user',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content, project, kind, content='memories', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, project, kind) VALUES (new.id, new.content, new.project, new.kind);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, project, kind) VALUES ('delete', old.id, old.content, old.project, old.kind);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, project, kind) VALUES ('delete', old.id, old.content, old.project, old.kind);
    INSERT INTO memories_fts(rowid, content, project, kind) VALUES (new.id, new.content, new.project, new.kind);
END;
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    steps TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

STOPWORDS = set("""a an the and or but if then else of to in on at by for with from about into over after before
is are was were be been being do does did have has had i me my we our you your it its this that these those
what which who whom how why when where can could should would will shall may might must not no yes please
tell show give get make let just also very really some any""".split())


class MemoryStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)
        path.chmod(0o600)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def integrity_ok(self) -> bool:
        with self._lock:
            return self._db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    # ------------------------------------------------------------------ memories
    def add(self, content: str, kind: str = "note", project: str = "", pinned: bool = False,
            source: str = "user") -> int:
        content = content.strip()
        if not content:
            raise ValueError("empty memory")
        if kind not in KINDS:
            kind = "note"
        now = time.time()
        with self._lock:
            # De-duplicate exact repeats instead of piling them up.
            row = self._db.execute("SELECT id FROM memories WHERE content=? AND kind=?", (content, kind)).fetchone()
            if row:
                self._db.execute("UPDATE memories SET updated_at=? WHERE id=?", (now, row["id"]))
                return int(row["id"])
            cur = self._db.execute(
                "INSERT INTO memories(kind, content, project, pinned, source, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)", (kind, content[:4000], project[:120], int(pinned), source, now, now))
            return int(cur.lastrowid)

    def delete(self, mem_id: int) -> bool:
        with self._lock:
            return self._db.execute("DELETE FROM memories WHERE id=?", (int(mem_id),)).rowcount > 0

    def clear(self) -> int:
        with self._lock:
            n = self._db.execute("DELETE FROM memories").rowcount
            self._db.execute("INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')")
            return n

    def list(self, limit: int = 100, kind: str | None = None) -> list[dict]:
        q = "SELECT * FROM memories"
        args: tuple = ()
        if kind:
            q += " WHERE kind=?"
            args = (kind,)
        q += " ORDER BY pinned DESC, updated_at DESC LIMIT ?"
        with self._lock:
            return [dict(r) for r in self._db.execute(q, (*args, int(limit)))]

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    @staticmethod
    def fts_query(text: str) -> str:
        words = [w for w in re.findall(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*", text.lower())
                 if len(w) > 2 and w not in STOPWORDS]
        words = list(dict.fromkeys(words))[:12]
        # Quote every term: user text must never be parsed as FTS syntax.
        return " OR ".join('"' + w.replace('"', "") + '"' for w in words)

    def search(self, text: str, limit: int = 5) -> list[dict]:
        q = self.fts_query(text)
        if not q:
            return []
        with self._lock:
            rows = self._db.execute(
                "SELECT m.*, bm25(memories_fts) AS score FROM memories_fts f JOIN memories m ON m.id=f.rowid "
                "WHERE memories_fts MATCH ? ORDER BY score LIMIT ?", (q, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def pinned(self, limit: int = 5) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT * FROM memories WHERE pinned=1 ORDER BY updated_at DESC LIMIT ?",
                (int(limit),))]

    def apply_retention(self, days: float) -> int:
        """Drop old unpinned summaries/notes. Preferences, decisions and pinned items stay."""
        if days <= 0:
            return 0
        cutoff = time.time() - days * 86400
        with self._lock:
            return self._db.execute(
                "DELETE FROM memories WHERE pinned=0 AND kind IN ('summary','note','fact') AND updated_at < ?",
                (cutoff,)).rowcount

    # ------------------------------------------------------------------ tasks
    def create_task(self, title: str, steps: list[str]) -> dict:
        now = time.time()
        st = [{"text": s.strip()[:300], "status": "pending"} for s in steps if s.strip()][:20]
        with self._lock:
            cur = self._db.execute("INSERT INTO tasks(title, status, steps, created_at, updated_at) VALUES (?,?,?,?,?)",
                                   (title.strip()[:200], "active", json.dumps(st), now, now))
        return self.get_task(int(cur.lastrowid))

    def get_task(self, task_id: int) -> dict:
        with self._lock:
            r = self._db.execute("SELECT * FROM tasks WHERE id=?", (int(task_id),)).fetchone()
        if not r:
            raise KeyError(f"no task {task_id}")
        d = dict(r)
        d["steps"] = json.loads(d["steps"])
        return d

    def update_step(self, task_id: int, step: int, status: str) -> dict:
        if status not in ("pending", "in_progress", "done", "skipped", "failed"):
            raise ValueError(f"bad status {status}")
        t = self.get_task(task_id)
        if not 1 <= step <= len(t["steps"]):
            raise ValueError(f"task {task_id} has {len(t['steps'])} steps")
        t["steps"][step - 1]["status"] = status
        done = all(s["status"] in ("done", "skipped") for s in t["steps"])
        with self._lock:
            self._db.execute("UPDATE tasks SET steps=?, status=?, updated_at=? WHERE id=?",
                             (json.dumps(t["steps"]), "done" if done else "active", time.time(), int(task_id)))
        return self.get_task(task_id)

    def list_tasks(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM tasks ORDER BY status='done', updated_at DESC LIMIT ?",
                                    (int(limit),)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["steps"] = json.loads(d["steps"])
            out.append(d)
        return out

    def delete_task(self, task_id: int) -> bool:
        with self._lock:
            return self._db.execute("DELETE FROM tasks WHERE id=?", (int(task_id),)).rowcount > 0
