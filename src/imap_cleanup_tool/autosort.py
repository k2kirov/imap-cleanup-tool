"""One auto-sort pass: learn from moves, trust Sent, classify new INBOX mail, move.

Never deletes and never uses Trash. Design:
docs/superpowers/specs/2026-09-25-autosort-core-loop-design.md
"""

from __future__ import annotations

import imaplib
import os
import re
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.utils import getaddresses

from . import ai_sort, core, scheduler, signals, sortchain, sortstore, triage

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


_HEADERS = ("FROM TO CC DATE SUBJECT MESSAGE-ID LIST-ID LIST-POST LIST-UNSUBSCRIBE "
            "LIST-UNSUBSCRIBE-POST PRECEDENCE AUTO-SUBMITTED X-MAILMAN-VERSION "
            "X-BEENTHERE X-MAILINGLIST X-MAILING-LIST X-MC-USER X-KMAIL-MESSAGE "
            "X-KMAIL-ACCOUNT X-SFMC-STACK X-CSA-COMPLAINTS X-CAMPAIGN X-CAMPAIGNID "
            "X-FEEDBACK-ID X-ME-VSCATEGORY")
FETCH_FIELDS = f"(UID FLAGS INTERNALDATE BODY.PEEK[HEADER.FIELDS ({_HEADERS})])"
# Gmail X-GM-RAW category -> auto-sort category.
_GMAIL_CATEGORIES = {"social": "social", "promotions": "promotions",
                     "updates": "notifications", "forums": "news"}
LOCK_STALE_SECONDS = 3600


class Busy(RuntimeError):
    """Another auto-sort run holds the lock for this account."""


@dataclass
class RunResult:
    run_id: str
    dry_run: bool
    planned: list[dict] = field(default_factory=list)
    moved: int = 0
    skipped: list[str] = field(default_factory=list)
    learned: int = 0
    trusted: int = 0
    ai_note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@contextmanager
def account_lock(account: str):
    """One run per account; a lock older than an hour is treated as stale."""
    path = (scheduler.config_dir() / "locks"
            / f"autosort-{scheduler.account_slug(account)}.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if time.time() - path.stat().st_mtime > LOCK_STALE_SECONDS:
            path.unlink()
    except FileNotFoundError:
        pass
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise Busy("Auto-sort is already running for this account.") from exc
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)


def _parse(meta: bytes, header: bytes, account: str) -> dict | None:
    uid = core._uid_from_meta(meta)
    if not uid:
        return None
    flags_match = re.search(rb"FLAGS \(([^)]*)\)", meta)
    flags = tuple(flags_match.group(1).decode(errors="replace").split()) if flags_match else ()
    internal = imaplib.Internaldate2tuple(meta)
    received = (datetime.fromtimestamp(time.mktime(internal), timezone.utc)
                if internal else None)
    msg = message_from_bytes(header)
    headers = {name: " ".join(core.decode_mime_header(v) for v in msg.get_all(name, []))
               for name in set(msg.keys())}
    sender = core.extract_sender_email(msg.get("From", "")) or "(no sender)"
    return {"uid": uid, "flags": flags, "received": received, "sender": sender,
            "subject": core.decode_mime_header(msg.get("Subject", "")),
            "message_id": (msg.get("Message-ID") or "").strip(),
            "signals": signals.detect(headers, sender, account)}


def fetch_rows(conn, uids: list[int], account: str) -> list[dict]:
    """Fetch flags, INTERNALDATE and sorting headers of the selected folder."""
    rows: list[dict] = []
    for start in range(0, len(uids), core.AI_FETCH_CHUNK):
        chunk = b",".join(str(u).encode() for u in uids[start:start + core.AI_FETCH_CHUNK])
        status, data = conn.uid("FETCH", chunk, FETCH_FIELDS)
        if status != "OK":
            raise ValueError("Cannot fetch inbox headers.")
        pending: list | None = None
        for item in [*(data or []), None]:
            if isinstance(item, bytes) and pending is not None:
                # Some servers send FLAGS or INTERNALDATE after the literal.
                pending[0] += b" " + item
                continue
            if pending is not None and (row := _parse(pending[0], pending[1], account)):
                rows.append(row)
            pending = ([item[0], item[1]] if isinstance(item, tuple) and len(item) >= 2
                       and item[1] else None)
    return rows


def gmail_categories(conn, uids: set[str]) -> dict[str, str]:
    """Gmail's own tabs as a hint, only when the server offers X-GM-EXT-1."""
    if "X-GM-EXT-1" not in triage._capabilities(conn):
        return {}
    found: dict[str, str] = {}
    for gmail_name, category in _GMAIL_CATEGORIES.items():
        try:
            status, data = conn.uid("SEARCH", "X-GM-RAW", f'"category:{gmail_name}"')
        except (imaplib.IMAP4.error, OSError):
            continue
        if status != "OK" or not data or not data[0]:
            continue
        for raw in data[0].split():
            uid = raw.decode()
            if uid in uids:
                found.setdefault(uid, category)
    return found


def run(conn, account: str, *, dry_run: bool = False, backlog: bool = False,
        ai_model: str | None = None, litellm=None,
        now: datetime | None = None) -> RunResult:
    """One pass for one account. Moves only; never deletes."""
    now = now or datetime.now(timezone.utc)
    account = account.strip().lower()
    settings = sortstore.get_settings(account)
    if not settings["started_at"]:
        settings = sortstore.update_settings(
            account, started_at=now.isoformat(timespec="seconds"))
    result = RunResult(run_id=sortstore.new_run_id(), dry_run=dry_run)
    with account_lock(account):
        result.learned = learn(conn, account, now=now)
        result.trusted = trust_sent(conn, account, now=now)
        _sort_inbox(conn, account, result, settings=settings, backlog=backlog,
                    ai_model=ai_model, litellm=litellm)
    return result


