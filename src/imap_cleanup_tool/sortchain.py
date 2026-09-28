"""Auto-sort layer chain: the first matching layer decides. Pure, no IMAP."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import rulepacks, signals


@dataclass(frozen=True)
class Decision:
    category: str   # "inbox" or one of rulepacks.CATEGORIES
    layer: str
    reason: str


@dataclass
class Context:
    account: str
    rules: dict[str, dict] = field(default_factory=dict)
    gmail: dict[str, str] = field(default_factory=dict)   # uid -> category
    ai_min_confidence: float = 0.8


INBOX_DEFAULT = Decision("inbox", "default", "No rule matched")

# Narrower than core._PROTECTED_SUBJECT_HINT on purpose: order, invoice and
# shipping words must reach the Receipts layer.
PROTECTED_SUBJECT = re.compile(
    r"\b(?:pin|sign[ -]?in|log[ -]?in|password|passwort|verif\w*|"
    r"remember me|activat\w*[^\n]*\baccount|please confirm|"
    r"personal information processing|privacy notice|"
    r"confirm[^\n]*\b(?:mailbox|account)|incoming messages[^\n]*\bdelayed|"
    r"(?:security|verification|login|sign[ -]?in|one[- ]time|confirmation|"
    r"bestätigungs|sicherheits|anmelde)[ -]?code|otp|2fa|mfa|"
    r"appointment|termin|booking|reservation|flight|boarding pass|hotel|"
    r"contract|vertrag|agreement|medical|doctor|prescription|test results?|"
    r"insurance|tax|steuer|support (?:ticket|case)|incident|outage)\b",
    re.IGNORECASE)
_CALENDAR = re.compile(
    r"^\s*(?:(?:updated )?invitation|accepted|declined|tentative|einladung|"
    r"aktualisierte einladung|angenommen|abgelehnt|zugesagt|abgesagt)\s*:",
    re.IGNORECASE)
_NEWS_SIGNALS = frozenset({"list_post", "mailman", "newsletter_platform"})
_MARKETING_SIGNALS = frozenset({"marketing_esp", "campaign_feedback"})
_RECEIPT_SENDER = re.compile(
    r"^(?:billing|invoices?|receipts?|orders?|payments?|checkout)(?:[-_.+].*)?$",
    re.IGNORECASE)


def decide(row: dict, ctx: Context) -> Decision | None:
    """Return the first matching layer's decision, or None to ask the AI."""
    sender = row["sender"].casefold()
    domain = sender.rpartition("@")[2]
    subject = row["subject"] or ""
    sig = row["signals"]
    gmail = ctx.gmail.get(row["uid"], "")

    if "\\flagged" in {f.casefold() for f in row.get("flags", ())}:
        return Decision("inbox", "flagged", "Message is flagged")
    group = rulepacks.protected_group(domain, subject)
    if group or PROTECTED_SUBJECT.search(subject) or _CALENDAR.match(subject):
        return Decision("inbox", "protected",
                        f"inpector {group} group" if group else "Protected subject")
    rule = ctx.rules.get(sender)
    if rule and rule["source"] in ("user", "learned"):
        return Decision(rule["category"], rule["source"], "Saved sender rule")
    if rule and rule["source"] == "sent":
        return Decision("inbox", "sent", "You wrote to this sender")

    pack = rulepacks.domain_category(domain)
    pack_category, pack_source = pack if pack else ("", "")
    if (signals.RECEIPTS.search(subject) or "fastmail_receipts" in sig
            or (pack_category == "receipts"
                and _RECEIPT_SENDER.match(sender.partition("@")[0]))):
        return Decision("receipts", "receipts",
                        pack_source if pack_category == "receipts"
                        else "Order, invoice or delivery subject")
    if pack_category == "social" or "fastmail_social" in sig or gmail == "social":
        return Decision("social", "social", pack_source or "Social category header")
    if ("auto_submitted" in sig or "fastmail_notifications" in sig
            or gmail == "notifications"
            or (not sig & signals.BULK and "noreply" in sig)):
        return Decision("notifications", "notifications",
                        "Automated notice (Auto-Submitted, no-reply or subject)")
    if (sig & _NEWS_SIGNALS or pack_category == "news" or gmail == "news"
            or ("list_id" in sig and not sig & _MARKETING_SIGNALS)):
        return Decision("news", "news", pack_source if pack_category == "news"
                        else "Mailing list or newsletter platform")
    if (sig & _MARKETING_SIGNALS or pack_category == "promotions"
            or "fastmail_promotions" in sig or gmail == "promotions"
            or ("one_click" in sig and signals.PROMOTIONS.search(subject))):
        return Decision("promotions", "promotions",
                        pack_source if pack_category == "promotions"
                        else "Marketing headers or promotional subject")
    if "cc_only" in sig:
        return Decision("cc", "cc", "You are only in Cc")
    if (rule and rule["source"] == "ai"
            and (rule["confidence"] or 0.0) >= ctx.ai_min_confidence):
        return Decision(rule["category"], "ai", "Saved AI sender rule")
    return None
