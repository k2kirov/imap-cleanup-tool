"""One auto-sort pass: learn from moves, trust Sent, classify new INBOX mail, move.

Never deletes and never uses Trash. Design:
docs/superpowers/specs/2026-09-25-autosort-core-loop-design.md
"""

from __future__ import annotations

import imaplib
from datetime import datetime, timedelta
from email import message_from_bytes
from email.utils import getaddresses

from . import core, sortstore, triage

LEARN_DAYS = 30
SENT_FIRST_DAYS = 365
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _imap_date(moment: datetime) -> str:
    # IMAP needs English month names regardless of the process locale.
    return f"{moment.day:02d}-{_MONTHS[moment.month - 1]}-{moment.year}"


def _search_uids(conn, *criteria) -> list[int]:
    status, data = conn.uid("SEARCH", None, *criteria)
    if status != "OK" or not data or not data[0]:
        return []
    return sorted(int(u) for u in data[0].split())


def _fetch_headers(conn, uids: list[int], fields: str) -> list[bytes]:
    """Return raw header blocks for ``uids`` in the selected folder."""
    blocks: list[bytes] = []
    for start in range(0, len(uids), core.UID_CHUNK_SIZE):
        chunk = b",".join(str(u).encode() for u in uids[start:start + core.UID_CHUNK_SIZE])
        status, data = conn.uid("FETCH", chunk, f"(UID BODY.PEEK[HEADER.FIELDS ({fields})])")
        if status != "OK":
            continue
        blocks += [part[1] for part in data or []
                   if isinstance(part, tuple) and len(part) >= 2 and part[1]]
    return blocks


def _recent_message_ids(conn, folder: str, since: datetime) -> set[str]:
    status, _ = conn.select(core._quote_mailbox(folder), readonly=True)
    if status != "OK":
        return set()
    uids = _search_uids(conn, "SINCE", _imap_date(since))
    return {mid for block in _fetch_headers(conn, uids, "MESSAGE-ID")
            if (mid := core._message_id(block))}


def learn(conn, account: str, *, now: datetime) -> int:
    """Turn the user's own moves of auto-sorted mail into sender rules."""
    since = now - timedelta(days=LEARN_DAYS)
    logged = [m for m in sortstore.moves_since(account, since.isoformat(timespec="seconds"))
              if m["message_id"]]
    if not logged:
        return 0
    delimiter = triage.hierarchy_delimiter(conn)
    sort_folders = {triage.folder_name(c, delimiter): c for c in triage.CATEGORY_FOLDERS}
    existing = set(core.list_folders(conn))
    where: dict[str, str] = {}
    for folder in ("INBOX", *sort_folders):
        if folder != "INBOX" and folder not in existing:
            continue
        for message_id in _recent_message_ids(conn, folder, since):
            where.setdefault(message_id, folder)
    current = sortstore.rules(account)
    changed = 0
    for move in logged:
        found = where.get(move["message_id"])
        if found is None or found == move["target_folder"]:
            continue
        category = "inbox" if found == "INBOX" else sort_folders[found]
        rule = current.get(move["sender"])
        if rule and rule["category"] == category:
            continue
        if sortstore.save_rule(account, move["sender"], category, "learned"):
            current[move["sender"]] = {"category": category, "source": "learned",
                                       "confidence": None}
            changed += 1
    return changed


def trust_sent(conn, account: str, *, now: datetime) -> int:
    """Keep mail from people the user writes to in INBOX."""
    folder = core.special_folder(conn, "\\Sent")
    if not folder:
        return 0
    status, _ = conn.select(core._quote_mailbox(folder), readonly=True)
    if status != "OK":
        return 0
    uidvalidity = core._read_uidvalidity(conn)
    key = f"sent:{folder}"
    state = sortstore.get_state(account, key)
    if state and state["uidvalidity"] == uidvalidity:
        uids = [u for u in _search_uids(conn, "UID", f"{state['last_uid'] + 1}:*")
                if u > state["last_uid"]]
        last_uid = max([state["last_uid"], *uids])
    else:
        uids = _search_uids(conn, "SINCE", _imap_date(now - timedelta(days=SENT_FIRST_DAYS)))
        last_uid = max(_search_uids(conn, "ALL") or [0])
    me = account.strip().lower()
    rules = sortstore.rules(account)
    added = 0
    for block in _fetch_headers(conn, uids, "TO CC"):
        msg = message_from_bytes(block)
        for _, address in getaddresses(msg.get_all("To", []) + msg.get_all("Cc", [])):
            address = address.strip().lower()
            if "@" not in address or address == me:
                continue
            if rules.get(address, {}).get("source") in ("user", "learned", "sent"):
                continue
            if sortstore.save_rule(account, address, "inbox", "sent"):
                rules[address] = {"category": "inbox", "source": "sent", "confidence": None}
                added += 1
    sortstore.set_state(account, key, uidvalidity, last_uid)
    return added


def undo_move(conn, account: str, move_id: int) -> str:
    """Move one auto-sorted message back to INBOX and keep its sender there."""
    move = sortstore.get_move(account, move_id)
    if move is None:
        raise ValueError("Unknown move.")
    if move["undone_at"]:
        raise ValueError("This move was already undone.")
    if not move["message_id"]:
        raise ValueError("This message has no Message-ID, so it cannot be found again.")
    status, _ = conn.select(core._quote_mailbox(move["target_folder"]), readonly=False)
    if status != "OK":
        raise ValueError(f"Cannot open {move['target_folder']}.")
    uids = _search_uids(conn, "HEADER", "Message-ID", f'"{move["message_id"]}"')
    if not uids:
        raise ValueError(f"The message is no longer in {move['target_folder']}.")
    triage.move_uid(conn, str(uids[0]), "INBOX")
    sortstore.mark_undone(move["id"])
    sortstore.save_rule(account, move["sender"], "inbox", "user")
    return "INBOX"


def undo_run(conn, account: str, run_id: str) -> dict:
    undone, errors = 0, []
    for move in sortstore.run_moves(account, run_id):
        try:
            undo_move(conn, account, move["id"])
            undone += 1
        except (ValueError, imaplib.IMAP4.error, OSError) as exc:
            errors.append(f"{move['subject']}: {exc}")
    return {"undone": undone, "errors": errors}
