"""Stockage SQLite (un seul fichier) avec recherche plein texte FTS5."""
from __future__ import annotations

import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS facts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fact        TEXT NOT NULL,
    category    TEXT NOT NULL DEFAULT 'general',
    tags        TEXT NOT NULL DEFAULT '',
    author      TEXT NOT NULL,
    updated_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    fact, category, tags,
    content='facts', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, fact, category, tags) VALUES (new.id, new.fact, new.category, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, fact, category, tags) VALUES ('delete', old.id, old.fact, old.category, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, fact, category, tags) VALUES ('delete', old.id, old.fact, old.category, old.tags);
    INSERT INTO facts_fts(rowid, fact, category, tags) VALUES (new.id, new.fact, new.category, new.tags);
END;

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    from_agent   TEXT NOT NULL,
    to_agent     TEXT NOT NULL,
    subject      TEXT NOT NULL,
    body         TEXT NOT NULL,
    thread_id    INTEGER,
    in_reply_to  INTEGER REFERENCES messages(id),
    created_at   TEXT NOT NULL,
    read_at      TEXT
);
CREATE INDEX IF NOT EXISTS messages_inbox ON messages(to_agent, read_at);

-- Métadonnées des fichiers transférés entre agents (le contenu est sur disque, jamais ici).
CREATE TABLE IF NOT EXISTS files (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    from_agent     TEXT NOT NULL,
    to_agent       TEXT NOT NULL,
    name           TEXT NOT NULL,
    subject        TEXT NOT NULL DEFAULT '',
    size           INTEGER NOT NULL,
    sha256         TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    downloaded_at  TEXT,
    message_id     INTEGER
);
CREATE INDEX IF NOT EXISTS files_to ON files(to_agent);
CREATE INDEX IF NOT EXISTS files_exp ON files(expires_at);

CREATE TABLE IF NOT EXISTS audit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    agent     TEXT NOT NULL,
    action    TEXT NOT NULL,
    target_id INTEGER,
    detail    TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _norm_tags(tags) -> str:
    if tags is None:
        return ""
    if isinstance(tags, str):
        tags = re.split(r"[,\s]+", tags)
    return ",".join(sorted({t.strip().lower().lstrip("#") for t in tags if t and t.strip()}))


