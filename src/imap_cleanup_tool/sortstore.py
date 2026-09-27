"""Auto-sort state stored beside the manual triage rules (triage_rules.sqlite).

Tables: sender_rule (now with a rule source), move_log, sync_state,
autosort_account (per-account settings) and ai_review (unsure AI answers).
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .rulepacks import CATEGORIES

VALID_CATEGORIES = frozenset((*CATEGORIES, "inbox"))
# A rule may replace a rule of the same or a lower rank only.
_RANK = {"ai": 1, "sent": 2, "learned": 3, "user": 3}
_SETTINGS_DEFAULTS = {"started_at": None, "ai_model": "",
                      "ai_min_confidence": 0.8, "ai_max_calls": 50}
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS sender_rule (account TEXT NOT NULL, "
    "sender TEXT NOT NULL, category TEXT NOT NULL, PRIMARY KEY (account, sender))",
    "CREATE TABLE IF NOT EXISTS move_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "run_id TEXT NOT NULL, account TEXT NOT NULL, message_id TEXT NOT NULL, "
    "uid TEXT NOT NULL, sender TEXT NOT NULL, subject TEXT NOT NULL, "
    "source_folder TEXT NOT NULL, target_folder TEXT NOT NULL, "
    "layer TEXT NOT NULL, reason TEXT NOT NULL, moved_at TEXT NOT NULL, "
    "undone_at TEXT)",
    "CREATE INDEX IF NOT EXISTS move_log_account ON move_log (account, moved_at)",
    "CREATE INDEX IF NOT EXISTS move_log_message ON move_log (account, message_id)",
    "CREATE TABLE IF NOT EXISTS sync_state (account TEXT NOT NULL, "
    "folder TEXT NOT NULL, uidvalidity TEXT NOT NULL, last_uid INTEGER NOT NULL, "
    "updated_at TEXT NOT NULL, PRIMARY KEY (account, folder))",
    "CREATE TABLE IF NOT EXISTS autosort_account (account TEXT PRIMARY KEY, "
    "started_at TEXT, ai_model TEXT NOT NULL DEFAULT '', "
    "ai_min_confidence REAL NOT NULL DEFAULT 0.8, "
    "ai_max_calls INTEGER NOT NULL DEFAULT 50)",
    "CREATE TABLE IF NOT EXISTS ai_review (account TEXT NOT NULL, "
    "sender TEXT NOT NULL, category TEXT NOT NULL, confidence REAL NOT NULL, "
    "reason TEXT NOT NULL, seen_at TEXT NOT NULL, PRIMARY KEY (account, sender))",
)
# Columns added to the pre-auto-sort sender_rule table. Old rows become "user".
_RULE_COLUMNS = {"source": "TEXT NOT NULL DEFAULT 'user'",
                 "confidence": "REAL", "updated_at": "TEXT"}


def db_path() -> Path:
    # Imported lazily: triage imports this module, and tests patch
    # triage.rules_path.
    from . import triage
    return triage.rules_path()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm(value: str) -> str:
    return (value or "").strip().lower()


def connect() -> sqlite3.Connection:
    """Open the store, creating and migrating tables as needed."""
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    with conn:
        for statement in _SCHEMA:
            conn.execute(statement)
        have = {row["name"] for row in conn.execute("PRAGMA table_info(sender_rule)")}
        for column, declaration in _RULE_COLUMNS.items():
            if column not in have:
                conn.execute(f"ALTER TABLE sender_rule ADD COLUMN {column} {declaration}")
        # "other" was the manual tab's old name for the Promotions folder.
        conn.execute("UPDATE sender_rule SET category='promotions' WHERE category='other'")
    return conn


# ----- sender rules --------------------------------------------------------- #
def save_rule(account: str, sender: str, category: str, source: str,
              confidence: float | None = None) -> bool:
    """Save a sender rule unless a higher-rank rule already exists."""
    if category not in VALID_CATEGORIES:
        raise ValueError("Unknown sort category.")
    if source not in _RANK:
        raise ValueError("Unknown rule source.")
    account, sender = _norm(account), _norm(sender)
    if not sender:
        raise ValueError("Empty sender.")
    with closing(connect()) as conn, conn:
        row = conn.execute("SELECT source FROM sender_rule WHERE account=? AND sender=?",
                           (account, sender)).fetchone()
        if row is not None and _RANK.get(row["source"], 3) > _RANK[source]:
            return False
        conn.execute(
            "INSERT INTO sender_rule (account, sender, category, source, confidence,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(account, sender)"
            " DO UPDATE SET category=excluded.category, source=excluded.source,"
            " confidence=excluded.confidence, updated_at=excluded.updated_at",
            (account, sender, category, source, confidence, _now()))
    return True


def rules(account: str, *, with_updated_at: bool = False) -> dict[str, dict]:
    """Sender -> {category, source, confidence}; plus updated_at when asked."""
    with closing(connect()) as conn:
        rows = conn.execute("SELECT sender, category, source, confidence, updated_at "
                            "FROM sender_rule WHERE account=?", (_norm(account),)).fetchall()
    found = {}
    for r in rows:
        rule = {"category": r["category"], "source": r["source"],
                "confidence": r["confidence"]}
        if with_updated_at:
            rule["updated_at"] = r["updated_at"]
        found[r["sender"]] = rule
    return found


def forget_rule(account: str, sender: str) -> None:
    with closing(connect()) as conn, conn:
        conn.execute("DELETE FROM sender_rule WHERE account=? AND sender=?",
                     (_norm(account), _norm(sender)))


# ----- move log ------------------------------------------------------------- #
def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def log_move(account: str, run_id: str, *, message_id: str, uid: str, sender: str,
             subject: str, source_folder: str, target_folder: str, layer: str,
             reason: str) -> int:
    with closing(connect()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO move_log (run_id, account, message_id, uid, sender, subject,"
            " source_folder, target_folder, layer, reason, moved_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, _norm(account), message_id or "", uid, _norm(sender), subject,
             source_folder, target_folder, layer, reason, _now()))
        return int(cur.lastrowid)


def _query(sql: str, params: tuple) -> list[dict]:
    with closing(connect()) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def moves(account: str, limit: int = 100) -> list[dict]:
    return _query("SELECT * FROM move_log WHERE account=? ORDER BY id DESC LIMIT ?",
                  (_norm(account), int(limit)))


def moves_since(account: str, since_iso: str) -> list[dict]:
    return _query("SELECT * FROM move_log WHERE account=? AND moved_at>=? AND "
                  "undone_at IS NULL ORDER BY id", (_norm(account), since_iso))


def get_move(account: str, move_id: int) -> dict | None:
    found = _query("SELECT * FROM move_log WHERE account=? AND id=?",
                   (_norm(account), int(move_id)))
    return found[0] if found else None


def run_moves(account: str, run_id: str) -> list[dict]:
    return _query("SELECT * FROM move_log WHERE account=? AND run_id=? AND "
                  "undone_at IS NULL ORDER BY id", (_norm(account), run_id))


def mark_undone(move_id: int) -> None:
    with closing(connect()) as conn, conn:
        conn.execute("UPDATE move_log SET undone_at=? WHERE id=?", (_now(), int(move_id)))


def moved_message_ids(account: str) -> set[str]:
    return {r["message_id"] for r in _query(
        "SELECT DISTINCT message_id FROM move_log WHERE account=? AND "
        "message_id != ''", (_norm(account),))}


# ----- sync state ----------------------------------------------------------- #
def get_state(account: str, folder: str) -> dict | None:
    found = _query("SELECT uidvalidity, last_uid FROM sync_state WHERE account=? "
                   "AND folder=?", (_norm(account), folder))
    return found[0] if found else None


def set_state(account: str, folder: str, uidvalidity: str, last_uid: int) -> None:
    with closing(connect()) as conn, conn:
        conn.execute(
            "INSERT INTO sync_state (account, folder, uidvalidity, last_uid, updated_at)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT(account, folder) DO UPDATE SET"
            " uidvalidity=excluded.uidvalidity, last_uid=excluded.last_uid,"
            " updated_at=excluded.updated_at",
            (_norm(account), folder, uidvalidity, int(last_uid), _now()))


# ----- settings ------------------------------------------------------------- #
def get_settings(account: str) -> dict:
    found = _query("SELECT started_at, ai_model, ai_min_confidence, ai_max_calls "
                   "FROM autosort_account WHERE account=?", (_norm(account),))
    return found[0] if found else dict(_SETTINGS_DEFAULTS)


def update_settings(account: str, **fields) -> dict:
    unknown = set(fields) - set(_SETTINGS_DEFAULTS)
    if unknown:
        raise ValueError(f"Unknown auto-sort setting: {sorted(unknown)[0]}.")
    if "ai_min_confidence" in fields and not 0.0 < float(fields["ai_min_confidence"]) <= 1.0:
        raise ValueError("AI minimum confidence must be above 0 and at most 1.")
    if "ai_max_calls" in fields and not 0 <= int(fields["ai_max_calls"]) <= 500:
        raise ValueError("AI calls per run must be between 0 and 500.")
    merged = {**get_settings(account), **fields}
    with closing(connect()) as conn, conn:
        conn.execute(
            "INSERT INTO autosort_account (account, started_at, ai_model,"
            " ai_min_confidence, ai_max_calls) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(account) DO UPDATE SET started_at=excluded.started_at,"
            " ai_model=excluded.ai_model, ai_min_confidence=excluded.ai_min_confidence,"
            " ai_max_calls=excluded.ai_max_calls",
            (_norm(account), merged["started_at"], merged["ai_model"] or "",
             float(merged["ai_min_confidence"]), int(merged["ai_max_calls"])))
    return get_settings(account)


# ----- AI review queue ------------------------------------------------------ #
def add_review(account: str, sender: str, category: str, confidence: float,
               reason: str) -> None:
    with closing(connect()) as conn, conn:
        conn.execute(
            "INSERT INTO ai_review (account, sender, category, confidence, reason,"
            " seen_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(account, sender) DO"
            " UPDATE SET category=excluded.category, confidence=excluded.confidence,"
            " reason=excluded.reason, seen_at=excluded.seen_at",
            (_norm(account), _norm(sender), category, float(confidence), reason, _now()))


def reviews(account: str) -> list[dict]:
    return _query("SELECT sender, category, confidence, reason, seen_at FROM "
                  "ai_review WHERE account=? ORDER BY seen_at DESC, sender",
                  (_norm(account),))


def clear_review(account: str, sender: str) -> None:
    with closing(connect()) as conn, conn:
        conn.execute("DELETE FROM ai_review WHERE account=? AND sender=?",
                     (_norm(account), _norm(sender)))
