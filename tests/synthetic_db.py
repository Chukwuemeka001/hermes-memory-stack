#!/usr/bin/env python3
"""Synthetic Hermes-like state.db builder for testing state_db_remediate.py.

Builds real SQLite databases that faithfully mirror the Hermes session schema
(sessions + messages + the two own-content FTS5 indexes + their AFTER triggers),
so remediation behaviour can be verified WITHOUT ever touching live data.

The DDL here is copied from a live ``state.db`` (schema_version 16). A reduced
``schema='old'`` variant (no ``end_reason`` column, no trigram index) exercises
the schema-variance code paths.

Can also be run as a CLI to drop a messy fixture DB somewhere for manual poking:
    python3 tests/synthetic_db.py /tmp/messy/state.db --messy
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import time

SECONDS_PER_DAY = 86400.0

SESSIONS_DDL_V16 = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    user_id TEXT,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    title TEXT,
    handoff_state TEXT,
    handoff_platform TEXT,
    handoff_error TEXT,
    rewind_count INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
)
"""

# Older schema: no end_reason, no parent self-FK, no archived/handoff columns.
SESSIONS_DDL_OLD = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    system_prompt TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    message_count INTEGER DEFAULT 0,
    title TEXT
)
"""

MESSAGES_DDL = """
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    active INTEGER NOT NULL DEFAULT 1,
    compacted INTEGER NOT NULL DEFAULT 0
)
"""

FTS_DDL = "CREATE VIRTUAL TABLE messages_fts USING fts5(content)"
TRIGRAM_DDL = "CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(content, tokenize='trigram')"

FTS_TRIGGERS = [
    """CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
        INSERT INTO messages_fts(rowid, content) VALUES (
            new.id,
            COALESCE(new.content,'')||' '||COALESCE(new.tool_name,'')||' '||COALESCE(new.tool_calls,''));
    END""",
    """CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN
        DELETE FROM messages_fts WHERE rowid = old.id;
    END""",
    """CREATE TRIGGER messages_fts_update AFTER UPDATE ON messages BEGIN
        DELETE FROM messages_fts WHERE rowid = old.id;
        INSERT INTO messages_fts(rowid, content) VALUES (
            new.id,
            COALESCE(new.content,'')||' '||COALESCE(new.tool_name,'')||' '||COALESCE(new.tool_calls,''));
    END""",
]

TRIGRAM_TRIGGERS = [
    """CREATE TRIGGER messages_fts_trigram_insert AFTER INSERT ON messages BEGIN
        INSERT INTO messages_fts_trigram(rowid, content) VALUES (
            new.id,
            COALESCE(new.content,'')||' '||COALESCE(new.tool_name,'')||' '||COALESCE(new.tool_calls,''));
    END""",
    """CREATE TRIGGER messages_fts_trigram_delete AFTER DELETE ON messages BEGIN
        DELETE FROM messages_fts_trigram WHERE rowid = old.id;
    END""",
    """CREATE TRIGGER messages_fts_trigram_update AFTER UPDATE ON messages BEGIN
        DELETE FROM messages_fts_trigram WHERE rowid = old.id;
        INSERT INTO messages_fts_trigram(rowid, content) VALUES (
            new.id,
            COALESCE(new.content,'')||' '||COALESCE(new.tool_name,'')||' '||COALESCE(new.tool_calls,''));
    END""",
]


class SyntheticDB:
    """Builder for a synthetic Hermes session database."""

    def __init__(self, path: str, schema: str = "v16", with_trigram: bool = True,
                 now: float | None = None):
        self.path = path
        self.schema = schema
        self.with_trigram = with_trigram
        self.now = now if now is not None else time.time()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        if os.path.exists(path):
            os.remove(path)
        self.con = sqlite3.connect(path)
        self._init_schema()

    def _init_schema(self) -> None:
        c = self.con
        if self.schema == "old":
            c.execute(SESSIONS_DDL_OLD)
        else:
            c.execute(SESSIONS_DDL_V16)
        c.execute(MESSAGES_DDL)
        c.execute("CREATE INDEX idx_messages_session ON messages(session_id)")
        c.execute(FTS_DDL)
        for trg in FTS_TRIGGERS:
            c.execute(trg)
        if self.with_trigram:
            c.execute(TRIGRAM_DDL)
            for trg in TRIGRAM_TRIGGERS:
                c.execute(trg)
        # Hermes records its schema version in a table, not the pragma.
        c.execute("CREATE TABLE schema_version (version INTEGER)")
        ver = 11 if self.schema == "old" else 16
        c.execute("INSERT INTO schema_version VALUES (?)", (ver,))
        c.execute("CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT)")
        c.commit()

    def add_session(self, sid: str, *, source: str = "cli",
                    days_ago: float = 1.0, ended: bool | float = False,
                    end_reason: str | None = None, parent_id: str | None = None,
                    n_messages: int = 3, text: str | None = None,
                    last_message_days_ago: float | None = None) -> str:
        """Insert a session and ``n_messages`` messages.

        ``ended``: False -> open; True -> ended at started+1h; a float -> ended
        that many days ago. ``days_ago`` is when it started.
        ``last_message_days_ago``: if set, the final message's timestamp is
        placed this many days ago (to model a long-lived session still active).
        """
        started = self.now - days_ago * SECONDS_PER_DAY
        ended_at = None
        if ended is True:
            ended_at = started + 3600
        elif isinstance(ended, (int, float)) and ended is not False:
            ended_at = self.now - float(ended) * SECONDS_PER_DAY

        if self.schema == "old":
            self.con.execute(
                "INSERT INTO sessions (id, source, system_prompt, started_at, "
                "ended_at, message_count, title) VALUES (?,?,?,?,?,?,?)",
                (sid, source, "sys-prompt " * 50, started, ended_at, n_messages,
                 f"session {sid}"))
        else:
            self.con.execute(
                "INSERT INTO sessions (id, source, system_prompt, parent_session_id, "
                "started_at, ended_at, end_reason, message_count) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (sid, source, "sys-prompt " * 50, parent_id, started, ended_at,
                 end_reason, n_messages))

        base = text or (f"message body for {sid} with searchable token "
                        f"remediation{sid.replace('-', '')} content")
        for i in range(n_messages):
            role = ("user", "assistant", "tool")[i % 3]
            tool_name = "session_search" if role == "tool" else None
            tool_calls = ('{"name":"write_file","args":"x"}' * 5) if role == "assistant" else None
            ts = started + i * 10
            if last_message_days_ago is not None and i == n_messages - 1:
                ts = self.now - last_message_days_ago * SECONDS_PER_DAY
            self.con.execute(
                "INSERT INTO messages (session_id, role, content, tool_calls, "
                "tool_name, timestamp, active) VALUES (?,?,?,?,?,?,1)",
                (sid, role, f"{base} #{i}", tool_calls, tool_name, ts))
        self.con.commit()
        return sid

    def set_meta(self, key: str, value: str) -> None:
        self.con.execute("INSERT OR REPLACE INTO state_meta VALUES (?,?)", (key, value))
        self.con.commit()

    def close(self) -> None:
        self.con.commit()
        self.con.close()


def build_clean_db(path: str, now: float | None = None) -> str:
    """A small, tidy DB: a few recent closed sessions, nothing to clean."""
    db = SyntheticDB(path, now=now)
    db.add_session("clean-1", days_ago=1, ended=0.5, n_messages=4)
    db.add_session("clean-2", days_ago=2, ended=1.5, n_messages=3)
    db.add_session("clean-3", days_ago=0.2, ended=False, n_messages=2)  # recent open
    db.close()
    return path


def build_messy_db(path: str, now: float | None = None) -> str:
    """A representative messy DB covering every remediation scenario."""
    db = SyntheticDB(path, now=now)
    # Recent, active — must NEVER be pruned.
    db.add_session("recent-open", source="telegram", days_ago=0.1, ended=False,
                   n_messages=5)
    db.add_session("recent-closed", source="cli", days_ago=1, ended=0.5, n_messages=4)
    # Old CLOSED sessions — eligible for prune_closed.
    db.add_session("old-closed-1", source="cli", days_ago=200, ended=199, n_messages=6)
    db.add_session("old-closed-2", source="cron", days_ago=150, ended=149, n_messages=3)
    # Old UNCLOSED sessions — only eligible when prune_unclosed=yes.
    db.add_session("old-open-1", source="cli", days_ago=300, ended=False, n_messages=8)
    db.add_session("old-open-2", source="telegram", days_ago=120, ended=False, n_messages=4)
    # Compression parent (holds original transcript) + its summarized child.
    db.add_session("comp-parent", source="cli", days_ago=40, ended=39,
                   end_reason="compression", n_messages=20)
    db.add_session("comp-child", source="cli", days_ago=39, ended=False,
                   parent_id="comp-parent", n_messages=2)
    # Compression parent WITHOUT a child — must NOT be auto-deleted (no summary).
    db.add_session("comp-orphan", source="cli", days_ago=41, ended=40,
                   end_reason="compression", n_messages=10)
    db.close()
    return path


def build_chain_db(path: str, now: float | None = None) -> str:
    """A multi-level compression chain: grandparent -> mid -> leaf (all linked)."""
    db = SyntheticDB(path, now=now)
    db.add_session("chain-gp", source="cli", days_ago=60, ended=59,
                   end_reason="compression", n_messages=12)
    db.add_session("chain-mid", source="cli", days_ago=50, ended=49,
                   end_reason="compression", parent_id="chain-gp", n_messages=8)
    db.add_session("chain-leaf", source="cli", days_ago=49, ended=False,
                   parent_id="chain-mid", n_messages=3)
    db.close()
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate a synthetic Hermes state.db")
    ap.add_argument("path")
    ap.add_argument("--messy", action="store_true", help="rich multi-scenario DB")
    ap.add_argument("--old-schema", action="store_true", help="reduced legacy schema")
    args = ap.parse_args()
    if args.old_schema:
        db = SyntheticDB(args.path, schema="old", with_trigram=False)
        db.add_session("a", days_ago=1, ended=0.5)
        db.add_session("b", days_ago=200, ended=False)
        db.close()
    elif args.messy:
        build_messy_db(args.path)
    else:
        build_clean_db(args.path)
    print(f"wrote {args.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