def fts_query(text: str) -> str | None:
    """Transforme une requête libre en requête FTS5 sûre (mots en préfixe, combinés en OR)."""
    words = re.findall(r"\w+", text, flags=re.UNICODE)
    if not words:
        return None
    return " OR ".join(f'"{w}"*' for w in words[:20])


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self.on_close: list = []
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute("PRAGMA busy_timeout=5000")

    def close(self):
        for cb in getattr(self, "on_close", []):
            cb()
        self._conn.close()

    def _audit(self, agent: str, action: str, target_id: int | None, detail: str = ""):
        self._conn.execute(
            "INSERT INTO audit(ts, agent, action, target_id, detail) VALUES (?,?,?,?,?)",
            (now(), agent, action, target_id, detail[:500]),
        )

    @staticmethod
    def _fact(row) -> dict:
        d = dict(row)
        d["tags"] = [t for t in d.get("tags", "").split(",") if t]
        d.pop("rank", None)
        return d

    # ---------- mémoire ----------
    def remember(self, agent: str, fact: str, category: str, tags) -> dict:
        ts = now()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO facts(fact, category, tags, author, updated_by, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (fact.strip(), (category or "general").strip().lower(), _norm_tags(tags), agent, agent, ts, ts),
            )
            fid = cur.lastrowid
            self._audit(agent, "remember", fid)
            return self._fact(self._conn.execute("SELECT * FROM facts WHERE id=?", (fid,)).fetchone())

    def recall(self, query: str, limit: int = 10, category: str | None = None) -> list[dict]:
        q = fts_query(query)
        if not q:
            return []
        sql = ("SELECT f.*, bm25(facts_fts) AS rank FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid"
               " WHERE facts_fts MATCH ?")
        args: list = [q]
        if category:
            sql += " AND f.category = ?"
            args.append(category.strip().lower())
        sql += " ORDER BY rank LIMIT ?"
        args.append(limit)
        with self._lock:
            return [self._fact(r) for r in self._conn.execute(sql, args).fetchall()]

    def list_recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM facts ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
        return [self._fact(r) for r in rows]

    def get_fact(self, fid: int) -> dict | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM facts WHERE id=?", (fid,)).fetchone()
        return self._fact(r) if r else None

    def update(self, agent: str, fid: int, fact: str) -> dict | None:
        with self._lock:
            old = self._conn.execute("SELECT fact FROM facts WHERE id=?", (fid,)).fetchone()
            if not old:
                return None
            self._conn.execute("UPDATE facts SET fact=?, updated_by=?, updated_at=? WHERE id=?",
                               (fact.strip(), agent, now(), fid))
            self._audit(agent, "update", fid, "avant: " + old["fact"])
            return self._fact(self._conn.execute("SELECT * FROM facts WHERE id=?", (fid,)).fetchone())

    def forget(self, agent: str, fid: int) -> bool:
        with self._lock:
            old = self._conn.execute("SELECT fact FROM facts WHERE id=?", (fid,)).fetchone()
            if not old:
                return False
            self._conn.execute("DELETE FROM facts WHERE id=?", (fid,))
            self._audit(agent, "forget", fid, "contenu: " + old["fact"])
            return True

    # ---------- boîte aux lettres ----------
    def send(self, sender: str, to: str, subject: str, body: str, in_reply_to: int | None = None,
             thread_id: int | None = None) -> dict:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO messages(from_agent, to_agent, subject, body, thread_id, in_reply_to, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (sender, to, subject.strip()[:300], body, thread_id, in_reply_to, now()),
            )
            mid = cur.lastrowid
            if thread_id is None:
                self._conn.execute("UPDATE messages SET thread_id=? WHERE id=?", (mid, mid))
            self._audit(sender, "send_message", mid, f"to={to}")
            return dict(self._conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone())

    def inbox(self, agent: str, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE to_agent=? AND read_at IS NULL ORDER BY id LIMIT ?",
                (agent, limit)).fetchall()
        return [dict(r) for r in rows]

    def get_message(self, mid: int) -> dict | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        return dict(r) if r else None

    def mark_read(self, agent: str, mid: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE messages SET read_at=? WHERE id=? AND to_agent=? AND read_at IS NULL",
                (now(), mid, agent))
            return cur.rowcount == 1

    # ---------- fichiers (métadonnées) ----------
    def audit(self, agent: str, action: str, target_id: int | None, detail: str = "") -> None:
        with self._lock:
            self._audit(agent, action, target_id, detail)

    def file_reserve(self, sender: str, to: str, name: str, subject: str, size: int, sha256: str,
                     expires_at: str, max_files: int, quota_bytes: int) -> dict | None:
        """Réserve atomiquement une ligne si le quota le permet. Renvoie None si quota/nombre dépassé."""
        with self._lock:
            count, total = self._conn.execute("SELECT count(*), coalesce(sum(size),0) FROM files").fetchone()
            if count + 1 > max_files or total + size > quota_bytes:
                return None
            cur = self._conn.execute(
                "INSERT INTO files(from_agent,to_agent,name,subject,size,sha256,created_at,expires_at)"
                " VALUES (?,?,?,?,?,?,?,?)", (sender, to, name, subject, size, sha256, now(), expires_at))
            fid = cur.lastrowid
            self._audit(sender, "file_put", fid, f"to={to} size={size} sha256={sha256}")
            return dict(self._conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone())

    def file_get(self, fid: int) -> dict | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        return dict(r) if r else None

    def file_list(self, agent: str, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM files WHERE to_agent=? OR from_agent=? ORDER BY id DESC LIMIT ?",
                (agent, agent, limit)).fetchall()
        return [dict(r) for r in rows]

    def file_set_message(self, fid: int, message_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE files SET message_id=? WHERE id=?", (message_id, fid))

    def file_mark_downloaded(self, fid: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE files SET downloaded_at=? WHERE id=?", (now(), fid))

    def file_remove(self, agent: str, fid: int, action: str) -> dict | None:
        """Supprime la ligne et journalise (sans contenu). Renvoie la ligne supprimée, ou None."""
        with self._lock:
            r = self._conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
            if not r:
                return None
            self._conn.execute("DELETE FROM files WHERE id=?", (fid,))
            self._audit(agent, action, fid, f"from={r['from_agent']} to={r['to_agent']} size={r['size']} sha256={r['sha256']}")
            return dict(r)

    def file_expired_ids(self, at: str) -> list[int]:
        with self._lock:
            return [r[0] for r in self._conn.execute("SELECT id FROM files WHERE expires_at<=?", (at,)).fetchall()]

    def file_all_ids(self) -> dict[int, str]:
        """id -> created_at, pour la détection d'orphelins."""
        with self._lock:
            return {r[0]: r[1] for r in self._conn.execute("SELECT id, created_at FROM files").fetchall()}
