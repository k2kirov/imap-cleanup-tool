"""Header-only category signals for auto-sort. Pure functions, no IMAP."""

from __future__ import annotations

import re
from email.utils import getaddresses

RECEIPTS = re.compile(
    r"\b(receipt|invoice|order (confirm|#|no\.?|number|\d{4,})|your order|"
    r"payment (received|confirm)|thank(s| you) for your (order|purchase|payment)|"
    r"shipped|shipping confirm|out for delivery|tracking (?:number|details)|refund|"
    r"subscription renewed|renewal|Rechnung|Quittung|Beleg|Bestellbestätigung|"
    r"Ihre Bestellung|Deine Bestellung|Bestellung (Nr|eingegangen)|"
    r"Zahlungsbestätigung|Zahlung erhalten|versandt|verschickt|"
    r"Versandbestätigung|Sendungsverfolgung|zugestellt|Lieferung|Gutschrift|"
    r"Rückerstattung|Abo verlängert)\b", re.IGNORECASE)
# "your account" is left out on purpose: account-security mail is protected.
NOTIFICATIONS = re.compile(
    r"\b(notification|reminder|alert|update[sd]?|mentioned you|commented|"
    r"replied|new (message|comment|follower)|invited you|shared .* with you|"
    r"your (report|statement|weekly|monthly)|build (failed|passed)|digest|"
    r"status|Benachrichtigung|Erinnerung|Hinweis|Mitteilung|Aktualisierung|"
    r"neue Nachricht|hat dich erwähnt|Kontoauszug|Monatsübersicht|"
    r"Wochenbericht|Statusmeldung)\b", re.IGNORECASE)
PROMOTIONS = re.compile(
    r"(\d{1,2}\s?%|\b(sale|deal|offer|discount|coupon|promo|save|"
    r"free shipping|limited time|last chance|ends (today|tonight)|"
    r"black friday|cyber monday|exclusive|new arrivals|Angebot|Rabatt|"
    r"Gutschein|Aktion|Sonderangebot|reduziert|gratis|kostenlos|"
    r"versandkostenfrei|nur heute|letzte Chance|Schnäppchen|Neuheiten|"
    r"exklusiv)\b)", re.IGNORECASE)

NEWSLETTER_PLATFORMS = frozenset({
    "substack.com", "beehiiv.com", "convertkit.com", "convertkit-mail.com",
    "convertkit-mail2.com", "kit.com", "buttondown.email", "ghost.io",
    "cmail19.com", "cmail20.com"})
# Headers set by marketing ESPs (Mailchimp, Klaviyo, ExactTarget, Emarsys,
# MailUp) or campaign tools. Transactional-only ESP headers are not listed.
_MARKETING_HEADERS = ("x-mc-user", "x-kmail-message", "x-kmail-account",
                      "x-sfmc-stack", "x-csa-complaints", "x-campaign",
                      "x-campaignid")
_NOREPLY = re.compile(
    r"^(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|notifications?|alerts?|"
    r"mailer-daemon)(?:[-_.+].*)?$", re.IGNORECASE)
# Fastmail X-ME-VSCategory values -> auto-sort category.
_FASTMAIL = {"purchases": "receipts", "community": "social",
             "alerts": "notifications", "commercial": "promotions"}

BULK = frozenset({"list_id", "list_post", "mailman", "list_unsubscribe",
                  "one_click", "bulk", "marketing_esp", "campaign_feedback",
                  "newsletter_platform"})


def detect(headers: dict[str, str], sender: str, account: str) -> frozenset[str]:
    """Return the signal names present in one message's headers."""
    h = {name.casefold(): value or "" for name, value in headers.items()}

    def has(name: str) -> bool:
        return bool(h.get(name, "").strip())

    found: set[str] = set()
    auto = h.get("auto-submitted", "").strip().casefold()
    if auto and auto != "no":
        found.add("auto_submitted")
    if has("list-id"):
        found.add("list_id")
    if has("list-post"):
        found.add("list_post")
    if any(has(n) for n in ("x-mailman-version", "x-beenthere",
                            "x-mailinglist", "x-mailing-list")):
        found.add("mailman")
    if has("list-unsubscribe"):
        found.add("list_unsubscribe")
    if "one-click" in h.get("list-unsubscribe-post", "").casefold():
        found.add("one_click")
    if any(w in h.get("precedence", "").casefold() for w in ("bulk", "list", "junk")):
        found.add("bulk")
    if any(has(n) for n in _MARKETING_HEADERS):
        found.add("marketing_esp")
    if "campaign" in h.get("x-feedback-id", "").casefold():
        found.add("campaign_feedback")
    local, _, domain = (sender or "").casefold().rpartition("@")
    if any(domain == p or domain.endswith("." + p) for p in NEWSLETTER_PLATFORMS):
        found.add("newsletter_platform")
    if _NOREPLY.match(local):
        found.add("noreply")
    me = (account or "").strip().casefold()
    if "@" in me:
        to = {a.casefold() for _, a in getaddresses([h.get("to", "")])}
        cc = {a.casefold() for _, a in getaddresses([h.get("cc", "")])}
        if me in cc and me not in to:
            found.add("cc_only")
    fastmail = _FASTMAIL.get(h.get("x-me-vscategory", "").strip().casefold())
    if fastmail:
        found.add("fastmail_" + fastmail)
    return frozenset(found)
