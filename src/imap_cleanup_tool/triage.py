"""Header-only inbox sorting with explicit, per-account sender training."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from email import message_from_bytes
from pathlib import Path

from . import core
from .scheduler import config_dir

DESTINATIONS = {"social": "INBOX.Social", "other": "INBOX.Other"}
_SOCIAL_SUBJECT = re.compile(
    r"\b(?:people viewed your profile|profile views?|connection request|"
    r"new follower|started following you|mentioned you|reacted to your post)\b",
    re.IGNORECASE)
_SOCIAL_DOMAINS = ("linkedin.com", "facebookmail.com", "instagram.com")
_OTHER_SUBJECT = re.compile(
    r"\b(?:newsletter|special offer|promotional courses?|course schedule|"
    r"spaces for our .* workshop|webinar|weekly digest)\b", re.IGNORECASE)
_EXTRA_PROTECTED_SUBJECT = re.compile(
    r"\b(?:medical|doctor|prescription|test results?|insurance|tax|bank(?:ing)?|"
    r"flight|boarding pass|hotel|reservation|support (?:ticket|case)|"
    r"incident|outage)\b", re.IGNORECASE)
_FETCH_FIELDS = "(UID BODY.PEEK[HEADER.FIELDS (FROM DATE SUBJECT LIST-UNSUBSCRIBE PRECEDENCE)])"


def rules_path() -> Path:
    return config_dir() / "triage_rules.sqlite"


def _rules_db() -> sqlite3.Connection:
    conn = sqlite3.connect(rules_path())
    conn.execute("CREATE TABLE IF NOT EXISTS sender_rule ("
                 "account TEXT NOT NULL, sender TEXT NOT NULL, category TEXT NOT NULL,"
                 "PRIMARY KEY (account, sender))")
    return conn


def sender_rules(account: str) -> dict[str, str]:
    """Return the user's explicit sender choices for one mailbox."""
    with closing(_rules_db()) as conn:
        return dict(conn.execute(
            "SELECT sender, category FROM sender_rule WHERE account=?",
            (account.strip().lower(),)).fetchall())


def train_sender(account: str, sender: str, category: str) -> None:
    """Remember an explicit choice for future previews; Inbox is a keep rule."""
    if category not in {*DESTINATIONS, "inbox"}:
        raise ValueError("Choose Inbox, Social, or Other.")
    with closing(_rules_db()) as conn:
        with conn:
            conn.execute("INSERT INTO sender_rule (account, sender, category) "
                         "VALUES (?, ?, ?) ON CONFLICT(account, sender) DO UPDATE "
                         "SET category=excluded.category",
                         (account.strip().lower(), sender.strip().lower(), category))


def forget_sender(account: str, sender: str) -> None:
    with closing(_rules_db()) as conn:
        with conn:
            conn.execute("DELETE FROM sender_rule WHERE account=? AND sender=?",
                         (account.strip().lower(), sender.strip().lower()))


def classify(row: dict, rules: dict[str, str] | None = None) -> tuple[str, str]:
    """Return (inbox/social/other, short reason). Protected topics win."""
    sender = row["sender"].lower()
    subject = row["subject"]
    if (core._PROTECTED_SUBJECT_HINT.search(subject)
            or _EXTRA_PROTECTED_SUBJECT.search(subject)
            or re.match(r"\s*(?:invitation|accepted|declined):", subject,
                        re.IGNORECASE)):
        return "inbox", "Protected topic"
    learned = (rules or {}).get(sender)
    if learned in {*DESTINATIONS, "inbox"}:
        return learned, "Your sender rule"
    domain = sender.rsplit("@", 1)[-1]
    if domain in _SOCIAL_DOMAINS and _SOCIAL_SUBJECT.search(subject):
        return "social", "Social activity update"
    if core._old_service_alert(subject, row["date"]):
        return "other", "Old routine service alert"
    if _OTHER_SUBJECT.search(subject):
        return "other", "Newsletter or promotion"
    return "inbox", "No safe sorting rule"


