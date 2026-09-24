"""Header-only inbox sorting and review hints with per-account sender training."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from email import message_from_bytes
from functools import lru_cache
from pathlib import Path

from . import core
from .scheduler import config_dir


@dataclass(frozen=True)
class MatchRule:
    """A SpamSieve-style Location, Match Style, and Text to Match clause.

    These clauses select mail for this app. They do not create SpamSieve
    allowlist or blocklist entries, which only decide good versus spam.
    """

    match_field: str
    match_style: str
    text_to_match: str

    def matches(self, row: dict) -> bool:
        if self.match_field == "From (address)":
            value = row["sender"]
        elif self.match_field == "Subject":
            value = row["subject"]
        else:
            raise ValueError("Unsupported SpamSieve rule location.")
        if self.match_style == "Matches Regex":
            return bool(_compiled_rule(self.text_to_match).search(value))
        value, wanted = value.casefold(), self.text_to_match.casefold()
        if self.match_style == "Is Equal to":
            return value == wanted
        if self.match_style == "Contains":
            return wanted in value
        if self.match_style == "Starts with":
            return value.startswith(wanted)
        if self.match_style == "Ends with":
            return value.endswith(wanted)
        raise ValueError("Unsupported SpamSieve match style.")


@lru_cache(maxsize=128)
def _compiled_rule(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


DESTINATIONS = {"social": "INBOX.Social", "other": "INBOX.Other"}
_SOCIAL_SUBJECT_PATTERN = (
    r"\b(?:people viewed your profile|profile views?|connection request|"
    r"new follower|started following you|mentioned you|reacted to your post|"
    r"(?:liked|commented on|replied to|shared) your (?:post|photo|video|comment))\b")
# Social Media domains from https://github.com/inpector/sieve-filters (CC0).
# LinkedIn and facebookmail.com preserve the existing, tested notification rules.
_SOCIAL_DOMAINS = frozenset({
    "facebook.com", "facebookmail.com", "flickr.com", "instagram.com",
    "linkedin.com", "pinterest.com", "reddit.com", "tiktok.com",
    "tumblr.com", "twitter.com", "x.com",
})
_SOCIAL_FROM_RULES = tuple(
    MatchRule("From (address)", "Matches Regex",
              rf"@(?:[^@.]+\.)*{re.escape(domain)}$")
    for domain in sorted(_SOCIAL_DOMAINS)
)
_SOCIAL_SUBJECT_RULE = MatchRule("Subject", "Matches Regex", _SOCIAL_SUBJECT_PATTERN)
_OTHER_SUBJECT = re.compile(
    r"\b(?:newsletter|special offer|promotional courses?|course schedule|"
    r"spaces for our .* workshop|webinar|weekly digest)\b", re.IGNORECASE)
_EXTRA_PROTECTED_SUBJECT = re.compile(
    r"\b(?:medical|doctor|prescription|test results?|insurance|tax|bank(?:ing)?|"
    r"flight|boarding pass|hotel|reservation|support (?:ticket|case)|"
    r"incident|outage)\b", re.IGNORECASE)
_FETCH_FIELDS = ("(UID BODY.PEEK[HEADER.FIELDS (FROM TO CC DATE SUBJECT "
                 "LIST-ID LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST PRECEDENCE "
                 "X-BEENTHERE X-MAILINGLIST X-MAILING-LIST CONTENT-TYPE "
                 "CONTENT-DISPOSITION X-SPAM-FLAG X-SPAM-LEVEL "
                 "X-MBO-SPAM-PROBABILITY X-SPAM-SCORE AUTHENTICATION-RESULTS)])")

_SPAM_TLDS = frozenset("ac bond cc cfd click cm cyou date finance gd help icu "
                       "info li live men online pro ru shop site st support sx "
                       "top vg win work world ws wtf xin xyz".split())
_DISPOSABLE_DOMAINS = frozenset("yopmail.com mailinator.com guerrillamail.com "
    "10minutemail.com 10minutemail.net tempmail.com tempmailo.com tempmail.dev "
    "getnada.com trashmail.com".split())
_FREE_MAIL_DOMAINS = frozenset("gmail.com yahoo.com yahoo.co.uk outlook.com "
    "hotmail.com live.com mail.ru yandex.ru yandex.com proton.me "
    "protonmail.com zoho.com icloud.com".split())
_TYPO_DOMAINS = re.compile(
    r"(?:paypal-secure\.|paypa[il][^.]*\.|amaz0n-|amazon-order\.|"
    r"microsoftsecuremail\.|micros0ft-|appleid-verify\.|icloud-alert\."
    r"|dhl-delivery\.|fedextrack-|sparkasse-|\-verifikation\."
    r"|commerzbank-security\.|postbank-verifikation\.)", re.IGNORECASE)
_SUSPICIOUS_LOCAL = re.compile(
    r"^(?:(?:noreply|no[-_.]?reply|info|account|support|admin)"
    r"(?:[-_.]?[0-9]{3,}|[-_.]?[A-Za-z0-9]{6,})|"
    r"[A-Za-z0-9]{15,}|(?:service|security|update|verify)"
    r"(?:[-_.]?(?:notice|alert|center))?[0-9]*)$", re.IGNORECASE)
_MALICIOUS_HEADER = ("application/x-msdownload", "application/x-msdos-program",
    "application/x-dosexec", "application/x-msi", "application/x-sh",
    "application/hta", "application/x-vbs", "application/javascript")
_MALICIOUS_EXTENSION = re.compile(
    r"\.(?:exe|msi|com|scr|pif|cpl|hta|bat|cmd|vbs|vbe|ps1|js|jse|"
    r"wsf|wsh|sh|jar)(?:\b|\"|')", re.IGNORECASE)
_SHIPPING_DOMAINS = frozenset({
    "dhl.de", "dhl.com", "deutschepost.de", "dpd.de", "dpd.com",
    "myhermes.de", "hermes-europe.de", "ups.com", "gls-group.eu",
    "gls-germany.com", "fedex.com", "tnt.com", "post.at",
    "swisspost.ch", "amazon.de", "amazon.com",
})

# These source clauses need the SMTP envelope, account-specific values, or MIME
# parts. A header-only IMAP scan cannot report a reliable per-message match.
_UNAVAILABLE_CHECKS = (
    {"name": "Filter abused standard addresses", "reason": "Needs SMTP recipient and your domains"},
    {"name": "RFC-Mandated-Addresses", "reason": "Needs SMTP recipient and your domains"},
    {"name": "Block Dead addresses", "reason": "Needs SMTP recipient and your retired addresses"},
    {"name": "Deliveries recipient branch", "reason": "Needs SMTP recipient and your domains"},
    {"name": "Free Time recipient branch", "reason": "Needs SMTP recipient and your domains"},
    {"name": "PDF Bills", "reason": "Needs message body and MIME parts"},
    {"name": "Last Rule CatchAll", "reason": "Needs your address list; source rule has incomplete quotes"},
)


@lru_cache(maxsize=1)
def _inpector_groups() -> tuple[dict, ...]:
    """Pinned CC0, header-only rules from inpector/sieve-filters."""
    path = Path(__file__).with_name("inpector_rules.json")
    return tuple(json.loads(path.read_text(encoding="utf-8"))["groups"])


def _domain_matches(domain: str, known: str) -> bool:
    return domain == known or domain.endswith("." + known)


def source_matches(row: dict) -> list[str]:
    """Return source rule names only; the source's actions are never run."""
    sender = row["sender"].lower()
    domain = sender.rsplit("@", 1)[-1] if "@" in sender else ""
    subject = row["subject"].casefold()
    matches = []
    for group in _inpector_groups():
        name = group["name"]
        if name == "Mailinglists":
            matched = (row.get("list_unsubscribe", False) or row.get("bulk", False)
                       or row.get("list_id", False) or row.get("mailing_list", False))
        else:
            matched = (any(_domain_matches(domain, known) for known in group["domains"])
                       or any(term in subject for term in group["subject_contains"]))
            if name == "Deliveries":
                matched = matched or sender in {
                    "shipment-tracking@amazon.de", "shipment-tracking@amazon.com",
                    "order-update@amazon.de", "auto-confirm@amazon.de"}
            if name == "Free Time":
                matched = matched or any(term in row.get("review_headers", {}).get(
                    "from", "").casefold() for term in
                    ("prime gaming", "lieferando", "mastodon", ".social"))
            if name == "Cronjobs & Monitoring":
                matched = matched or "cron" in row.get("review_headers", {}).get(
                    "from", "").casefold()
        if matched:
            matches.append(name)
    return matches