def _sort_inbox(conn, account: str, result: RunResult, *, settings: dict,
                backlog: bool, ai_model: str | None, litellm) -> None:
    status, _ = conn.select("INBOX", readonly=result.dry_run)
    if status != "OK":
        raise ValueError("Cannot open INBOX.")
    uidvalidity = core._read_uidvalidity(conn)
    if not uidvalidity:
        raise ValueError("The server did not provide UIDVALIDITY.")
    all_uids = _search_uids(conn, "ALL")
    state = sortstore.get_state(account, "INBOX")
    if state and state["uidvalidity"] != uidvalidity:
        if not result.dry_run:
            sortstore.set_state(account, "INBOX", uidvalidity, max(all_uids or [0]))
        result.skipped.append("INBOX UIDVALIDITY changed; state reset, no moves this run.")
        return
    last = 0 if (backlog or not state) else state["last_uid"]
    rows = fetch_rows(conn, [u for u in all_uids if u > last], account)
    cutoff = datetime.fromisoformat(settings["started_at"])
    moved_ids = sortstore.moved_message_ids(account)
    candidates = [r for r in rows
                  if (backlog or r["received"] is None or r["received"] >= cutoff)
                  and not (r["message_id"] and r["message_id"] in moved_ids)]
    ctx = sortchain.Context(
        account=account, rules=sortstore.rules(account),
        gmail=gmail_categories(conn, {r["uid"] for r in candidates}),
        ai_min_confidence=float(settings["ai_min_confidence"]))
    decisions: dict[str, sortchain.Decision] = {}
    unknown: dict[str, list[dict]] = {}
    for row in candidates:
        decision = sortchain.decide(row, ctx)
        if decision is not None:
            decisions[row["uid"]] = decision
        elif row["sender"] != "(no sender)":
            unknown.setdefault(row["sender"].lower(), []).append(row)
    retry = _ai_layer(account, unknown, decisions, ctx, settings, ai_model,
                      litellm, result)
    _move_all(conn, account, result, candidates, decisions)
    if not result.dry_run and all_uids:
        # Senders beyond the AI budget are read again on the next run.
        next_last = min(retry) - 1 if retry else max(all_uids)
        sortstore.set_state(account, "INBOX", uidvalidity, max(next_last, last))


def _ai_layer(account: str, unknown: dict[str, list[dict]],
              decisions: dict[str, sortchain.Decision], ctx: sortchain.Context,
              settings: dict, ai_model: str | None, litellm,
              result: RunResult) -> list[int]:
    """Ask the AI about unknown senders; return UIDs left over by the budget."""
    if not unknown:
        return []
    name = ai_model if ai_model is not None else settings["ai_model"]
    max_calls = int(settings["ai_max_calls"])
    try:
        cfg = ai_sort.load_model(name)
        verdicts, errors = ai_sort.classify_senders(unknown, cfg, max_calls=max_calls,
                                                    litellm=litellm)
    except ai_sort.Skip as exc:
        result.ai_note = str(exc)
        return []
    result.skipped += [f"AI: {error}" for error in errors]
    for sender, verdict in verdicts.items():
        if verdict["confidence"] >= ctx.ai_min_confidence:
            sortstore.save_rule(account, sender, verdict["category"], "ai",
                                verdict["confidence"])
            decision = sortchain.Decision(verdict["category"], "ai",
                                          verdict["reason"] or "AI verdict")
            for row in unknown[sender]:
                decisions[row["uid"]] = decision
        else:
            sortstore.add_review(account, sender, verdict["category"],
                                 verdict["confidence"], verdict["reason"])
    left = sorted(unknown)[max(0, max_calls):]
    if left:
        result.skipped.append(f"AI budget reached; {len(left)} sender(s) stay in "
                              "INBOX until the next run.")
    return [int(row["uid"]) for sender in left for row in unknown[sender]]


def _move_all(conn, account: str, result: RunResult, candidates: list[dict],
              decisions: dict[str, sortchain.Decision]) -> None:
    delimiter = triage.hierarchy_delimiter(conn)
    known = set(core.list_folders(conn))
    failed: set[str] = set()
    for row in candidates:
        decision = decisions.get(row["uid"], sortchain.INBOX_DEFAULT)
        if decision.category == "inbox":
            continue
        target = triage.folder_name(decision.category, delimiter)
        result.planned.append({
            "uid": row["uid"], "message_id": row["message_id"], "sender": row["sender"],
            "subject": row["subject"], "category": decision.category, "folder": target,
            "layer": decision.layer, "reason": decision.reason})
        if result.dry_run or target in failed:
            continue
        if target not in known:
            try:
                core.create_folder(conn, target)
                known.add(target)
            except (imaplib.IMAP4.error, OSError) as exc:
                failed.add(target)
                result.skipped.append(f"Cannot create {target}: {exc}")
                continue
        triage.move_uid(conn, row["uid"], target)
        sortstore.log_move(account, result.run_id, message_id=row["message_id"],
                           uid=row["uid"], sender=row["sender"], subject=row["subject"],
                           source_folder="INBOX", target_folder=target,
                           layer=decision.layer, reason=decision.reason)
        result.moved += 1
        if not row["message_id"]:
            result.skipped.append(f"UID {row['uid']} has no Message-ID; this move "
                                  "cannot teach or be undone.")