def _parse_part(part) -> dict | None:
    if not (isinstance(part, tuple) and len(part) >= 2 and part[1]):
        return None
    uid = core._uid_from_meta(part[0])
    if not uid:
        return None
    msg = message_from_bytes(part[1])
    return {"uid": uid,
            "sender": core.extract_sender_email(msg.get("From", "")) or "(no sender)",
            "subject": core.decode_mime_header(msg.get("Subject", "")),
            "date": msg.get("Date", ""),
            "list_unsubscribe": bool(msg.get("List-Unsubscribe")),
            "bulk": "bulk" in msg.get("Precedence", "").lower()}


def preview(conn, account: str, *, folder: str = "INBOX") -> dict:
    """Read headers without changing Seen flags or moving mail."""
    status, _ = conn.select(core._quote_mailbox(folder), readonly=True)
    if status != "OK":
        raise ValueError(f"Cannot open {folder!r}.")
    uidvalidity = core._read_uidvalidity(conn)
    if not uidvalidity:
        raise ValueError("The server did not provide UIDVALIDITY.")
    status, data = conn.uid("SEARCH", None, "ALL")
    if status != "OK":
        raise ValueError("Cannot search the inbox.")
    uids = data[0].split() if data and data[0] else []
    rules = sender_rules(account)
    rows = []
    for start in range(0, len(uids), core.AI_FETCH_CHUNK):
        status, data = conn.uid("FETCH", b",".join(uids[start:start + core.AI_FETCH_CHUNK]),
                                _FETCH_FIELDS)
        if status != "OK":
            raise ValueError("Cannot fetch inbox headers.")
        for part in data or []:
            row = _parse_part(part)
            if row:
                row["category"], row["reason"] = classify(row, rules)
                rows.append(row)
    return {"folder": folder, "uidvalidity": uidvalidity, "rows": rows}


def move_checked(conn, *, uid: str, uidvalidity: str, sender: str,
                 subject: str, date: str, category: str) -> str:
    """Move one still-matching message to a review folder. Never use Trash."""
    if category not in DESTINATIONS:
        raise ValueError("Choose Social or Other.")
    if not uid.isascii() or not uid.isdecimal() or int(uid) <= 0:
        raise ValueError("Invalid message UID.")
    capabilities = {c.decode().upper() if isinstance(c, bytes) else c.upper()
                    for c in getattr(conn, "capabilities", ())}
    if "MOVE" not in capabilities and "UIDPLUS" not in capabilities:
        raise ValueError("The server needs MOVE or UIDPLUS for a safe move.")
    status, _ = conn.select("INBOX", readonly=False)
    if status != "OK" or core._read_uidvalidity(conn) != uidvalidity:
        raise ValueError("Inbox identity changed. Scan again.")
    status, data = conn.uid("FETCH", uid.encode(), _FETCH_FIELDS)
    row = next((r for part in data or [] if (r := _parse_part(part))), None) \
        if status == "OK" else None
    if (row is None or row["uid"] != uid or row["sender"].lower() != sender.lower()
            or row["subject"] != subject or row["date"] != date):
        raise ValueError("Message changed or left the inbox. Scan again.")
    destination = DESTINATIONS[category]
    if destination not in core.list_folders(conn):
        core.create_folder(conn, destination)
    if "MOVE" in capabilities:
        status, _ = conn.uid("MOVE", uid.encode(), core._quote_mailbox(destination))
        if status != "OK":
            raise ValueError("The server did not move the message.")
    else:
        status, _ = conn.uid("COPY", uid.encode(), core._quote_mailbox(destination))
        if status != "OK":
            raise ValueError("The server did not copy the message.")
        status, _ = conn.uid("STORE", uid.encode(), "+FLAGS", r"(\Deleted)")
        if status != "OK":
            raise ValueError("The copy succeeded, but the source remains in INBOX.")
        status, _ = conn.uid("EXPUNGE", uid.encode())
        if status != "OK":
            raise ValueError("The copy succeeded, but the source remains in INBOX.")
    return destination