def review_hints(row: dict) -> list[str]:
    """Evaluate source checks as independent hints, never as Sieve actions."""
    sender = row["sender"].casefold()
    local, _, domain = sender.rpartition("@")
    subject = row["subject"].casefold()
    headers = row.get("review_headers", {})
    auth = headers.get("authentication-results", "").casefold()
    content = (headers.get("content-type", "") + " "
               + headers.get("content-disposition", "")).casefold()
    hits = []

    def add(condition: bool, name: str) -> None:
        if condition:
            hits.append(name)

    add(any(t in content for t in _MALICIOUS_HEADER)
        or bool(_MALICIOUS_EXTENSION.search(content)),
        "Filter malicous attachments (top-level header only)")
    add(domain.rsplit(".", 1)[-1] in _SPAM_TLDS, "Block common spam tlds")
    try:
        spam_score = float(headers.get("x-spam-score", ""))
    except ValueError:
        spam_score = 0
    add("yes" in headers.get("x-spam-flag", "").casefold()
        or "*****" in headers.get("x-spam-level", "")
        or "*****" in headers.get("x-mbo-spam-probability", "")
        or spam_score >= 5, "Common Spam: spam headers")
    add(any(f"dmarc={v}" in auth for v in ("fail", "none", "permerror"))
        and "dmarc=pass" not in auth, "Common Spam: DMARC alignment failure")
    add("spf=fail" in auth and "dkim=pass" not in auth,
        "Common Spam: SPF-only failure")
    add("dkim=fail" in auth and "spf=pass" not in auth,
        "Common Spam: DKIM-only failure")
    add(domain in _DISPOSABLE_DOMAINS,
        "Common Spam: Disposable/temporary email providers")
    add(bool(_TYPO_DOMAINS.search(domain)),
        "Common Spam: Typosquatted brand domains")
    add(any(term in subject for term in ("verify your account", "update your payment",
        "unusual login", "crypto", "investment opportunity")),
        "Common Spam: Phishing subject lines")
    add(any(term in headers.get("from", "").casefold() for term in
            ("customer support", "security team", "account services")),
        "Common Spam: Impersonated support addresses")
    dhl_subject = "dhl" in subject
    shipping_subject = any(term in subject for term in
        ("sendung", "paket", "sendungsbenachrichtigung", "geliefert",
         "versand", "dpd"))
    add((dhl_subject and not any(_domain_matches(domain, known) for known in
                                 ("dhl.de", "dhl.com", "deutschepost.de")))
        or (shipping_subject and not any(_domain_matches(domain, known)
                                         for known in _SHIPPING_DOMAINS)),
        "Common Spam: False Shipping notifications")
    add(domain in _FREE_MAIL_DOMAINS and bool(_SUSPICIOUS_LOCAL.fullmatch(local)),
        "Common Spam: Suspicious sender patterns")
    add(any(symbol in subject for symbol in "$£€"),
        "Common Spam: Currency symbols")
    add(bool(re.search(r"invoice\s?#?[0-9]{6,}", subject)),
        "Common Spam: Invoice number pattern")
    return hits


