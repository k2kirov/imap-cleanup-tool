"""Vendored, header-only sorting data merged into one domain lookup.

Sources (see THIRD_PARTY_NOTICES.md): inpector/sieve-filters (CC0),
poli0981/proton-sieve-filters (CC0), scrothers/sieve-filters (MIT).
Nothing here reads the network or the mailbox.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

CATEGORIES = ("social", "news", "promotions", "notifications", "receipts", "cc")
# When two packs list one domain, the category checked first by the layer
# chain wins.
_PRIORITY = ("receipts", "social", "notifications", "news", "promotions")
# inpector groups that pick a folder by sender domain.
_INPECTOR_DOMAIN_MAP = {"Deliveries": "receipts", "Finances": "receipts",
                        "Fix Costs": "receipts", "Shopping": "promotions"}
# inpector groups that keep mail in INBOX before any folder layer.
PROTECTED_GROUPS = ("Security", "Travelling")
# inpector groups whose subject terms mean an order or a delivery.
_RECEIPT_GROUPS = ("Deliveries", "Shopping")
# Social domains already used by triage (inpector Social Media list + LinkedIn).
_BUILTIN_SOCIAL = ("facebook.com", "facebookmail.com", "flickr.com",
                   "instagram.com", "linkedin.com", "pinterest.com",
                   "reddit.com", "tiktok.com", "tumblr.com", "twitter.com",
                   "x.com")


def _load(name: str) -> dict:
    return json.loads(Path(__file__).with_name(name).read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _groups() -> dict[str, dict]:
    return {g["name"]: g for g in _load("inpector_rules.json")["groups"]}


@lru_cache(maxsize=1)
def _tables() -> tuple[dict[str, tuple[str, str]], dict[str, tuple[str, str]]]:
    """Return (exact, subdomain) maps of domain -> (category, source)."""
    entries: list[tuple[str, str, str, str]] = [
        (d, "subdomains", "social", "builtin social") for d in _BUILTIN_SOCIAL]
    for group in _load("inpector_rules.json")["groups"]:
        category = _INPECTOR_DOMAIN_MAP.get(group["name"])
        if category:
            entries += [(d.lower(), "subdomains", category,
                         f"inpector {group['name']}") for d in group["domains"]]
    for file_name, label in (("poli0981_rules.json", "poli0981"),
                             ("scrothers_rules.json", "scrothers")):
        for category, items in _load(file_name)["categories"].items():
            entries += [(i["match"].lower(), i["scope"], category,
                         f"{label} {category}") for i in items]
    exact: dict[str, tuple[str, str]] = {}
    subdomains: dict[str, tuple[str, str]] = {}
    # sorted() is stable, so earlier sources win inside one category.
    for domain, scope, category, source in sorted(
            entries, key=lambda e: _PRIORITY.index(e[2])):
        target = subdomains if scope == "subdomains" else exact
        target.setdefault(domain, (category, source))
    return exact, subdomains


def domain_category(domain: str) -> tuple[str, str] | None:
    """Return (category, source) for a sender domain, or None."""
    domain = (domain or "").strip().lower().rstrip(".")
    if not domain:
        return None
    exact, subdomains = _tables()
    if domain in exact:
        return exact[domain]
    labels = domain.split(".")
    for start in range(len(labels) - 1):
        hit = subdomains.get(".".join(labels[start:]))
        if hit:
            return hit
    return None


@lru_cache(maxsize=None)
def _terms_regex(names: tuple[str, ...]) -> re.Pattern | None:
    terms = sorted({t.casefold() for name in names
                    for t in _groups()[name]["subject_contains"]},
                   key=len, reverse=True)
    if not terms:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(t) for t in terms)
                      + r")(?!\w)", re.IGNORECASE)


def _in_domains(domain: str, known: list[str]) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in known)


def protected_group(domain: str, subject: str) -> str | None:
    """Name of the inpector group that keeps this mail in INBOX, or None."""
    domain = (domain or "").lower()
    for name in PROTECTED_GROUPS:
        pattern = _terms_regex((name,))
        if (_in_domains(domain, _groups()[name]["domains"])
                or (pattern is not None and pattern.search(subject or ""))):
            return name
    return None


def receipt_subject(subject: str) -> bool:
    """True when inpector order or delivery terms appear as whole words."""
    pattern = _terms_regex(_RECEIPT_GROUPS)
    return bool(pattern is not None and pattern.search(subject or ""))
