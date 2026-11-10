"""SQLite persistence (WAL). One connection per thread."""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS products(
  upc TEXT PRIMARY KEY, description TEXT, brand TEXT, size TEXT, data TEXT, last_seen_at TEXT);
CREATE TABLE IF NOT EXISTS choices(
  key TEXT, upc TEXT, times_chosen INTEGER NOT NULL DEFAULT 0, first_chosen_at TEXT, last_chosen_at TEXT,
  pinned INTEGER NOT NULL DEFAULT 0, source TEXT, PRIMARY KEY (key, upc));
CREATE TABLE IF NOT EXISTS rejections(
  key TEXT NOT NULL DEFAULT '', upc TEXT, reason TEXT, created_at TEXT, PRIMARY KEY (key, upc));
CREATE TABLE IF NOT EXISTS diet_overrides(
  upc TEXT, diet TEXT, status TEXT, note TEXT, created_at TEXT, PRIMARY KEY (upc, diet));
CREATE TABLE IF NOT EXISTS imports(file_sha256 TEXT PRIMARY KEY, imported_at TEXT, trips INTEGER, lines INTEGER);
CREATE TABLE IF NOT EXISTS aliases(alias TEXT PRIMARY KEY, key TEXT);
CREATE TABLE IF NOT EXISTS draft_list(id INTEGER PRIMARY KEY CHECK (id = 1), text TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS runs(
  run_id TEXT PRIMARY KEY, created_at TEXT, source TEXT, raw_text TEXT, source_fingerprint TEXT,
  location_id TEXT, modality TEXT, list_diet TEXT, status TEXT, resolved_at TEXT, submitted_at TEXT,
  message TEXT, version INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS run_lines(
  run_id TEXT, line_no INTEGER, raw TEXT, text TEXT, key TEXT, quantity INTEGER, diet TEXT, note TEXT,
  decision TEXT, reason TEXT, candidates TEXT, chosen_upc TEXT, sale_note TEXT, cheaper_upc TEXT,
  decided_by TEXT, decided_at TEXT, outcome TEXT NOT NULL DEFAULT 'pending', outcome_detail TEXT, sent_at TEXT,
  PRIMARY KEY (run_id, line_no));
CREATE TABLE IF NOT EXISTS settings(name TEXT PRIMARY KEY, value TEXT);
"""

OPEN_STATUSES = ("resolving", "review", "submitting")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.db.executescript(SCHEMA)
        self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @property
    def db(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=5000")
            self._local.conn = c
        return c

    class _Tx:
        def __init__(self, conn):
            self.c = conn

        def __enter__(self):
            self.c.execute("BEGIN IMMEDIATE")
            return self.c

        def __exit__(self, et, ev, tb):
            self.c.execute("COMMIT" if et is None else "ROLLBACK")

    def tx(self):
        return self._Tx(self.db)

    # ------------------------------------------------------------ settings
    def setting(self, name: str, default: str | None = None) -> str | None:
        r = self.db.execute("SELECT value FROM settings WHERE name=?", (name,)).fetchone()
        return r["value"] if r else default

    def set_setting(self, name: str, value: str) -> None:
        self.db.execute("INSERT INTO settings(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                        (name, value))

    def form_secret(self) -> str:
        s = self.setting("form_secret")
        if not s:
            s = secrets.token_urlsafe(32)
            self.set_setting("form_secret", s)
        return s

    # ------------------------------------------------------------ draft
    def draft(self) -> str:
        r = self.db.execute("SELECT text FROM draft_list WHERE id=1").fetchone()
        return r["text"] if r else ""

    def save_draft(self, text: str) -> None:
        self.db.execute("INSERT INTO draft_list(id,text,updated_at) VALUES(1,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET text=excluded.text, updated_at=excluded.updated_at",
                        (text, now()))

    # ------------------------------------------------------------ products
    def cache_products(self, products) -> None:
        with self.tx() as c:
            for p in products:
                c.execute("INSERT INTO products(upc,description,brand,size,data,last_seen_at) VALUES(?,?,?,?,?,?) "
                          "ON CONFLICT(upc) DO UPDATE SET description=excluded.description, brand=excluded.brand, "
                          "size=excluded.size, data=excluded.data, last_seen_at=excluded.last_seen_at",
                          (p.upc, p.description, p.brand, p.size, json.dumps(p.to_cache()), now()))

    def product_info(self, upc: str) -> dict | None:
        r = self.db.execute("SELECT data FROM products WHERE upc=?", (upc,)).fetchone()
        return json.loads(r["data"]) if r else None

    # ------------------------------------------------------------ memory
    def memory(self) -> dict:
        """Snapshot of everything ranking needs, safe to pass to worker threads."""
        db = self.db
        choices = [dict(r) for r in db.execute(
            "SELECT c.*, p.brand, p.size FROM choices c LEFT JOIN products p USING(upc)")]
        rejections = {(r["key"], r["upc"]) for r in db.execute("SELECT key, upc FROM rejections")}
        overrides = {(r["upc"], r["diet"]): r["status"] for r in db.execute("SELECT upc, diet, status FROM diet_overrides")}
        aliases = {r["alias"]: r["key"] for r in db.execute("SELECT alias, key FROM aliases")}
        return {"choices": choices, "rejections": rejections, "overrides": overrides, "aliases": aliases}

    def record_choice(self, key: str, upc: str, source: str = "session", when: str | None = None) -> None:
        when = when or now()
        self.db.execute(
            "INSERT INTO choices(key,upc,times_chosen,first_chosen_at,last_chosen_at,source) VALUES(?,?,1,?,?,?) "
            "ON CONFLICT(key,upc) DO UPDATE SET times_chosen=times_chosen+1, "
            "first_chosen_at=COALESCE(MIN(first_chosen_at, excluded.first_chosen_at), excluded.first_chosen_at), "
            "last_chosen_at=COALESCE(MAX(last_chosen_at, excluded.last_chosen_at), excluded.last_chosen_at)",
            (key, upc, when, when, source))

    def set_pin(self, key: str, upc: str, pinned: bool) -> None:
        if pinned:
            with self.tx() as c:
                c.execute("UPDATE choices SET pinned=0 WHERE key=?", (key,))
                c.execute("INSERT INTO choices(key,upc,times_chosen,first_chosen_at,last_chosen_at,pinned,source) "
                          "VALUES(?,?,0,NULL,NULL,1,'manual') ON CONFLICT(key,upc) DO UPDATE SET pinned=1", (key, upc))
        else:
            self.db.execute("UPDATE choices SET pinned=0 WHERE key=? AND upc=?", (key, upc))

    def forget(self, key: str, upc: str | None = None) -> None:
        if upc:
            self.db.execute("DELETE FROM choices WHERE key=? AND upc=?", (key, upc))
        else:
            self.db.execute("DELETE FROM choices WHERE key=?", (key,))

    def reject(self, key: str, upc: str, reason: str = "") -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO rejections(key,upc,reason,created_at) VALUES(?,?,?,?)", (key, upc, reason, now()))
            c.execute("DELETE FROM choices WHERE key=? AND upc=?", (key, upc) if key else ("", upc))
            if not key:
                c.execute("DELETE FROM choices WHERE upc=?", (upc,))

    def unreject(self, key: str, upc: str) -> None:
        self.db.execute("DELETE FROM rejections WHERE key=? AND upc=?", (key, upc))

    def set_override(self, upc: str, diet: str, status: str | None) -> None:
        if status:
            self.db.execute("INSERT OR REPLACE INTO diet_overrides(upc,diet,status,created_at) VALUES(?,?,?,?)",
                            (upc, diet, status, now()))
        else:
            self.db.execute("DELETE FROM diet_overrides WHERE upc=? AND diet=?", (upc, diet))

    def set_alias(self, alias: str, key: str) -> None:
        if alias and key and alias != key:
            self.db.execute("INSERT OR REPLACE INTO aliases(alias,key) VALUES(?,?)", (alias, key))

    def remove_alias(self, alias: str) -> None:
        self.db.execute("DELETE FROM aliases WHERE alias=?", (alias,))

    def saved_items(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT c.key, c.upc, c.times_chosen, c.last_chosen_at, c.pinned, c.source, p.description, p.brand, p.size "
            "FROM choices c LEFT JOIN products p USING(upc) ORDER BY c.key, c.pinned DESC, c.times_chosen DESC")]

    def rejected_items(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT r.key, r.upc, r.reason, r.created_at, p.description, p.brand, p.size "
            "FROM rejections r LEFT JOIN products p USING(upc) ORDER BY r.created_at DESC")]

    def override_items(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT o.upc, o.diet, o.status, o.created_at, p.description, p.brand FROM diet_overrides o "
            "LEFT JOIN products p USING(upc) ORDER BY o.created_at DESC")]

    def alias_items(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT alias, key FROM aliases ORDER BY alias")]

    # ------------------------------------------------------------ runs
    def open_run(self) -> dict | None:
        r = self.db.execute(f"SELECT * FROM runs WHERE status IN ({','.join('?' * len(OPEN_STATUSES))}) "
                            "ORDER BY created_at DESC LIMIT 1", OPEN_STATUSES).fetchone()
        return dict(r) if r else None

    def run(self, run_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(r) if r else None

    def runs(self, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT r.*, (SELECT COUNT(*) FROM run_lines l WHERE l.run_id=r.run_id) AS n_lines, "
            "(SELECT COUNT(*) FROM run_lines l WHERE l.run_id=r.run_id AND l.outcome='ok') AS n_ok, "
            "(SELECT COUNT(*) FROM run_lines l WHERE l.run_id=r.run_id AND l.outcome='unknown') AS n_unknown, "
            "(SELECT COUNT(*) FROM run_lines l WHERE l.run_id=r.run_id AND l.outcome='failed') AS n_failed "
            "FROM runs r ORDER BY created_at DESC LIMIT ?", (limit,))]

    def lines(self, run_id: str) -> list[dict]:
        out = []
        for r in self.db.execute("SELECT * FROM run_lines WHERE run_id=? ORDER BY line_no", (run_id,)):
            d = dict(r)
            d["candidates"] = json.loads(d["candidates"] or "[]")
            d["diet"] = json.loads(d["diet"] or "[]")
            out.append(d)
        return out

    def line(self, run_id: str, line_no: int) -> dict | None:
        for l in self.lines(run_id):
            if l["line_no"] == line_no:
                return l
        return None

    def new_run_id(self) -> str:
        day = datetime.now().strftime("%Y-%m-%d")
        n = self.db.execute("SELECT COUNT(*) FROM runs WHERE run_id LIKE ?", (f"r_{day}_%",)).fetchone()[0]
        return f"r_{day}_{n + 1}"

    def transition(self, run_id: str, frm: tuple[str, ...], to: str, **fields) -> bool:
        """Compare-and-set on status. Returns False if the run wasn't in one of `frm`."""
        sets = ", ".join(["status=?", "version=version+1"] + [f"{k}=?" for k in fields])
        cur = self.db.execute(
            f"UPDATE runs SET {sets} WHERE run_id=? AND status IN ({','.join('?' * len(frm))})",
            (to, *fields.values(), run_id, *frm))
        return cur.rowcount == 1

    def update_line(self, run_id: str, line_no: int, **fields) -> None:
        for k in ("candidates", "diet"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k])
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE run_lines SET {sets} WHERE run_id=? AND line_no=?", (*fields.values(), run_id, line_no))