def source_coverage() -> dict:
    """Expose source coverage and limits beside the review results."""
    return {"source": "https://github.com/inpector/sieve-filters",
            "commit": "fe5a0ce637504c6baf70514f13f369f09d234de0",
            "sorting_groups": [group["name"] for group in _inpector_groups()],
            "review_checks": [
                "Filter malicous attachments (top-level header only)",
                "Block common spam tlds", "Common Spam: spam headers",
                "Common Spam: DMARC alignment failure",
                "Common Spam: SPF-only failure", "Common Spam: DKIM-only failure",
                "Common Spam: Disposable/temporary email providers",
                "Common Spam: Typosquatted brand domains",
                "Common Spam: Phishing subject lines",
                "Common Spam: Impersonated support addresses",
                "Common Spam: False Shipping notifications",
                "Common Spam: Suspicious sender patterns",
                "Common Spam: Currency symbols",
                "Common Spam: Invoice number pattern",
            ],
            "not_evaluated": list(_UNAVAILABLE_CHECKS),
            "note": "Checks use inbox headers only. Source actions never run. "
                    "Header claims are hints, not proof of spam or authenticity."}


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


def sender_rule_definitions(account: str) -> list[dict[str, str]]:
    """Expose learned choices with SpamSieve's three match columns."""
    return [
        {"match_field": "From (address)", "match_style": "Is Equal to",
         "text_to_match": sender, "category": category}
        for sender, category in sorted(sender_rules(account).items())
    ]


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
    matched = row.get("source_matches")
    if matched is None:
        matched = source_matches(row)
    if (core._PROTECTED_SUBJECT_HINT.search(subject)
            or _EXTRA_PROTECTED_SUBJECT.search(subject)
            or any(name in matched for name in (
                "Security", "Deliveries", "Finances", "Fix Costs", "Travelling"))
            or re.match(r"\s*(?:invitation|accepted|declined):", subject,
                        re.IGNORECASE)):
        return "inbox", "Protected subject or source rule"
    learned = (rules or {}).get(sender)
    if learned in {*DESTINATIONS, "inbox"}:
        return learned, "From (address) is equal to saved sender"
    if (any(rule.matches(row) for rule in _SOCIAL_FROM_RULES)
            and _SOCIAL_SUBJECT_RULE.matches(row)):
        return "social", "From (address) matches social domain; Subject matches activity"
    if core._old_service_alert(subject, row["date"]):
        return "other", "Subject matches service alert; Date is over 180 days old"
    if _OTHER_SUBJECT.search(subject):
        return "other", "Subject matches promotion or newsletter"
    domain = sender.rsplit("@", 1)[-1] if "@" in sender else ""
    if "Free Time" in matched and not any(
            _domain_matches(domain, social) for social in _SOCIAL_DOMAINS):
        return "other", "inpector Free Time rule"
    if "Mailinglists" in matched:
        return "other", "inpector Mailinglists rule"
    return "inbox", "No rule matched"


def _parse_part(part) -> dict | None:
    if not (isinstance(part, tuple) and len(part) >= 2 and part[1]):
        return None
    uid = core._uid_from_meta(part[0])
    if not uid:
        return None
    msg = message_from_bytes(part[1])
    recipients = " ".join(msg.get_all("To", []) + msg.get_all("Cc", [])).lower()
    precedence = msg.get("Precedence", "").lower()
    review_headers = {name: " ".join(msg.get_all(name, [])) for name in (
        "From", "Content-Type", "Content-Disposition", "X-Spam-Flag",
        "X-Spam-Level", "X-MBO-SPAM-Probability", "X-Spam-Score",
        "Authentication-Results")}
    review_headers = {name.casefold(): value for name, value in review_headers.items()}
    return {"uid": uid,
            "sender": core.extract_sender_email(msg.get("From", "")) or "(no sender)",
            "subject": core.decode_mime_header(msg.get("Subject", "")),
            "date": msg.get("Date", ""),
            "list_id": bool(msg.get("List-ID")),
            "list_unsubscribe": bool(msg.get("List-Unsubscribe")),
            "bulk": any(word in precedence for word in ("bulk", "list", "junk")),
            "review_headers": review_headers,
            "mailing_list": (bool(msg.get("List-Unsubscribe-Post"))
                             or bool(msg.get("X-BeenThere"))
                             or bool(msg.get("X-Mailinglist"))
                             or bool(msg.get("X-Mailing-List"))
                             or "@lists." in recipients or "newsletter@" in recipients)}


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
                row["source_matches"] = source_matches(row)
                row["review_hints"] = review_hints(row)
                row.pop("review_headers", None)
                row["category"], row["reason"] = classify(row, rules)
                rows.append(row)
    return {"folder": folder, "uidvalidity": uidvalidity, "rows": rows,
            "source_coverage": source_coverage()}


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
