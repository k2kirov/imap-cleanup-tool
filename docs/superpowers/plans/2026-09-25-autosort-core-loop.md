# Auto-sort Core Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sort new INBOX mail into Social, News, Promotions, Notifications, Receipts and CC folders on a schedule, learn from the user's own folder moves, trust people the user writes to, and ask an LLM only about senders no rule covers. Never delete mail.

**Architecture:** Pure units (`rulepacks`, `signals`, `sortchain`) decide a category from headers. `sortstore` keeps sender rules, the move log, sync state and settings in the existing `triage_rules.sqlite`. `ai_sort` asks a saved model about unknown senders. `autosort` runs one pass: learn, trust Sent, classify new INBOX UIDs, move. The CLI flag `--autosort` and a new web tab drive it; scheduling reuses the existing `interval` job kind.

**Tech Stack:** Python 3.10+, stdlib `imaplib`/`sqlite3`/`email`, `unittest`, FastAPI (web extra), litellm (ai extra), vanilla JS in `web/static/index.html`.

**Spec:** `docs/superpowers/specs/2026-09-25-autosort-core-loop-design.md`

## Global Constraints

- Core and CLI stay stdlib-only (`dependencies = []` in `pyproject.toml`). PyYAML is used only by `scripts/update_rulepacks.py`, never at runtime.
- The app never downloads rules at runtime. Rule packs are vendored JSON with a pinned commit.
- Never delete mail and never use Trash. `\Deleted` appears only inside the existing `COPY` + `STORE` + `UID EXPUNGE` fallback for one UID.
- `\Flagged` mail is never moved. Mail received before `started_at` is never moved unless `--backlog`.
- A message the tool moved once (same `Message-ID` in `move_log`) is never auto-moved again.
- Rule precedence: `user` = `learned` (3) > `sent` (2) > `ai` (1). A lower rank never overwrites a higher one.
- AI defaults: `ai_min_confidence` 0.8, `ai_max_calls` 50 per run. Encrypted model configs cannot run unattended.
- Learning window: 30 days. First Sent scan: 365 days.
- Folder names: `INBOX<delim>Social`, `News`, `Promotions`, `Notifications`, `Receipts`, `CC`; `<delim>` comes from the server `LIST` reply for INBOX (default `.`). `INBOX.Other` is no longer written; the manual tab maps `other` to `promotions`.
- Code comments in English. Tests use `unittest` (see `README.md`: `python -m unittest discover -s tests -v`).
- Run tests with `.venv/bin/python -m unittest ...` from the repo root.
- Lint: Python code follows the existing pylint-clean style. Do not add ESLint anything.

## Deviations from the spec (decided while planning)

- Autosort uses its own narrower protected-subject regex (`sortchain.PROTECTED_SUBJECT`). `core._PROTECTED_SUBJECT_HINT` includes `order`, `invoice`, `receipt`, `shipping`, which would keep every receipt in INBOX and make the Receipts folder empty.
- inpector `Travelling` stays a protected group, as in today's `triage.classify`.
- poli0981 contributes domains only. Its `keyword_groups` are English/Asian-language and too broad ("Daily Update"); subjects use the spec regexes plus inpector terms.
- Learning finds messages by fetching `Message-ID` headers of mail received in the last 30 days in INBOX and each sort folder (one SEARCH + FETCH per folder per run), not one SEARCH per logged move.
- The Auto-sort tab's "Turn on" button installs an `interval` job (15 min) through the existing `/api/jobs/install`; there is no separate `enabled` column.

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `scripts/update_rulepacks.py` | create | Dev tool: regenerate `poli0981_rules.json` from a pinned commit |
| `src/imap_cleanup_tool/poli0981_rules.json` | create (generated) | CC0 domain data: social, news, receipts, promotions |
| `src/imap_cleanup_tool/scrothers_rules.json` | create | MIT domain data: social + retailer marketing subdomains |
| `THIRD_PARTY_NOTICES.md` | create | Sources, licenses, commits, MIT text |
| `src/imap_cleanup_tool/rulepacks.py` | create | Merge packs into domain lookup; protected and receipt subject terms |
| `src/imap_cleanup_tool/signals.py` | create | Header signals and subject regexes |
| `src/imap_cleanup_tool/sortstore.py` | create | SQLite schema, migration, rules, move log, state, settings, AI review |
| `src/imap_cleanup_tool/sortchain.py` | create | Layer chain `decide(row, ctx)` |
| `src/imap_cleanup_tool/ai_sort.py` | create | Per-sender LLM fallback |
| `src/imap_cleanup_tool/autosort.py` | create | Fetch, lock, learn, trust Sent, run, undo |
| `src/imap_cleanup_tool/triage.py` | modify | Use `sortstore`; folder helpers; `move_uid`; `other` -> `promotions` |
| `src/imap_cleanup_tool/cli.py` | modify | `--autosort`, `--backlog`, `_run_autosort` |
| `src/imap_cleanup_tool/webapp.py` | modify | `/api/autosort/*`; `JobIn.autosort` |
| `src/imap_cleanup_tool/web/static/index.html` | modify | Inbox sort tab categories; new Auto-sort tab |
| `tests/fake_imap.py` | create | Multi-folder IMAP fake |
| `tests/test_rulepacks.py`, `tests/test_signals.py`, `tests/test_sortstore.py`, `tests/test_sortchain.py`, `tests/test_ai_sort.py`, `tests/test_autosort.py` | create | Unit tests |
| `tests/test_triage.py`, `tests/test_cli_config.py`, `tests/test_webapp.py` | modify | Updated and new tests |
| `README.md`, `TRIAGE_RULES.md`, `CONFIG_REFERENCE.md` | modify | Docs |

---

### Task 1: Rule packs

**Files:**
- Create: `scripts/update_rulepacks.py`
- Create: `src/imap_cleanup_tool/poli0981_rules.json` (generated by the script)
- Create: `src/imap_cleanup_tool/scrothers_rules.json`
- Create: `src/imap_cleanup_tool/rulepacks.py`
- Create: `THIRD_PARTY_NOTICES.md`
- Test: `tests/test_rulepacks.py`

**Interfaces:**
- Consumes: `src/imap_cleanup_tool/inpector_rules.json` (existing; `{"groups": [{"name", "domains", "subject_contains"}]}`).
- Produces:
  - `rulepacks.CATEGORIES: tuple[str, ...]` = `("social", "news", "promotions", "notifications", "receipts", "cc")`
  - `rulepacks.domain_category(domain: str) -> tuple[str, str] | None` — `(category, source label)`
  - `rulepacks.protected_group(domain: str, subject: str) -> str | None` — `"Security"` / `"Travelling"` / `None`
  - `rulepacks.receipt_subject(subject: str) -> bool`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rulepacks.py`:

```python
"""Vendored rule packs: conversion, merge priority and domain lookup."""

import importlib.util
import unittest
from pathlib import Path
from unittest import mock

from imap_cleanup_tool import rulepacks

ROOT = Path(__file__).resolve().parent.parent


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "update_rulepacks", ROOT / "scripts" / "update_rulepacks.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConvertTests(unittest.TestCase):
    def test_convert_keeps_only_allow_entries(self):
        script = _load_script()
        files = {
            "social": {"domains": [
                {"match": "Discord.com", "kind": "allow", "scope": "subdomains"},
                {"match": "linkedin.com", "kind": "ceded", "scope": "subdomains",
                 "ceded_to": "work"},
                {"match": "faceb00k.com", "kind": "block", "scope": "exact"},
                {"match": "example.tld", "kind": "tld-example", "scope": "exact"},
                {"match": "discord.com", "kind": "allow", "scope": "subdomains"},
            ]},
            "invoice": {"domains": [
                {"match": "paddle.com", "kind": "allow", "scope": "exact"}]},
        }
        out = script.convert("abc123", files)
        self.assertEqual(out["commit"], "abc123")
        self.assertEqual(out["license"], "CC0-1.0")
        self.assertEqual(out["categories"], {
            "social": [{"match": "discord.com", "scope": "subdomains"}],
            "receipts": [{"match": "paddle.com", "scope": "exact"}],
        })


class DomainCategoryTests(unittest.TestCase):
    def tearDown(self):
        rulepacks._tables.cache_clear()
        rulepacks._groups.cache_clear()
        rulepacks._terms_regex.cache_clear()

    def test_vendored_packs_load(self):
        self.assertEqual(rulepacks.domain_category("promo.newegg.com")[0],
                         "promotions")
        self.assertEqual(rulepacks.domain_category("mail.facebookmail.com")[0],
                         "social")
        self.assertEqual(rulepacks.domain_category("dhl.de")[0], "receipts")
        self.assertEqual(rulepacks.domain_category("news.substack.com")[0], "news")
        self.assertIsNone(rulepacks.domain_category("facebookmail.com.evil.test"))
        self.assertIsNone(rulepacks.domain_category(""))

    def _fake(self, poli):
        fake = {
            "inpector_rules.json": {"groups": [
                {"name": "Shopping", "domains": ["shop.test"],
                 "subject_contains": []}]},
            "poli0981_rules.json": {"categories": poli},
            "scrothers_rules.json": {"categories": {}},
        }
        return mock.patch.object(rulepacks, "_load", side_effect=lambda n: fake[n])

    def test_receipts_beat_promotions_for_same_domain(self):
        with self._fake({"receipts": [{"match": "shop.test", "scope": "subdomains"}]}), \
             mock.patch.object(rulepacks, "_BUILTIN_SOCIAL", ()):
            rulepacks._tables.cache_clear()
            self.assertEqual(rulepacks.domain_category("a.shop.test"),
                             ("receipts", "poli0981 receipts"))

    def test_exact_scope_does_not_match_subdomains(self):
        with self._fake({"social": [{"match": "only.test", "scope": "exact"}]}), \
             mock.patch.object(rulepacks, "_BUILTIN_SOCIAL", ()):
            rulepacks._tables.cache_clear()
            self.assertEqual(rulepacks.domain_category("only.test"),
                             ("social", "poli0981 social"))
            self.assertIsNone(rulepacks.domain_category("a.only.test"))


class SubjectTermTests(unittest.TestCase):
    def test_protected_group_uses_word_boundaries(self):
        self.assertEqual(rulepacks.protected_group(
            "example.com", "Your one-time password"), "Security")
        self.assertEqual(rulepacks.protected_group("bahn.de", "Newsletter"),
                         "Travelling")
        self.assertIsNone(rulepacks.protected_group(
            "example.com", "Spotify weekly mix"))

    def test_receipt_subject_terms(self):
        self.assertTrue(rulepacks.receipt_subject("Ihre Bestellung ist unterwegs"))
        self.assertFalse(rulepacks.receipt_subject("Border control news"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_rulepacks -v`
Expected: FAIL with `ImportError: cannot import name 'rulepacks'`.

- [ ] **Step 3: Write the converter script**

Create `scripts/update_rulepacks.py`:

```python
"""Regenerate src/imap_cleanup_tool/poli0981_rules.json from a pinned commit.

Dev tool only: it needs PyYAML and network access. The app never downloads
rules at runtime.

Usage: python scripts/update_rulepacks.py <commit-sha>
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

REPO = "poli0981/proton-sieve-filters"
# poli0981 category file -> auto-sort category.
CATEGORY_MAP = {"social": "social", "news": "news", "invoice": "receipts",
                "shipping": "receipts", "shopping": "promotions"}
OUT = (Path(__file__).resolve().parent.parent / "src" / "imap_cleanup_tool"
       / "poli0981_rules.json")


def _fetch(commit: str, name: str) -> dict:
    import yaml  # dev-only dependency

    url = (f"https://raw.githubusercontent.com/{REPO}/{commit}"
           f"/data/categories/{name}.yml")
    with urllib.request.urlopen(url, timeout=30) as resp:
        return yaml.safe_load(resp.read().decode("utf-8"))


def convert(commit: str, files: dict[str, dict]) -> dict:
    """Keep only ``kind: allow`` domains; ``ceded``, ``block`` and examples go."""
    categories: dict[str, list[dict]] = {}
    for name, data in files.items():
        target = CATEGORY_MAP[name]
        for entry in data.get("domains", []):
            if entry.get("kind") != "allow":
                continue
            categories.setdefault(target, []).append({
                "match": entry["match"].strip().lower(),
                "scope": entry.get("scope", "subdomains")})
    for target, entries in categories.items():
        unique = {(e["match"], e["scope"]): e for e in entries}
        categories[target] = sorted(unique.values(),
                                    key=lambda e: (e["match"], e["scope"]))
    return {"source": f"https://github.com/{REPO}", "commit": commit,
            "license": "CC0-1.0",
            "scope": "Sender domains only, from data/categories/*.yml. "
                     "Keyword groups are not used.",
            "categories": categories}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    commit = argv[1]
    files = {name: _fetch(commit, name) for name in CATEGORY_MAP}
    OUT.write_text(json.dumps(convert(commit, files), indent=2,
                              ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
```

- [ ] **Step 4: Generate the poli0981 pack**

Run: `.venv/bin/python scripts/update_rulepacks.py 20fe383b94db4894f9a2a8525a807f65d9ed232b`
Expected: `Wrote .../src/imap_cleanup_tool/poli0981_rules.json`. Then check: `.venv/bin/python -c "import json;d=json.load(open('src/imap_cleanup_tool/poli0981_rules.json'));print({k:len(v) for k,v in d['categories'].items()})"` prints counts for `social`, `news`, `receipts`, `promotions`, each above 0.

- [ ] **Step 5: Write the scrothers pack**

Create `src/imap_cleanup_tool/scrothers_rules.json`:

```json
{
  "source": "https://github.com/scrothers/sieve-filters",
  "commit": "76d7728181cb2b2df3c072e242a0bd7e9b938873",
  "license": "MIT",
  "notice": "Copyright (c) 2022 Steven Crothers",
  "scope": "From-domain lists of social_media.sieve and shopping.sieve only.",
  "categories": {
    "social": [
      {"match": "facebookmail.com", "scope": "subdomains"},
      {"match": "redditmail.com", "scope": "subdomains"}
    ],
    "promotions": [
      {"match": "b.cabelas.com", "scope": "subdomains"},
      {"match": "comms.activision.com", "scope": "subdomains"},
      {"match": "e-mail.gopro.com", "scope": "subdomains"},
      {"match": "e.fitbit.com", "scope": "subdomains"},
      {"match": "e.godiva.com", "scope": "subdomains"},
      {"match": "e2.bathandbodyworks.com", "scope": "subdomains"},
      {"match": "ea.tractorsupply.com", "scope": "subdomains"},
      {"match": "em.harborfreight.com", "scope": "subdomains"},
      {"match": "email.aarons.com", "scope": "subdomains"},
      {"match": "email.bedbathandbeyond.com", "scope": "subdomains"},
      {"match": "emailinfo.buffalowildwings.com", "scope": "subdomains"},
      {"match": "emails.monoprice.com", "scope": "subdomains"},
      {"match": "engage.windows.com", "scope": "subdomains"},
      {"match": "littlecaesars.fbmta.com", "scope": "subdomains"},
      {"match": "mail.zillow.com", "scope": "subdomains"},
      {"match": "mailer.humblebundle.com", "scope": "subdomains"},
      {"match": "members.wayfair.com", "scope": "subdomains"},
      {"match": "menard.messages1.com", "scope": "subdomains"},
      {"match": "microcenterinsider.com", "scope": "subdomains"},
      {"match": "p.touchofmodern.com", "scope": "subdomains"},
      {"match": "promo.newegg.com", "scope": "subdomains"},
      {"match": "shop.meijer.com", "scope": "subdomains"}
    ]
  }
}
```

- [ ] **Step 6: Write the loader**

Create `src/imap_cleanup_tool/rulepacks.py`:

```python
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
```

- [ ] **Step 7: Write the notices file**

Create `THIRD_PARTY_NOTICES.md`:

```markdown
# Third-party rule data

The auto-sort feature ships header-only sorting data from these sources.
The app never downloads rules at runtime.

| File | Source | Commit | License |
|---|---|---|---|
| `src/imap_cleanup_tool/inpector_rules.json` | https://github.com/inpector/sieve-filters | fe5a0ce637504c6baf70514f13f369f09d234de0 | CC0-1.0 |
| `src/imap_cleanup_tool/poli0981_rules.json` | https://github.com/poli0981/proton-sieve-filters (`data/categories/*.yml`) | 20fe383b94db4894f9a2a8525a807f65d9ed232b | CC0-1.0 |
| `src/imap_cleanup_tool/scrothers_rules.json` | https://github.com/scrothers/sieve-filters | 76d7728181cb2b2df3c072e242a0bd7e9b938873 | MIT |

Regenerate the poli0981 file with `python scripts/update_rulepacks.py <commit>`.

## scrothers/sieve-filters (MIT)

MIT License

Copyright (c) 2022 Steven Crothers

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `.venv/bin/python -m unittest tests.test_rulepacks -v`
Expected: 6 tests, all PASS. If `news.substack.com` fails, open `poli0981_rules.json` and confirm `substack.com` is under `news`; it is `kind: allow` in `news.yml` at the pinned commit.

- [ ] **Step 9: Check the wheel includes the new JSON files**

Run: `grep -n "include\|artifacts\|packages" pyproject.toml`
If the wheel target lists files explicitly, add `poli0981_rules.json` and `scrothers_rules.json` next to `inpector_rules.json`. If it packages the whole `src/imap_cleanup_tool` directory, change nothing.

- [ ] **Step 10: Commit**

```bash
git add scripts/update_rulepacks.py src/imap_cleanup_tool/poli0981_rules.json src/imap_cleanup_tool/scrothers_rules.json src/imap_cleanup_tool/rulepacks.py THIRD_PARTY_NOTICES.md tests/test_rulepacks.py
git commit -m "feat(autosort): vendor poli0981 and scrothers rule packs"
```

---

### Task 2: Header signals and subject regexes

**Files:**
- Create: `src/imap_cleanup_tool/signals.py`
- Test: `tests/test_signals.py`

**Interfaces:**
- Produces:
  - `signals.detect(headers: dict[str, str], sender: str, account: str) -> frozenset[str]` — header names are matched case-insensitively. Signal names: `auto_submitted`, `list_id`, `list_post`, `mailman`, `list_unsubscribe`, `one_click`, `bulk`, `marketing_esp`, `campaign_feedback`, `newsletter_platform`, `noreply`, `cc_only`, `fastmail_receipts`, `fastmail_social`, `fastmail_notifications`, `fastmail_promotions`.
  - `signals.BULK: frozenset[str]` — signals that mark list or bulk mail.
  - `signals.RECEIPTS`, `signals.NOTIFICATIONS`, `signals.PROMOTIONS: re.Pattern`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_signals.py`:

```python
"""Header-only category signals and subject patterns."""

import unittest

from imap_cleanup_tool import signals

ME = "me@example.com"


class DetectTests(unittest.TestCase):
    def test_list_and_bulk_headers(self):
        sig = signals.detect({"List-Id": "<dev.lists.example.org>",
                              "List-Post": "<mailto:dev@lists.example.org>",
                              "Precedence": "list"},
                             "dev@lists.example.org", ME)
        self.assertTrue({"list_id", "list_post", "bulk"} <= sig)

    def test_marketing_esp_and_one_click(self):
        sig = signals.detect({"X-Kmail-Message": "x",
                              "List-Unsubscribe": "<https://u.test>",
                              "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
                             "shop@brand.test", ME)
        self.assertTrue({"marketing_esp", "list_unsubscribe", "one_click"} <= sig)

    def test_campaign_feedback_id(self):
        self.assertIn("campaign_feedback", signals.detect(
            {"X-Feedback-ID": "12:34:campaign:ESP"}, "a@b.test", ""))
        self.assertNotIn("campaign_feedback", signals.detect(
            {"Feedback-ID": "a:b:c:SenderID"}, "a@b.test", ""))

    def test_auto_submitted_no_is_ignored(self):
        self.assertIn("auto_submitted", signals.detect(
            {"Auto-Submitted": "auto-generated"}, "a@b.test", ""))
        self.assertNotIn("auto_submitted", signals.detect(
            {"Auto-Submitted": "no"}, "a@b.test", ""))

    def test_cc_only(self):
        self.assertIn("cc_only", signals.detect(
            {"To": "Boss <boss@example.com>", "Cc": "Me <ME@example.com>"},
            "boss@example.com", ME))
        self.assertNotIn("cc_only", signals.detect(
            {"To": ME, "Cc": ME}, "x@y.test", ME))

    def test_newsletter_platform_and_noreply(self):
        self.assertIn("newsletter_platform",
                      signals.detect({}, "writer@mail.substack.com", ""))
        self.assertIn("noreply", signals.detect({}, "no-reply@service.test", ""))
        self.assertNotIn("noreply", signals.detect({}, "anna@service.test", ""))

    def test_fastmail_category(self):
        self.assertIn("fastmail_receipts", signals.detect(
            {"X-ME-VSCategory": "Purchases"}, "a@b.test", ""))


class SubjectPatternTests(unittest.TestCase):
    def test_receipts(self):
        for subject in ("Your order #123 has shipped", "Ihre Rechnung Nr. 42",
                        "Zahlungsbestätigung", "Out for delivery"):
            self.assertTrue(signals.RECEIPTS.search(subject), subject)
        self.assertIsNone(signals.RECEIPTS.search("Lunch tomorrow?"))

    def test_notifications_do_not_match_security_codes(self):
        self.assertTrue(signals.NOTIFICATIONS.search("Build passed"))
        self.assertTrue(signals.NOTIFICATIONS.search("Neue Nachricht von Anna"))
        self.assertIsNone(signals.NOTIFICATIONS.search(
            "Your security code is 123456"))

    def test_promotions(self):
        self.assertTrue(signals.PROMOTIONS.search("20% off everything"))
        self.assertTrue(signals.PROMOTIONS.search("Rabatt nur heute"))
        self.assertIsNone(signals.PROMOTIONS.search("Meeting notes"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_signals -v`
Expected: FAIL with `ImportError: cannot import name 'signals'`.

- [ ] **Step 3: Write the implementation**

Create `src/imap_cleanup_tool/signals.py`:

```python
"""Header-only category signals for auto-sort. Pure functions, no IMAP."""

from __future__ import annotations

import re
from email.utils import getaddresses

RECEIPTS = re.compile(
    r"\b(receipt|invoice|order (confirm|#|no\.?|number)|your order|"
    r"payment (received|confirm)|thank(s| you) for your (order|purchase|payment)|"
    r"shipped|shipping confirm|out for delivery|delivered|tracking|refund|"
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m unittest tests.test_signals -v`
Expected: 10 tests, all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/imap_cleanup_tool/signals.py tests/test_signals.py
git commit -m "feat(autosort): add header signals and subject patterns"
```

---

### Task 3: Sort store and triage wiring

**Files:**
- Create: `src/imap_cleanup_tool/sortstore.py`
- Modify: `src/imap_cleanup_tool/triage.py` (imports, `_rules_db`, `sender_rules`, `train_sender`, `forget_sender`, `classify` learned check)
- Test: `tests/test_sortstore.py`

**Interfaces:**
- Consumes: `rulepacks.CATEGORIES`; `triage.rules_path() -> Path` (existing; tests patch it).
- Produces (all `account`/`sender` values are stripped and lowercased):
  - `sortstore.VALID_CATEGORIES: frozenset[str]` — `CATEGORIES` plus `"inbox"`
  - `sortstore.save_rule(account, sender, category, source, confidence=None) -> bool` — `source` in `user|learned|sent|ai`; returns False when a higher-rank rule blocks it; raises `ValueError` for an unknown category or source
  - `sortstore.rules(account) -> dict[str, dict]` — `{sender: {"category", "source", "confidence"}}`
  - `sortstore.forget_rule(account, sender) -> None`
  - `sortstore.new_run_id() -> str`
  - `sortstore.log_move(account, run_id, *, message_id, uid, sender, subject, source_folder, target_folder, layer, reason) -> int`
  - `sortstore.moves(account, limit=100) -> list[dict]` (newest first)
  - `sortstore.moves_since(account, since_iso: str) -> list[dict]` (not undone, oldest first)
  - `sortstore.get_move(account, move_id: int) -> dict | None`
  - `sortstore.run_moves(account, run_id) -> list[dict]` (not undone)
  - `sortstore.mark_undone(move_id: int) -> None`
  - `sortstore.moved_message_ids(account) -> set[str]`
  - `sortstore.get_state(account, folder) -> dict | None` — `{"uidvalidity": str, "last_uid": int}`
  - `sortstore.set_state(account, folder, uidvalidity: str, last_uid: int) -> None`
  - `sortstore.get_settings(account) -> dict` — keys `started_at`, `ai_model`, `ai_min_confidence`, `ai_max_calls`
  - `sortstore.update_settings(account, **fields) -> dict`
  - `sortstore.add_review(account, sender, category, confidence, reason)`, `sortstore.reviews(account) -> list[dict]`, `sortstore.clear_review(account, sender)`
  - `triage.LEGACY_CATEGORIES = {"other": "promotions"}`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sortstore.py`:

```python
"""Auto-sort state: migration, rule precedence, move log, sync state."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from imap_cleanup_tool import sortstore, triage

A = "me@example.com"
EPOCH = "2000-01-01T00:00:00+00:00"


class SortStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patch = mock.patch.object(triage, "rules_path",
                                  return_value=Path(self.temp.name) / "rules.sqlite")
        patch.start()
        self.addCleanup(patch.stop)

    def test_migrates_old_sender_rule_table(self):
        with closing(sqlite3.connect(triage.rules_path())) as conn:
            conn.execute("CREATE TABLE sender_rule (account TEXT NOT NULL, "
                         "sender TEXT NOT NULL, category TEXT NOT NULL, "
                         "PRIMARY KEY (account, sender))")
            conn.execute("INSERT INTO sender_rule VALUES (?, ?, ?)",
                         (A, "a@b.test", "social"))
            conn.commit()
        self.assertEqual(sortstore.rules(A), {"a@b.test": {
            "category": "social", "source": "user", "confidence": None}})

    def test_rule_precedence(self):
        self.assertTrue(sortstore.save_rule(A, "x@y.test", "news", "ai", 0.9))
        self.assertTrue(sortstore.save_rule(A, "x@y.test", "inbox", "sent"))
        self.assertFalse(sortstore.save_rule(A, "x@y.test", "promotions", "ai", 0.95))
        self.assertTrue(sortstore.save_rule(A, "X@Y.test", "social", "learned"))
        self.assertFalse(sortstore.save_rule(A, "x@y.test", "inbox", "sent"))
        self.assertTrue(sortstore.save_rule(A, "x@y.test", "receipts", "user"))
        self.assertEqual(sortstore.rules(A)["x@y.test"]["category"], "receipts")
        with self.assertRaises(ValueError):
            sortstore.save_rule(A, "x@y.test", "trash", "user")
        with self.assertRaises(ValueError):
            sortstore.save_rule(A, "x@y.test", "news", "guess")
        sortstore.forget_rule(A, "x@y.test")
        self.assertEqual(sortstore.rules(A), {})

    def test_move_log_roundtrip(self):
        run = sortstore.new_run_id()
        move_id = sortstore.log_move(
            A, run, message_id="<1@x>", uid="5", sender="S@x.test", subject="Hi",
            source_folder="INBOX", target_folder="INBOX.News", layer="news",
            reason="List-Post")
        self.assertEqual([m["id"] for m in sortstore.moves(A)], [move_id])
        self.assertEqual(sortstore.moves(A)[0]["sender"], "s@x.test")
        self.assertEqual(sortstore.moved_message_ids(A), {"<1@x>"})
        self.assertEqual(len(sortstore.moves_since(A, EPOCH)), 1)
        self.assertEqual(len(sortstore.run_moves(A, run)), 1)
        sortstore.mark_undone(move_id)
        self.assertEqual(sortstore.moves_since(A, EPOCH), [])
        self.assertEqual(sortstore.run_moves(A, run), [])
        self.assertIsNotNone(sortstore.get_move(A, move_id)["undone_at"])
        self.assertIsNone(sortstore.get_move("other@example.com", move_id))

    def test_state_settings_and_review(self):
        self.assertIsNone(sortstore.get_state(A, "INBOX"))
        sortstore.set_state(A, "INBOX", "7", 12)
        self.assertEqual(sortstore.get_state(A, "INBOX"),
                         {"uidvalidity": "7", "last_uid": 12})
        self.assertEqual(sortstore.get_settings(A), {
            "started_at": None, "ai_model": "", "ai_min_confidence": 0.8,
            "ai_max_calls": 50})
        sortstore.update_settings(A, ai_model="local", ai_min_confidence=0.9)
        self.assertEqual(sortstore.get_settings(A)["ai_model"], "local")
        self.assertEqual(sortstore.get_settings(A)["ai_min_confidence"], 0.9)
        with self.assertRaises(ValueError):
            sortstore.update_settings(A, ai_min_confidence=1.5)
        with self.assertRaises(ValueError):
            sortstore.update_settings(A, ai_max_calls=501)
        with self.assertRaises(ValueError):
            sortstore.update_settings(A, unknown=1)
        sortstore.add_review(A, "q@z.test", "news", 0.4, "unsure")
        self.assertEqual([r["sender"] for r in sortstore.reviews(A)], ["q@z.test"])
        sortstore.clear_review(A, "q@z.test")
        self.assertEqual(sortstore.reviews(A), [])

    def test_triage_train_sender_uses_store(self):
        triage.train_sender(A, "old@x.test", "other")
        self.assertEqual(triage.sender_rules(A), {"old@x.test": "promotions"})
        self.assertEqual(sortstore.rules(A)["old@x.test"]["source"], "user")
        with self.assertRaisesRegex(ValueError, "Choose Inbox or a sort folder"):
            triage.train_sender(A, "old@x.test", "trash")
        triage.forget_sender(A, "old@x.test")
        self.assertEqual(triage.sender_rules(A), {})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_sortstore -v`
Expected: FAIL with `ImportError: cannot import name 'sortstore'`.

- [ ] **Step 3: Write `sortstore.py`**

Create `src/imap_cleanup_tool/sortstore.py`:

```python
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


def rules(account: str) -> dict[str, dict]:
    with closing(connect()) as conn:
        rows = conn.execute("SELECT sender, category, source, confidence FROM "
                            "sender_rule WHERE account=?", (_norm(account),)).fetchall()
    return {r["sender"]: {"category": r["category"], "source": r["source"],
                          "confidence": r["confidence"]} for r in rows}


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
```

- [ ] **Step 4: Wire `triage.py` to the store**

In `src/imap_cleanup_tool/triage.py`:

1. Replace the imports block `import json` … `from .scheduler import config_dir` so it no longer imports `sqlite3` or `closing`, and imports the store:

```python
import json
import re
from dataclasses import dataclass
from email import message_from_bytes
from functools import lru_cache
from pathlib import Path

from . import core, sortstore
from .scheduler import config_dir
```

2. Add below `DESTINATIONS = ...` (Task 5 removes `DESTINATIONS`):

```python
# "other" is the old manual-tab name of the Promotions folder.
LEGACY_CATEGORIES = {"other": "promotions"}
```

3. Delete `_rules_db()` and replace `sender_rules`, `train_sender` and `forget_sender` with:

```python
def sender_rules(account: str) -> dict[str, str]:
    """Return the saved sender choices for one mailbox (any rule source)."""
    return {sender: rule["category"]
            for sender, rule in sortstore.rules(account).items()}


def train_sender(account: str, sender: str, category: str) -> None:
    """Remember an explicit choice for future sorting; Inbox is a keep rule."""
    category = LEGACY_CATEGORIES.get(category, category)
    if category not in sortstore.VALID_CATEGORIES:
        raise ValueError("Choose Inbox or a sort folder.")
    sortstore.save_rule(account, sender, category, "user")


def forget_sender(account: str, sender: str) -> None:
    sortstore.forget_rule(account, sender)
```

4. In `classify`, change `if learned in {*DESTINATIONS, "inbox"}:` to `if learned in sortstore.VALID_CATEGORIES:`.

- [ ] **Step 5: Run the store and triage tests**

Run: `.venv/bin/python -m unittest tests.test_sortstore tests.test_triage -v`
Expected: all PASS. `test_triage` still passes because `rules_path()` and `sender_rules()` keep their shapes.

- [ ] **Step 6: Commit**

```bash
git add src/imap_cleanup_tool/sortstore.py src/imap_cleanup_tool/triage.py tests/test_sortstore.py
git commit -m "feat(autosort): add sort store with rule sources and move log"
```

---

### Task 4: Layer chain

**Files:**
- Create: `src/imap_cleanup_tool/sortchain.py`
- Test: `tests/test_sortchain.py`

**Interfaces:**
- Consumes: `rulepacks.domain_category`, `rulepacks.protected_group`, `rulepacks.receipt_subject`, `signals.*`.
- Produces:
  - `sortchain.Decision(category: str, layer: str, reason: str)` (frozen dataclass)
  - `sortchain.Context(account: str, rules: dict[str, dict] = {}, gmail: dict[str, str] = {}, ai_min_confidence: float = 0.8)`
  - `sortchain.decide(row: dict, ctx: Context) -> Decision | None` — `None` means "no layer matched; try AI". `row` needs keys `uid`, `sender`, `subject`, `flags` (tuple of str), `signals` (frozenset from `signals.detect`).
  - `sortchain.INBOX_DEFAULT = Decision("inbox", "default", "No rule matched")`
  - `sortchain.PROTECTED_SUBJECT: re.Pattern`
  - Layer names: `flagged`, `protected`, `user`, `learned`, `sent`, `receipts`, `social`, `notifications`, `news`, `promotions`, `cc`, `ai`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sortchain.py`:

```python
"""Layer order of the auto-sort chain."""

import unittest

from imap_cleanup_tool import signals, sortchain

A = "me@example.com"


def row(sender="news@brand.test", subject="Hello", headers=None, flags=(), uid="1"):
    return {"uid": uid, "sender": sender, "subject": subject, "flags": flags,
            "signals": signals.detect(headers or {}, sender, A)}


def ctx(**kwargs):
    return sortchain.Context(account=A, **kwargs)


def rule(category, source, confidence=None):
    return {"news@brand.test": {"category": category, "source": source,
                                "confidence": confidence}}


class ChainTests(unittest.TestCase):
    def test_flagged_beats_everything(self):
        d = sortchain.decide(row(flags=("\\Flagged",)), ctx(rules=rule("news", "user")))
        self.assertEqual((d.category, d.layer), ("inbox", "flagged"))

    def test_protected_beats_user_rule(self):
        d = sortchain.decide(row(subject="Your verification code 1234"),
                             ctx(rules=rule("news", "user")))
        self.assertEqual((d.category, d.layer), ("inbox", "protected"))
        d = sortchain.decide(row(subject="Invitation: Sync @ Fri 10am"), ctx())
        self.assertEqual(d.layer, "protected")

    def test_user_and_learned_rules(self):
        self.assertEqual(sortchain.decide(row(), ctx(rules=rule("news", "user"))),
                         sortchain.Decision("news", "user", "Saved sender rule"))
        self.assertEqual(sortchain.decide(row(), ctx(rules=rule("inbox", "learned"))).layer,
                         "learned")

    def test_sent_trust_keeps_inbox(self):
        d = sortchain.decide(row(), ctx(rules=rule("inbox", "sent")))
        self.assertEqual((d.category, d.layer), ("inbox", "sent"))

    def test_receipts_by_subject(self):
        d = sortchain.decide(row("shop@brand.test", "Your order #123 has shipped"), ctx())
        self.assertEqual(d.category, "receipts")

    def test_social_by_domain(self):
        d = sortchain.decide(row("notifications@linkedin.com",
                                 "New connection request"), ctx())
        self.assertEqual(d.category, "social")

    def test_notifications_from_auto_submitted(self):
        d = sortchain.decide(row("alerts@status.test", "Build passed",
                                 {"Auto-Submitted": "auto-generated"}), ctx())
        self.assertEqual(d.category, "notifications")

    def test_notification_subject_ignored_for_list_mail(self):
        d = sortchain.decide(row("digest@lists.test", "Weekly digest",
                                 {"List-Id": "<l.test>", "List-Post": "<mailto:l@l.test>"}),
                             ctx())
        self.assertEqual(d.category, "news")

    def test_news_from_newsletter_platform(self):
        d = sortchain.decide(row("writer@substack.com", "Issue 42"), ctx())
        self.assertEqual(d.category, "news")

    def test_promotions_from_marketing_esp(self):
        d = sortchain.decide(row("hello@shop.test", "New arrivals",
                                 {"X-Kmail-Message": "1"}), ctx())
        self.assertEqual(d.category, "promotions")

    def test_weak_signals_alone_do_not_decide(self):
        self.assertIsNone(sortchain.decide(row("hello@shop.test", "Hello",
            {"List-Unsubscribe": "<https://u.test>"}), ctx()))
        self.assertIsNone(sortchain.decide(row("hello@shop.test", "Hello",
            {"List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}), ctx()))

    def test_cc_only(self):
        d = sortchain.decide(row("boss@corp.test", "Plan",
                                 {"To": "team@corp.test", "Cc": A}), ctx())
        self.assertEqual(d.category, "cc")

    def test_ai_rule_needs_confidence(self):
        self.assertEqual(sortchain.decide(row(), ctx(rules=rule("news", "ai", 0.9))).layer,
                         "ai")
        self.assertIsNone(sortchain.decide(row(), ctx(rules=rule("news", "ai", 0.5))))

    def test_gmail_category(self):
        d = sortchain.decide(row("hello@shop.test", "Hello"),
                             ctx(gmail={"1": "promotions"}))
        self.assertEqual(d.category, "promotions")

    def test_no_match_returns_none(self):
        self.assertIsNone(sortchain.decide(row("anna@friend.test", "Lunch?"), ctx()))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_sortchain -v`
Expected: FAIL with `ImportError: cannot import name 'sortchain'`.

- [ ] **Step 3: Write the implementation**

Create `src/imap_cleanup_tool/sortchain.py`:

```python
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
    if (pack_category == "receipts" or signals.RECEIPTS.search(subject)
            or rulepacks.receipt_subject(subject) or "fastmail_receipts" in sig):
        return Decision("receipts", "receipts",
                        pack_source if pack_category == "receipts"
                        else "Order, invoice or delivery subject")
    if pack_category == "social" or "fastmail_social" in sig or gmail == "social":
        return Decision("social", "social", pack_source or "Social category header")
    if ("auto_submitted" in sig or "fastmail_notifications" in sig
            or gmail == "notifications"
            or (not sig & signals.BULK
                and ("noreply" in sig or signals.NOTIFICATIONS.search(subject)))):
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m unittest tests.test_sortchain -v`
Expected: 15 tests, all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/imap_cleanup_tool/sortchain.py tests/test_sortchain.py
git commit -m "feat(autosort): add classification layer chain"
```

---

### Task 5: Triage folder helpers, `move_uid`, and the full folder set in the Inbox sort tab

**Files:**
- Modify: `src/imap_cleanup_tool/triage.py`
- Modify: `src/imap_cleanup_tool/web/static/index.html` (Inbox sort tab only, lines ~592-605 and ~2176-2245)
- Test: `tests/test_triage.py`

**Interfaces:**
- Consumes: `triage.LEGACY_CATEGORIES` (Task 3).
- Produces:
  - `triage.CATEGORY_FOLDERS: dict[str, str]` — `{"social": "Social", "news": "News", "promotions": "Promotions", "notifications": "Notifications", "receipts": "Receipts", "cc": "CC"}`
  - `triage.hierarchy_delimiter(conn) -> str`
  - `triage.folder_name(category: str, delimiter: str = ".") -> str` — raises `ValueError("Choose a sort folder.")`
  - `triage.folder_for(conn, category: str) -> str`
  - `triage.move_uid(conn, uid: str, destination: str) -> None` — moves one UID out of the selected folder
  - `triage.classify` now returns `"promotions"` where it returned `"other"`.

- [ ] **Step 1: Update and add triage tests**

In `tests/test_triage.py`:

1. Add `create` and `subscribe` to `TriageConn` (after `list`), and let the move test servers report the new folder:

```python
    def create(self, name):
        self.calls.append(("CREATE", name))
        return "OK", [b"created"]

    def subscribe(self, name):
        return "OK", [b""]
```

2. Replace `"3": "other"` with `"3": "promotions"` (line ~81), `("other", "inpector Free Time rule")` with `("promotions", "inpector Free Time rule")` (line ~148), `("other", "inpector Mailinglists rule")` with `("promotions", "inpector Mailinglists rule")` (line ~152), and `"Choose Social or Other"` with `"Choose a sort folder"` (line ~262).

3. Add these tests to `TriageTests`:

```python
    def test_folder_names_follow_server_delimiter(self):
        conn = TriageConn()
        self.assertEqual(triage.hierarchy_delimiter(conn), ".")
        conn.list = lambda *a: ("OK", [b'(\\HasNoChildren) "/" "INBOX"'])
        self.assertEqual(triage.folder_for(conn, "receipts"), "INBOX/Receipts")
        conn.list = lambda *a: ("OK", [b'(\\Noselect) NIL ""'])
        self.assertEqual(triage.hierarchy_delimiter(conn), ".")
        with self.assertRaisesRegex(ValueError, "Choose a sort folder"):
            triage.folder_name("trash")

    def test_legacy_other_moves_to_promotions_and_creates_folder(self):
        conn = TriageConn()
        row = next(r for r in triage.preview(conn, "me@example.com")["rows"]
                   if r["uid"] == "3")
        folder = triage.move_checked(conn, uid="3", uidvalidity="42",
                                     sender=row["sender"], subject=row["subject"],
                                     date=row["date"], category="other")
        self.assertEqual(folder, "INBOX.Promotions")
        self.assertIn(("CREATE", '"INBOX.Promotions"'), conn.calls)
        self.assertNotIn("3", conn.messages)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_triage -v`
Expected: FAIL — `AttributeError: module 'imap_cleanup_tool.triage' has no attribute 'hierarchy_delimiter'` and the renamed-category assertions fail.

- [ ] **Step 3: Implement the triage changes**

In `src/imap_cleanup_tool/triage.py`:

1. Replace `DESTINATIONS = {"social": "INBOX.Social", "other": "INBOX.Other"}` with:

```python
CATEGORY_FOLDERS = {"social": "Social", "news": "News", "promotions": "Promotions",
                    "notifications": "Notifications", "receipts": "Receipts",
                    "cc": "CC"}
```

2. In `classify`, change every `return "other", ...` to `return "promotions", ...` (4 places: service alert, promotion subject, Free Time, Mailinglists).

3. Add after `forget_sender`:

```python
_INBOX_LIST_LINE = re.compile(r'^\([^)]*\)\s+"(.)"\s+"?INBOX"?\s*$', re.IGNORECASE)


def hierarchy_delimiter(conn) -> str:
    """The server's folder separator from the LIST line for INBOX; "." if absent."""
    status, data = conn.list()
    if status == "OK":
        for item in data or []:
            line = item.decode(errors="replace") if isinstance(item, bytes) else str(item or "")
            match = _INBOX_LIST_LINE.match(line.strip())
            if match:
                return match.group(1)
    return "."


def folder_name(category: str, delimiter: str = ".") -> str:
    category = LEGACY_CATEGORIES.get(category, category)
    if category not in CATEGORY_FOLDERS:
        raise ValueError("Choose a sort folder.")
    return f"INBOX{delimiter}{CATEGORY_FOLDERS[category]}"


def folder_for(conn, category: str) -> str:
    return folder_name(category, hierarchy_delimiter(conn))


def _capabilities(conn) -> set[str]:
    return {c.decode().upper() if isinstance(c, bytes) else str(c).upper()
            for c in getattr(conn, "capabilities", ())}


def move_uid(conn, uid: str, destination: str) -> None:
    """Move one UID out of the selected folder: MOVE, else COPY + STORE + UID EXPUNGE."""
    capabilities = _capabilities(conn)
    target = core._quote_mailbox(destination)
    if "MOVE" in capabilities:
        status, _ = conn.uid("MOVE", uid.encode(), target)
        if status != "OK":
            raise ValueError("The server did not move the message.")
        return
    if "UIDPLUS" not in capabilities:
        raise ValueError("The server needs MOVE or UIDPLUS for a safe move.")
    status, _ = conn.uid("COPY", uid.encode(), target)
    if status != "OK":
        raise ValueError("The server did not copy the message.")
    status, _ = conn.uid("STORE", uid.encode(), "+FLAGS", r"(\Deleted)")
    if status != "OK":
        raise ValueError("The copy succeeded, but the source remains in place.")
    status, _ = conn.uid("EXPUNGE", uid.encode())
    if status != "OK":
        raise ValueError("The copy succeeded, but the source remains in place.")
```

4. Replace `move_checked` with:

```python
def move_checked(conn, *, uid: str, uidvalidity: str, sender: str,
                 subject: str, date: str, category: str) -> str:
    """Move one still-matching message to a sort folder. Never use Trash."""
    destination = folder_for(conn, category)
    if not uid.isascii() or not uid.isdecimal() or int(uid) <= 0:
        raise ValueError("Invalid message UID.")
    capabilities = _capabilities(conn)
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
    if destination not in core.list_folders(conn):
        core.create_folder(conn, destination)
    move_uid(conn, uid, destination)
    return destination
```

Note: `test_uidplus_fallback_expunges_only_the_chosen_uid` expects the UIDPLUS error messages to still mention the source; the new text "remains in place" is only reached on failure paths that test does not hit. Run the suite to confirm.

- [ ] **Step 4: Update the Inbox sort tab UI**

In `src/imap_cleanup_tool/web/static/index.html`:

1. Line ~592: change `Move one message to Social or Other.` to `Move one message to a sort folder.`
2. Line ~599: change `<span class="text-muted">Other suggestions</span>` to `<span class="text-muted">Other folder suggestions</span>`.
3. Line ~605: replace `<option value="other">Other</option>` with:

```html
<option value="news">News</option><option value="promotions">Promotions</option><option value="notifications">Notifications</option><option value="receipts">Receipts</option><option value="cc">CC</option>
```

4. Above `let triagePreview=null;` add:

```javascript
const SORT_FOLDERS={social:'Social',news:'News',promotions:'Promotions',notifications:'Notifications',receipts:'Receipts',cc:'CC'};
```

5. In `renderTriage`, replace the `counts` lines with:

```javascript
  const counts={social:0,inbox:0};
  rows.forEach(r=>{ counts[r.category]=(counts[r.category]||0)+1; });
  const otherCount=rows.filter(r=>r.category!=='inbox'&&r.category!=='social').length;
```

and `$('triageOther').textContent=triagePreview?counts.other:'—';` with `$('triageOther').textContent=triagePreview?otherCount:'—';`.

6. In the row template, replace `escapeHtml(r.category==='inbox'?'Inbox':r.category==='social'?'Social':'Other')` with `escapeHtml(r.category==='inbox'?'Inbox':(SORT_FOLDERS[r.category]||r.category))`, and replace the `<select class="field text-xs triageDest">…</select>` string with:

```javascript
'<select class="field text-xs triageDest"><option value="">Choose folder</option>'+Object.entries(SORT_FOLDERS).map(([k,v])=>'<option value="'+k+'" '+(r.category===k?'selected':'')+'>'+v+'</option>').join('')+'</select>'
```

7. In `moveTriageRow`, replace the first three lines of the body and the toast with:

```javascript
  const category=tr.querySelector('.triageDest').value;
  if(!category){ toastErr('Choose a folder first.'); return; }
  const label=SORT_FOLDERS[category];
  if(!(await confirmDialog({title:'Move this message?',message:'Move only “'+row.subject+'” from '+row.sender+' to '+label+'?',okText:'Move to '+label}))) return;
  try{
    const train=tr.querySelector('.triageTrain').checked;
    const d=await api('/api/triage/move',{sid:SID,uid:row.uid,uidvalidity:triagePreview.uidvalidity,
      sender:row.sender,subject:row.subject,date:row.date,category,train});
    toast('Moved to '+d.folder+'.','border-ok/50 bg-ok/15 text-ok');
```

(keep the following `await loadTriage(); }catch(e){ toastErr(e.message); }` lines).

- [ ] **Step 5: Run the triage and web tests**

Run: `.venv/bin/python -m unittest tests.test_triage tests.test_webapp -v`
Expected: all PASS (web tests are skipped if the web extra is missing; the venv has it).

- [ ] **Step 6: Commit**

```bash
git add src/imap_cleanup_tool/triage.py src/imap_cleanup_tool/web/static/index.html tests/test_triage.py
git commit -m "feat(triage): full sort folder set, server delimiter, reusable move_uid"
```

---

### Task 6: AI fallback per sender

**Files:**
- Create: `src/imap_cleanup_tool/ai_sort.py`
- Test: `tests/test_ai_sort.py`

**Interfaces:**
- Consumes: `ai._call_once(litellm, kwargs)`, `ai._extract_json(content)`, `ai._batch_cost(cfg, pt, ct)`, `ai.LLM_TIMEOUT`; `llm.load_model(name)`, `llm.LLMError`, `llm.log_cost(name, pt, ct, cost)`; `rulepacks.CATEGORIES`.
- Produces:
  - `ai_sort.Skip(Exception)` — the AI layer cannot run; `str(exc)` says why
  - `ai_sort.load_model(name: str) -> dict`
  - `ai_sort.validate(content: str) -> dict` — `{"category", "confidence", "reason"}`; raises `ValueError`
  - `ai_sort.classify_senders(groups: dict[str, list[dict]], cfg: dict, *, max_calls: int, litellm=None) -> tuple[dict[str, dict], list[str]]` — `groups` maps sender to rows with `subject` and `signals`; returns `(verdicts, errors)`; senders are processed in sorted order and only the first `max_calls` are sent

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ai_sort.py`:

```python
"""AI fallback: strict verdicts, budget, cost logging, model checks."""

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from imap_cleanup_tool import ai_sort, llm

CFG = {"name": "local", "model": "ollama/llama3", "api_base": "", "api_key": "",
       "encrypted": False, "track_costs": True, "cost_input": 1.0, "cost_output": 2.0}


class FakeLiteLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def completion(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.replies.pop(0)))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))


def groups():
    return {"b@y.test": [{"subject": "Hi", "signals": frozenset()}],
            "a@x.test": [{"subject": "Issue 1", "signals": frozenset({"list_id"})}]}


class ValidateTests(unittest.TestCase):
    def test_accepts_wrapped_json(self):
        self.assertEqual(ai_sort.validate(
            'Sure: {"category": "News", "confidence": 0.9, "reason": "list"}'),
            {"category": "news", "confidence": 0.9, "reason": "list"})

    def test_rejects_bad_answers(self):
        for content in ("no json", '{"category": "trash", "confidence": 0.9}',
                        '{"category": "news", "confidence": 1.4}',
                        '{"category": "news", "confidence": "high"}'):
            with self.assertRaises(ValueError, msg=content):
                ai_sort.validate(content)


class ClassifyTests(unittest.TestCase):
    def test_one_call_per_sender_and_cost_logged(self):
        fake = FakeLiteLLM(['{"category":"news","confidence":0.9,"reason":"list"}',
                            '{"category":"inbox","confidence":0.7,"reason":"person"}'])
        with mock.patch.object(llm, "log_cost") as log_cost:
            verdicts, errors = ai_sort.classify_senders(groups(), CFG, max_calls=50,
                                                        litellm=fake)
        self.assertEqual(errors, [])
        self.assertEqual(verdicts["a@x.test"]["category"], "news")
        self.assertEqual(verdicts["b@y.test"]["category"], "inbox")
        self.assertEqual(len(fake.calls), 2)
        payload = json.loads(fake.calls[0]["messages"][1]["content"])
        self.assertEqual(payload, {"sender": "a@x.test", "messages": [
            {"subject": "Issue 1", "flags": ["list_id"]}]})
        log_cost.assert_called_with("local", 10, 5, 2e-05)

    def test_budget_limits_calls(self):
        fake = FakeLiteLLM(['{"category":"news","confidence":0.9,"reason":"x"}'])
        with mock.patch.object(llm, "log_cost"):
            verdicts, _ = ai_sort.classify_senders(groups(), CFG, max_calls=1,
                                                   litellm=fake)
        self.assertEqual(list(verdicts), ["a@x.test"])

    def test_bad_reply_is_an_error_not_a_verdict(self):
        fake = FakeLiteLLM(["nonsense", '{"category":"cc","confidence":0.8,"reason":"x"}'])
        with mock.patch.object(llm, "log_cost"):
            verdicts, errors = ai_sort.classify_senders(groups(), CFG, max_calls=5,
                                                        litellm=fake)
        self.assertEqual(list(verdicts), ["b@y.test"])
        self.assertTrue(errors[0].startswith("a@x.test:"))


class LoadModelTests(unittest.TestCase):
    def test_skips(self):
        with self.assertRaisesRegex(ai_sort.Skip, "No AI model"):
            ai_sort.load_model("")
        with mock.patch.object(llm, "load_model", return_value={**CFG, "encrypted": True}):
            with self.assertRaisesRegex(ai_sort.Skip, "Encrypted"):
                ai_sort.load_model("local")
        with mock.patch.object(llm, "load_model", side_effect=llm.LLMError("No model config")):
            with self.assertRaisesRegex(ai_sort.Skip, "No model config"):
                ai_sort.load_model("gone")
        with mock.patch.object(llm, "load_model", return_value=CFG):
            self.assertEqual(ai_sort.load_model("local"), CFG)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_ai_sort -v`
Expected: FAIL with `ImportError: cannot import name 'ai_sort'`.

- [ ] **Step 3: Write the implementation**

Create `src/imap_cleanup_tool/ai_sort.py`:

```python
"""AI fallback for auto-sort: one LLM call per unknown sender, strict JSON.

Only sender, subjects and header flags are sent. Message bodies never leave
the mailbox.
"""

from __future__ import annotations

import json

from . import ai, llm
from .rulepacks import CATEGORIES

ALLOWED = frozenset((*CATEGORIES, "inbox"))
SAMPLE_SIZE = 5
SYSTEM_PROMPT = (
    "You sort email for a busy person. For one sender you get up to five recent "
    "subjects and header flags. Pick exactly one category: inbox (a person wrote "
    "it, or it needs action or attention), social (social network activity), "
    "news (newsletters, mailing lists), promotions (marketing, sales), "
    "notifications (automated app or service notices), receipts (orders, "
    "invoices, shipping), cc (the person is only copied). When unsure, pick "
    "inbox with low confidence. Reply with JSON only: "
    '{"category":"...","confidence":0.0,"reason":"..."}')


class Skip(Exception):
    """The AI layer cannot run; the message says why."""


def load_model(name: str) -> dict:
    if not name:
        raise Skip("No AI model is set for auto-sort.")
    try:
        cfg = llm.load_model(name)
    except llm.LLMError as exc:
        raise Skip(str(exc)) from exc
    if cfg.get("encrypted"):
        raise Skip("Encrypted model configs can't run unattended.")
    return cfg


def _payload(sender: str, rows: list[dict]) -> str:
    return json.dumps({"sender": sender, "messages": [
        {"subject": r["subject"], "flags": sorted(r["signals"])}
        for r in rows[:SAMPLE_SIZE]]}, ensure_ascii=False)


def validate(content: str) -> dict:
    raw = ai._extract_json(content)
    if not isinstance(raw, dict):
        raise ValueError("reply is not a JSON object")
    category = str(raw.get("category", "")).strip().lower()
    if category not in ALLOWED:
        raise ValueError(f"unknown category {category!r}")
    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise ValueError("confidence is not a number") from exc
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence is out of range")
    return {"category": category, "confidence": confidence,
            "reason": str(raw.get("reason", ""))[:200]}


def classify_senders(groups: dict[str, list[dict]], cfg: dict, *, max_calls: int,
                     litellm=None) -> tuple[dict[str, dict], list[str]]:
    """Ask the model about each sender, up to ``max_calls`` calls."""
    if litellm is None:
        try:
            import litellm  # pylint: disable=import-outside-toplevel,redefined-outer-name
        except ImportError as exc:
            raise Skip("Install the [ai] extra to use the AI layer.") from exc
    base = {"model": cfg["model"], "timeout": ai.LLM_TIMEOUT}
    if cfg.get("api_key"):
        base["api_key"] = cfg["api_key"]
    if cfg.get("api_base"):
        base["api_base"] = cfg["api_base"]
    verdicts: dict[str, dict] = {}
    errors: list[str] = []
    for sender in sorted(groups)[:max(0, int(max_calls))]:
        kwargs = dict(base, messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _payload(sender, groups[sender])}])
        try:
            resp = ai._call_once(litellm, kwargs)
            usage = getattr(resp, "usage", None)
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            cost = ai._batch_cost(cfg, prompt_tokens, completion_tokens)
            if cost is not None:
                llm.log_cost(cfg["name"], prompt_tokens, completion_tokens, cost)
            verdicts[sender] = validate(resp.choices[0].message.content)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            errors.append(f"{sender}: {exc}")
    return verdicts, errors
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m unittest tests.test_ai_sort -v`
Expected: 6 tests, all PASS. If `LLM_TIMEOUT` is not defined in `ai.py`, run `grep -n "LLM_TIMEOUT" src/imap_cleanup_tool/ai.py` and use the constant it defines.

- [ ] **Step 5: Commit**

```bash
git add src/imap_cleanup_tool/ai_sort.py tests/test_ai_sort.py
git commit -m "feat(autosort): add per-sender AI fallback"
```

---

### Task 7: Fake mailbox, learning, Sent trust and undo

**Files:**
- Create: `tests/fake_imap.py`
- Create: `src/imap_cleanup_tool/autosort.py` (first part)
- Test: `tests/test_autosort.py` (first part)

**Interfaces:**
- Consumes: `sortstore.*` (Task 3), `triage.CATEGORY_FOLDERS`, `triage.folder_name`, `triage.hierarchy_delimiter`, `triage.move_uid` (Task 5), `core.special_folder`, `core.list_folders`, `core._quote_mailbox`, `core._read_uidvalidity`, `core._message_id`, `scheduler.config_dir`, `scheduler.account_slug`.
- Produces:
  - `autosort.LEARN_DAYS = 30`, `autosort.SENT_FIRST_DAYS = 365`
  - `autosort.learn(conn, account: str, *, now: datetime) -> int` — number of sender rules changed
  - `autosort.trust_sent(conn, account: str, *, now: datetime) -> int` — number of new trusted senders
  - `autosort.undo_move(conn, account: str, move_id: int) -> str` — returns `"INBOX"`
  - `autosort.undo_run(conn, account: str, run_id: str) -> dict` — `{"undone": int, "errors": list[str]}`
  - `tests.fake_imap.FakeMailbox(folders=("INBOX",), *, capabilities=("IMAP4REV1", "MOVE"), delimiter=".", sent="Sent")` with `add(folder, *, sender, subject, message_id="", to="me@example.com", cc="", extra="", flags=(), received=None) -> str`, fields `folders`, `validity`, `gmail`, `calls`, `readonly`

- [ ] **Step 1: Write the fake mailbox**

Create `tests/fake_imap.py`:

```python
"""A small multi-folder IMAP fake for auto-sort tests."""

from __future__ import annotations

import imaplib
from datetime import datetime, timezone

DEFAULT_RECEIVED = datetime(2026, 9, 24, 10, tzinfo=timezone.utc)


class FakeMailbox:
    def __init__(self, folders=("INBOX",), *, capabilities=("IMAP4REV1", "MOVE"),
                 delimiter=".", sent="Sent"):
        self.capabilities = capabilities
        self.delimiter = delimiter
        self.sent = sent
        self.folders = {name: {} for name in folders}
        if sent:
            self.folders.setdefault(sent, {})
        self.validity = {name: "7" for name in self.folders}
        self.next_uid = {name: 1 for name in self.folders}
        self.selected = None
        self.readonly = True
        self.calls = []
        self.gmail = {}   # Gmail category name -> set of INBOX UIDs

    def add(self, folder, *, sender, subject, message_id="", to="me@example.com",
            cc="", extra="", flags=(), received=None) -> str:
        uid = str(self.next_uid[folder])
        self.next_uid[folder] += 1
        header = (f"From: <{sender}>\r\nTo: {to}\r\nSubject: {subject}\r\n"
                  "Date: Thu, 24 Sep 2026 10:00:00 +0000\r\n")
        if cc:
            header += f"Cc: {cc}\r\n"
        if message_id:
            header += f"Message-ID: {message_id}\r\n"
        header += extra
        self.folders[folder][uid] = {"header": header + "\r\n", "flags": tuple(flags),
                                     "received": received or DEFAULT_RECEIVED}
        return uid

    def list(self, *args):
        lines = []
        for name in self.folders:
            attrs = "\\HasNoChildren" + (" \\Sent" if name == self.sent else "")
            lines.append(f'({attrs}) "{self.delimiter}" "{name}"'.encode())
        return "OK", lines

    def create(self, name):
        name = name.strip('"')
        self.calls.append(("CREATE", name))
        self.folders.setdefault(name, {})
        self.validity.setdefault(name, "7")
        self.next_uid.setdefault(name, 1)
        return "OK", [b"created"]

    def subscribe(self, name):
        return "OK", [b""]

    def select(self, name, readonly=False):
        name = name.strip('"')
        if name not in self.folders:
            return "NO", [b"no such folder"]
        self.selected, self.readonly = name, readonly
        return "OK", [str(len(self.folders[name])).encode()]

    def response(self, code):
        return code, [self.validity[self.selected].encode()]

    def uid(self, command, *args):
        self.calls.append((command, self.selected, *args))
        box = self.folders[self.selected]
        if command == "SEARCH":
            return "OK", [" ".join(self._search(box, args)).encode()]
        if command == "FETCH":
            parts = []
            for raw in args[0].split(b","):
                msg = box.get(raw.decode())
                if msg is None:
                    continue
                header = msg["header"].encode()
                internal = imaplib.Time2Internaldate(msg["received"].timestamp())
                meta = (f"1 (UID {raw.decode()} FLAGS ({' '.join(msg['flags'])}) "
                        f"INTERNALDATE {internal} BODY[HEADER] {{{len(header)}}}").encode()
                parts.append((meta, header))
                parts.append(b")")
            return "OK", parts
        if command == "MOVE":
            if self.readonly:
                raise AssertionError("MOVE on a read-only folder")
            dest = args[1].strip('"')
            msg = box.pop(args[0].decode())
            new_uid = str(self.next_uid[dest])
            self.next_uid[dest] += 1
            self.folders[dest][new_uid] = msg
            return "OK", [b"moved"]
        raise AssertionError(command)

    def _search(self, box, args):
        uids = sorted(box, key=int)
        args = [a for a in args if a is not None]
        if args == ["ALL"]:
            return uids
        if args[0] == "UID":
            low = int(args[1].split(":")[0])
            # Like real servers, "n:*" returns the last message when none is >= n.
            return [u for u in uids if int(u) >= low] or uids[-1:]
        if args[0] == "SINCE":
            day = datetime.strptime(args[1], "%d-%b-%Y").replace(tzinfo=timezone.utc)
            return [u for u in uids if box[u]["received"] >= day]
        if args[:2] == ["HEADER", "Message-ID"]:
            want = args[2].strip('"')
            return [u for u in uids if f"Message-ID: {want}\r\n" in box[u]["header"]]
        if args[0] == "X-GM-RAW":
            category = args[1].strip('"').split(":", 1)[1]
            return sorted(self.gmail.get(category, set()) & set(uids), key=int)
        raise AssertionError(args)
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_autosort.py`:

```python
"""Auto-sort runs against a multi-folder IMAP fake."""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from imap_cleanup_tool import autosort, scheduler, sortstore, triage
from tests.fake_imap import FakeMailbox

A = "me@example.com"
NOW = datetime(2026, 9, 25, tzinfo=timezone.utc)


class AutosortTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for target, name in ((triage, "rules_path"), (scheduler, "config_dir")):
            value = (Path(self.temp.name) / "rules.sqlite" if name == "rules_path"
                     else Path(self.temp.name))
            patch = mock.patch.object(target, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)

    def log(self, target, message_id, sender="n@brand.test", run="run1"):
        return sortstore.log_move(A, run, message_id=message_id, uid="1",
                                  sender=sender, subject="s", source_folder="INBOX",
                                  target_folder=target, layer="news", reason="r")


class LearnTests(AutosortTestCase):
    def test_move_back_to_inbox_trains_inbox(self):
        box = FakeMailbox(("INBOX", "INBOX.News", "INBOX.Promotions"))
        box.add("INBOX", sender="n@brand.test", subject="s", message_id="<m1@x>")
        self.log("INBOX.News", "<m1@x>")
        self.assertEqual(autosort.learn(box, A, now=NOW), 1)
        self.assertEqual(sortstore.rules(A)["n@brand.test"],
                         {"category": "inbox", "source": "learned", "confidence": None})
        self.assertEqual(autosort.learn(box, A, now=NOW), 0)

    def test_move_to_another_sort_folder_trains_that_folder(self):
        box = FakeMailbox(("INBOX", "INBOX.News", "INBOX.Promotions"))
        box.add("INBOX.Promotions", sender="n@brand.test", subject="s",
                message_id="<m2@x>")
        self.log("INBOX.News", "<m2@x>")
        self.assertEqual(autosort.learn(box, A, now=NOW), 1)
        self.assertEqual(sortstore.rules(A)["n@brand.test"]["category"], "promotions")

    def test_unmoved_or_missing_changes_nothing(self):
        box = FakeMailbox(("INBOX", "INBOX.News"))
        box.add("INBOX.News", sender="n@brand.test", subject="s", message_id="<m3@x>")
        self.log("INBOX.News", "<m3@x>")
        self.log("INBOX.News", "<gone@x>", sender="g@brand.test")
        self.assertEqual(autosort.learn(box, A, now=NOW), 0)
        self.assertEqual(sortstore.rules(A), {})


class TrustSentTests(AutosortTestCase):
    def test_first_run_trusts_recipients_but_not_me(self):
        box = FakeMailbox()
        box.add("Sent", sender=A, subject="Hi", to="Friend <friend@x.test>",
                cc=f"{A}, other@y.test")
        self.assertEqual(autosort.trust_sent(box, A, now=NOW), 2)
        self.assertEqual(sortstore.rules(A)["friend@x.test"],
                         {"category": "inbox", "source": "sent", "confidence": None})
        self.assertNotIn(A, sortstore.rules(A))

    def test_user_rule_wins_and_runs_are_incremental(self):
        box = FakeMailbox()
        sortstore.save_rule(A, "friend@x.test", "news", "user")
        box.add("Sent", sender=A, subject="Hi", to="friend@x.test, other@y.test")
        self.assertEqual(autosort.trust_sent(box, A, now=NOW), 1)
        self.assertEqual(sortstore.rules(A)["friend@x.test"]["source"], "user")
        self.assertEqual(autosort.trust_sent(box, A, now=NOW), 0)
        box.add("Sent", sender=A, subject="Hi", to="new@z.test")
        self.assertEqual(autosort.trust_sent(box, A, now=NOW), 1)

    def test_no_sent_folder(self):
        self.assertEqual(autosort.trust_sent(FakeMailbox(sent=None), A, now=NOW), 0)


class UndoTests(AutosortTestCase):
    def test_undo_moves_back_and_trains_inbox(self):
        box = FakeMailbox(("INBOX", "INBOX.News"))
        box.add("INBOX.News", sender="n@brand.test", subject="s", message_id="<u1@x>")
        move_id = self.log("INBOX.News", "<u1@x>")
        self.assertEqual(autosort.undo_move(box, A, move_id), "INBOX")
        self.assertEqual(len(box.folders["INBOX"]), 1)
        self.assertEqual(box.folders["INBOX.News"], {})
        self.assertIsNotNone(sortstore.get_move(A, move_id)["undone_at"])
        self.assertEqual(sortstore.rules(A)["n@brand.test"]["source"], "user")
        with self.assertRaisesRegex(ValueError, "already undone"):
            autosort.undo_move(box, A, move_id)

    def test_undo_errors(self):
        box = FakeMailbox(("INBOX", "INBOX.News"))
        with self.assertRaisesRegex(ValueError, "no longer in"):
            autosort.undo_move(box, A, self.log("INBOX.News", "<missing@x>"))
        with self.assertRaisesRegex(ValueError, "no Message-ID"):
            autosort.undo_move(box, A, self.log("INBOX.News", ""))
        with self.assertRaisesRegex(ValueError, "Unknown move"):
            autosort.undo_move(box, A, 999)

    def test_undo_run(self):
        box = FakeMailbox(("INBOX", "INBOX.News"))
        for mid in ("<r1@x>", "<r2@x>"):
            box.add("INBOX.News", sender="n@brand.test", subject="s", message_id=mid)
            self.log("INBOX.News", mid, run="runX")
        self.assertEqual(autosort.undo_run(box, A, "runX"), {"undone": 2, "errors": []})
        self.assertEqual(len(box.folders["INBOX"]), 2)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_autosort -v`
Expected: FAIL with `ImportError: cannot import name 'autosort'`.

- [ ] **Step 4: Write the first part of `autosort.py`**

Create `src/imap_cleanup_tool/autosort.py`:

```python
"""One auto-sort pass: learn from moves, trust Sent, classify new INBOX mail, move.

Never deletes and never uses Trash. Design:
docs/superpowers/specs/2026-09-25-autosort-core-loop-design.md
"""

from __future__ import annotations

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
        if rule and rule["category"] == category and rule["source"] == "learned":
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
        except ValueError as exc:
            errors.append(f"{move['subject']}: {exc}")
    return {"undone": undone, "errors": errors}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m unittest tests.test_autosort -v`
Expected: 9 tests, all PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/fake_imap.py tests/test_autosort.py src/imap_cleanup_tool/autosort.py
git commit -m "feat(autosort): learn from folder moves, trust Sent, undo moves"
```

---

### Task 8: The auto-sort run

**Files:**
- Modify: `src/imap_cleanup_tool/autosort.py` (append)
- Test: `tests/test_autosort.py` (append `RunTests`)

**Interfaces:**
- Consumes: Tasks 2-7; `ai_sort.load_model`, `ai_sort.classify_senders`, `ai_sort.Skip`; `sortchain.decide`, `sortchain.Context`, `sortchain.Decision`, `sortchain.INBOX_DEFAULT`; `signals.detect`; `core._uid_from_meta`, `core.decode_mime_header`, `core.extract_sender_email`, `core.create_folder`, `core.AI_FETCH_CHUNK`; `scheduler.config_dir`, `scheduler.account_slug`.
- Produces:
  - `autosort.Busy(RuntimeError)`
  - `autosort.account_lock(account: str)` — context manager
  - `autosort.RunResult` dataclass: `run_id: str`, `dry_run: bool`, `planned: list[dict]` (keys `uid`, `message_id`, `sender`, `subject`, `category`, `folder`, `layer`, `reason`), `moved: int`, `skipped: list[str]`, `learned: int`, `trusted: int`, `ai_note: str`; method `to_dict() -> dict`
  - `autosort.run(conn, account: str, *, dry_run=False, backlog=False, ai_model: str | None = None, litellm=None, now: datetime | None = None) -> RunResult`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_autosort.py` (above `if __name__ == "__main__":`), and add `import imaplib` and `from imap_cleanup_tool import ai_sort, core, llm` to the imports:

```python
NEWS = "List-Id: <l.test>\r\nList-Post: <mailto:l@l.test>\r\n"
CFG = {"name": "local", "model": "ollama/llama3", "api_base": "", "api_key": "",
       "encrypted": False, "track_costs": False, "cost_input": 0, "cost_output": 0}


class FakeLiteLLM:
    def __init__(self, replies):
        self.replies = list(replies)

    def completion(self, **kwargs):
        from types import SimpleNamespace
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.replies.pop(0)))],
            usage=None)


class RunTests(AutosortTestCase):
    def setUp(self):
        super().setUp()
        sortstore.update_settings(A, started_at="2026-09-01T00:00:00+00:00")

    def inbox_subjects(self, box):
        return sorted(m["header"].split("Subject: ")[1].split("\r\n")[0]
                      for m in box.folders["INBOX"].values())

    def test_sorts_new_mail_and_logs(self):
        box = FakeMailbox()
        box.add("INBOX", sender="writer@substack.com", subject="Issue 42", message_id="<n1@x>")
        box.add("INBOX", sender="friend@x.test", subject="Lunch?", message_id="<p1@x>")
        box.add("INBOX", sender="alerts@status.test", subject="Build passed",
                message_id="<a1@x>", extra="Auto-Submitted: auto-generated\r\n")
        result = autosort.run(box, A, now=NOW)
        self.assertEqual(result.moved, 2)
        self.assertEqual(self.inbox_subjects(box), ["Lunch?"])
        self.assertEqual(len(box.folders["INBOX.News"]), 1)
        self.assertEqual(len(box.folders["INBOX.Notifications"]), 1)
        self.assertEqual({m["target_folder"] for m in sortstore.moves(A)},
                         {"INBOX.News", "INBOX.Notifications"})
        self.assertEqual(sortstore.get_state(A, "INBOX")["last_uid"], 3)
        self.assertIn("No AI model", result.ai_note)

    def test_dry_run_moves_nothing(self):
        box = FakeMailbox()
        box.add("INBOX", sender="writer@substack.com", subject="Issue 42", message_id="<n1@x>")
        result = autosort.run(box, A, dry_run=True, now=NOW)
        self.assertEqual(result.moved, 0)
        self.assertEqual([p["folder"] for p in result.planned], ["INBOX.News"])
        self.assertEqual(len(box.folders["INBOX"]), 1)
        self.assertEqual(sortstore.moves(A), [])
        self.assertIsNone(sortstore.get_state(A, "INBOX"))
        self.assertFalse(any(c[0] == "MOVE" for c in box.calls))

    def test_second_run_reads_only_new_uids(self):
        box = FakeMailbox()
        box.add("INBOX", sender="friend@x.test", subject="Lunch?", message_id="<p1@x>")
        autosort.run(box, A, now=NOW)
        self.assertEqual(autosort.run(box, A, now=NOW).planned, [])
        box.add("INBOX", sender="writer@substack.com", subject="Issue 43", message_id="<n2@x>")
        self.assertEqual(autosort.run(box, A, now=NOW).moved, 1)

    def test_cutoff_skips_old_mail_unless_backlog(self):
        box = FakeMailbox()
        box.add("INBOX", sender="writer@substack.com", subject="Old issue",
                message_id="<o1@x>", received=datetime(2026, 8, 1, tzinfo=timezone.utc))
        self.assertEqual(autosort.run(box, A, now=NOW).moved, 0)
        self.assertEqual(autosort.run(box, A, backlog=True, now=NOW).moved, 1)

    def test_flagged_and_protected_mail_stays(self):
        box = FakeMailbox()
        box.add("INBOX", sender="writer@substack.com", subject="Issue 1",
                message_id="<f1@x>", flags=("\\Flagged",))
        box.add("INBOX", sender="writer@substack.com", subject="Your verification code",
                message_id="<f2@x>")
        self.assertEqual(autosort.run(box, A, now=NOW).moved, 0)

    def test_uidvalidity_change_skips_moves(self):
        box = FakeMailbox()
        box.add("INBOX", sender="friend@x.test", subject="Lunch?", message_id="<p1@x>")
        autosort.run(box, A, now=NOW)
        box.add("INBOX", sender="writer@substack.com", subject="Issue 2", message_id="<n3@x>")
        box.validity["INBOX"] = "8"
        result = autosort.run(box, A, now=NOW)
        self.assertEqual(result.moved, 0)
        self.assertIn("UIDVALIDITY", result.skipped[0])
        self.assertEqual(sortstore.get_state(A, "INBOX")["uidvalidity"], "8")

    def test_message_moved_once_is_never_moved_again(self):
        box = FakeMailbox()
        box.add("INBOX", sender="writer@substack.com", subject="Issue 5", message_id="<n5@x>")
        self.log("INBOX.News", "<n5@x>", sender="writer@substack.com")
        with mock.patch.object(autosort, "learn", return_value=0):
            self.assertEqual(autosort.run(box, A, now=NOW).moved, 0)

    def test_lock_blocks_a_second_run(self):
        with autosort.account_lock(A):
            with self.assertRaises(autosort.Busy):
                autosort.run(FakeMailbox(), A, now=NOW)

    def test_ai_moves_confident_and_queues_unsure(self):
        sortstore.update_settings(A, ai_model="local")
        box = FakeMailbox()
        box.add("INBOX", sender="hello@shop.test", subject="Hello", message_id="<h1@x>")
        box.add("INBOX", sender="maybe@odd.test", subject="Hi", message_id="<m1@x>")
        fake = FakeLiteLLM(['{"category":"promotions","confidence":0.9,"reason":"shop"}',
                            '{"category":"news","confidence":0.4,"reason":"unsure"}'])
        with mock.patch.object(ai_sort, "load_model", return_value=CFG):
            result = autosort.run(box, A, now=NOW, litellm=fake)
        self.assertEqual(result.moved, 1)
        self.assertEqual(len(box.folders["INBOX.Promotions"]), 1)
        self.assertEqual(sortstore.rules(A)["hello@shop.test"]["source"], "ai")
        self.assertEqual([r["sender"] for r in sortstore.reviews(A)], ["maybe@odd.test"])

    def test_ai_budget_leaves_rest_for_next_run(self):
        sortstore.update_settings(A, ai_model="local", ai_max_calls=1)
        box = FakeMailbox()
        box.add("INBOX", sender="a@one.test", subject="Hello", message_id="<b1@x>")
        box.add("INBOX", sender="b@two.test", subject="Hello", message_id="<b2@x>")
        fake = FakeLiteLLM(['{"category":"inbox","confidence":0.9,"reason":"person"}'])
        with mock.patch.object(ai_sort, "load_model", return_value=CFG):
            result = autosort.run(box, A, now=NOW, litellm=fake)
        self.assertIn("AI budget", " ".join(result.skipped))
        self.assertEqual(sortstore.get_state(A, "INBOX")["last_uid"], 1)

    def test_gmail_category_and_slash_delimiter(self):
        box = FakeMailbox(capabilities=("IMAP4REV1", "MOVE", "X-GM-EXT-1"), delimiter="/")
        uid = box.add("INBOX", sender="hello@shop.test", subject="Hello", message_id="<g1@x>")
        box.gmail = {"social": {uid}}
        self.assertEqual(autosort.run(box, A, now=NOW).moved, 1)
        self.assertEqual(len(box.folders["INBOX/Social"]), 1)

    def test_folder_create_failure_skips_category(self):
        box = FakeMailbox()
        box.add("INBOX", sender="writer@substack.com", subject="Issue 9", message_id="<n9@x>")
        with mock.patch.object(core, "create_folder", side_effect=imaplib.IMAP4.error("nope")):
            result = autosort.run(box, A, now=NOW)
        self.assertEqual(result.moved, 0)
        self.assertIn("Cannot create INBOX.News", " ".join(result.skipped))
        self.assertEqual(len(box.folders["INBOX"]), 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_autosort -v`
Expected: the new `RunTests` FAIL with `AttributeError: module 'imap_cleanup_tool.autosort' has no attribute 'run'`; Task 7 tests still PASS.

- [ ] **Step 3: Extend the imports of `autosort.py`**

Replace the import block of `src/imap_cleanup_tool/autosort.py` with:

```python
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
```

- [ ] **Step 4: Append the run code to `autosort.py`**

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m unittest tests.test_autosort -v`
Expected: 21 tests, all PASS.

Check `test_ai_budget_leaves_rest_for_next_run`: senders sorted are `a@one.test` (UID 1) then `b@two.test` (UID 2). Budget 1 sends only `a@one.test`; UID 2 is left, so `last_uid` = 2 - 1 = 1.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/imap_cleanup_tool/autosort.py tests/test_autosort.py
git commit -m "feat(autosort): sort new INBOX mail with dry run, cutoff and AI budget"
```

---

### Task 9: CLI `--autosort`

**Files:**
- Modify: `src/imap_cleanup_tool/cli.py` (`_add_arguments`, new `_run_autosort`, `main`)
- Test: `tests/test_cli_config.py` (append class)

**Interfaces:**
- Consumes: `autosort.run`, `autosort.Busy`, `autosort.RunResult`.
- Produces: CLI flags `--autosort`, `--backlog`; `cli._run_autosort(conn, args, user: str) -> int`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli_config.py` (above any `if __name__` block), and add `from imap_cleanup_tool import autosort` to its imports:

```python
class AutosortCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def test_flags_parse(self):
        args = cli.parse_args(["--autosort", "--backlog", "--dry-run"])
        self.assertTrue(args.autosort and args.backlog and args.dry_run)
        args = cli.parse_args([])
        self.assertFalse(args.autosort or args.backlog)

    def _main(self, run_mock):
        with mock.patch.object(cli.core, "connect", return_value=mock.MagicMock()), \
             mock.patch.object(autosort, "run", run_mock):
            return cli.main(["--host", "imap.example.com", "--user", "me@example.com",
                             "--password", "pw", "--autosort", "--dry-run"])

    def test_main_dispatches_to_autosort(self):
        result = autosort.RunResult(run_id="r", dry_run=True, planned=[{
            "uid": "1", "message_id": "<1@x>", "sender": "a@b.test", "subject": "s",
            "category": "news", "folder": "INBOX.News", "layer": "news",
            "reason": "List-Post"}])
        run = mock.MagicMock(return_value=result)
        self.assertEqual(self._main(run), 0)
        self.assertEqual(run.call_args.kwargs["dry_run"], True)
        self.assertEqual(run.call_args.kwargs["backlog"], False)
        self.assertEqual(run.call_args.args[1], "me@example.com")

    def test_busy_exits_zero_and_errors_exit_two(self):
        self.assertEqual(self._main(mock.MagicMock(side_effect=autosort.Busy("busy"))), 0)
        self.assertEqual(self._main(mock.MagicMock(side_effect=ValueError("bad"))), 2)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_cli_config.AutosortCliTests -v`
Expected: FAIL — `AttributeError: 'Namespace' object has no attribute 'autosort'`.

- [ ] **Step 3: Implement**

In `src/imap_cleanup_tool/cli.py`, inside `_add_arguments`, after the `--delete-folder` argument add:

```python
    parser.add_argument("--autosort", action="store_true",
                        help="Auto-sort new INBOX mail into Social, News, "
                             "Promotions, Notifications, Receipts and CC folders. "
                             "Moves only, never deletes. Add --dry-run to preview.")
    parser.add_argument("--backlog", action="store_true",
                        help="With --autosort: also sort mail received before "
                             "auto-sort first ran.")
```

Add above `def _parse_ai_weights`:

```python
def _run_autosort(conn, args: argparse.Namespace, user: str) -> int:
    """One auto-sort pass; the log lines land in the scheduled-job log file."""
    from . import autosort
    try:
        result = autosort.run(conn, user, dry_run=args.dry_run, backlog=args.backlog,
                              ai_model=args.ai_model)
    except autosort.Busy as exc:
        core.logger.warning("%s", exc)
        return 0
    except ValueError as exc:
        core.logger.error("Auto-sort stopped: %s", exc)
        return 2
    verb = "Would move" if result.dry_run else "Moved"
    for item in result.planned:
        core.logger.info("%s %s | %s -> %s (%s: %s)", verb, item["sender"],
                         item["subject"], item["folder"], item["layer"], item["reason"])
    for note in result.skipped:
        core.logger.warning("%s", note)
    if result.ai_note:
        core.logger.info("AI layer skipped: %s", result.ai_note)
    core.logger.info("Auto-sort done: %d moved, %d planned, %d rules learned, "
                     "%d senders trusted.", result.moved, len(result.planned),
                     result.learned, result.trusted)
    return 0
```

In `main`, directly above `        if args.ai_cleanup:` (inside the `try:` after `list_senders`), add:

```python
        if args.autosort:
            return _run_autosort(conn, args, user)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m unittest tests.test_cli_config -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/imap_cleanup_tool/cli.py tests/test_cli_config.py
git commit -m "feat(cli): add --autosort and --backlog"
```

---

### Task 10: Web API

**Files:**
- Modify: `src/imap_cleanup_tool/webapp.py` (imports line ~37, models near `TriageRuleIn` line ~409, `JobIn` line ~562, `_job_from` line ~2010, new endpoints after `/api/triage/forget`)
- Test: `tests/test_webapp.py` (append tests to `WebApiTests`)

**Interfaces:**
- Consumes: `autosort.run`, `autosort.undo_move`, `autosort.undo_run`, `autosort.Busy`; `sortstore.get_settings`, `update_settings`, `moves`, `reviews`, `save_rule`, `clear_review`, `VALID_CATEGORIES`; `llm.list_models`.
- Produces endpoints:
  - `POST /api/autosort/run` `{sid, dry_run=true, backlog=false}` -> `RunResult.to_dict()`
  - `GET /api/autosort/state/{sid}` -> `{"settings", "moves", "review"}`
  - `POST /api/autosort/undo` `{sid, move_id=0, run_id=""}` -> `{"undone", "errors"}`
  - `POST /api/autosort/settings` `{sid, ai_model="", ai_min_confidence=0.8, ai_max_calls=50}` -> settings
  - `POST /api/autosort/review` `{sid, sender, category}` -> `{"sender", "category"}`
  - `JobIn.autosort: bool = False` -> job args `["--profile", P, ..., "--autosort", "--yes"]`

- [ ] **Step 1: Write the failing tests**

Append to `WebApiTests` in `tests/test_webapp.py`:

```python
    def _autosort_session(self, tmp):
        from imap_cleanup_tool import webapp
        from tests.fake_imap import FakeMailbox

        box = FakeMailbox()
        box.add("INBOX", sender="writer@substack.com", subject="Issue 1",
                message_id="<w1@x>")
        sess = webapp.Session("autosort-test", box, "imap.example.com", 993,
                              "me@example.com")
        webapp._SESSIONS[sess.sid] = sess
        return box, sess

    def test_autosort_run_undo_and_review(self):
        from imap_cleanup_tool import scheduler, sortstore, triage, webapp

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(triage, "rules_path", return_value=Path(tmp) / "r.sqlite"), \
             mock.patch.object(scheduler, "config_dir", return_value=Path(tmp)), \
             mock.patch.object(webapp, "_folder_dicts", return_value=[]):
            box, sess = self._autosort_session(tmp)
            try:
                sortstore.update_settings(sess.user, started_at="2026-09-01T00:00:00+00:00")
                dry = self.client.post("/api/autosort/run", json={"sid": sess.sid})
                self.assertEqual(dry.status_code, 200)
                self.assertEqual(dry.json()["planned"][0]["folder"], "INBOX.News")
                self.assertEqual(len(box.folders["INBOX"]), 1)
                real = self.client.post("/api/autosort/run",
                                        json={"sid": sess.sid, "dry_run": False})
                self.assertEqual(real.json()["moved"], 1)
                state = self.client.get(f"/api/autosort/state/{sess.sid}").json()
                move_id = state["moves"][0]["id"]
                undo = self.client.post("/api/autosort/undo",
                                        json={"sid": sess.sid, "move_id": move_id})
                self.assertEqual(undo.json(), {"undone": 1, "errors": []})
                self.assertEqual(len(box.folders["INBOX"]), 1)
                self.assertEqual(self.client.post("/api/autosort/undo",
                                 json={"sid": sess.sid}).status_code, 400)
                sortstore.add_review(sess.user, "q@z.test", "news", 0.4, "unsure")
                pick = self.client.post("/api/autosort/review", json={
                    "sid": sess.sid, "sender": "q@z.test", "category": "promotions"})
                self.assertEqual(pick.status_code, 200)
                self.assertEqual(sortstore.reviews(sess.user), [])
                self.assertEqual(sortstore.rules(sess.user)["q@z.test"]["source"], "user")
            finally:
                webapp._SESSIONS.pop(sess.sid, None)

    def test_autosort_settings_reject_encrypted_model(self):
        from imap_cleanup_tool import scheduler, triage, webapp

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(triage, "rules_path", return_value=Path(tmp) / "r.sqlite"), \
             mock.patch.object(scheduler, "config_dir", return_value=Path(tmp)), \
             mock.patch.object(llm, "list_models", return_value=[
                 {"name": "sec", "encrypted": True}, {"name": "open", "encrypted": False}]):
            _, sess = self._autosort_session(tmp)
            try:
                bad = self.client.post("/api/autosort/settings",
                                       json={"sid": sess.sid, "ai_model": "sec"})
                self.assertEqual(bad.status_code, 400)
                good = self.client.post("/api/autosort/settings", json={
                    "sid": sess.sid, "ai_model": "open", "ai_min_confidence": 0.85,
                    "ai_max_calls": 20})
                self.assertEqual(good.json()["ai_max_calls"], 20)
            finally:
                webapp._SESSIONS.pop(sess.sid, None)

    def test_autosort_job_args(self):
        with mock.patch.object(profiles, "list_profiles",
                               return_value=[{"name": "p", "encrypted": False}]), \
             mock.patch.object(scheduler, "load_jobs", return_value=[]), \
             mock.patch.object(scheduler, "upsert_job") as upsert, \
             mock.patch.object(scheduler, "export_system", return_value="cmd"):
            r = self.client.post("/api/jobs", json={
                "name": "autosort", "profile": "p", "kind": "interval",
                "minutes": 15, "autosort": True})
        self.assertEqual(r.status_code, 200)
        job = upsert.call_args.args[0]
        self.assertIn("--autosort", job.args)
        self.assertEqual(job.schedule, {"kind": "interval", "minutes": 15})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_webapp -v`
Expected: the 3 new tests FAIL (404 on `/api/autosort/*`; `--autosort` missing from job args).

- [ ] **Step 3: Implement**

In `src/imap_cleanup_tool/webapp.py`:

1. Change the package import to:

```python
from . import (__version__, ai, autosort, core, llm, notifications, oauth, profiles,
               scheduler, sortstore, spamstore, triage)
```

2. After `class TriageRuleIn`, add:

```python
    class AutosortRunIn(BaseModel):
        sid: str
        dry_run: bool = True
        backlog: bool = False

    class AutosortUndoIn(BaseModel):
        sid: str
        move_id: int = 0
        run_id: str = ""

    class AutosortSettingsIn(BaseModel):
        sid: str
        ai_model: str = ""
        ai_min_confidence: float = 0.8
        ai_max_calls: int = 50

    class AutosortReviewIn(BaseModel):
        sid: str
        sender: str
        category: str
```

3. In `class JobIn`, after `ai_check_spam: bool = True`, add:

```python
        autosort: bool = False        # run --autosort instead of a cleanup
```

4. In `_job_from`, directly after the `for folder in (body.folders or ["INBOX"]):` loop and before `if body.ai_cleanup:`, add:

```python
        if body.autosort:
            args += ["--autosort", "--yes"]
            try:
                sched = scheduler.build_schedule(
                    body.kind, time=body.time, date=body.date,
                    minutes=body.minutes, day=body.day)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            return scheduler.Job(name=name, args=args, schedule=sched, label=label)
```

5. After the `@app.post("/api/triage/forget")` handler, add:

```python
    @app.post("/api/autosort/run")
    def autosort_run(body: AutosortRunIn) -> dict[str, Any]:
        """One auto-sort pass for the connected account. Dry run by default."""
        sess = _session(body.sid)
        if sess.run and sess.run.status == "running":
            raise HTTPException(409, "An operation is running; try again later.")
        with sess.lock:
            try:
                result = autosort.run(sess.conn, sess.user, dry_run=body.dry_run,
                                      backlog=body.backlog)
                if not body.dry_run:
                    sess.folders = _folder_dicts(sess.conn)
            except autosort.Busy as exc:
                raise HTTPException(409, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            except (OSError, core.imaplib.IMAP4.error) as exc:
                raise HTTPException(502, f"IMAP error: {exc}") from exc
        return result.to_dict()

    @app.get("/api/autosort/state/{sid}")
    def autosort_state(sid: str) -> dict[str, Any]:
        sess = _session(sid)
        return {"settings": sortstore.get_settings(sess.user),
                "moves": sortstore.moves(sess.user, 100),
                "review": sortstore.reviews(sess.user)}

    @app.post("/api/autosort/undo")
    def autosort_undo(body: AutosortUndoIn) -> dict[str, Any]:
        sess = _session(body.sid)
        if not body.move_id and not body.run_id:
            raise HTTPException(400, "Choose a move or a run to undo.")
        with sess.lock:
            try:
                if body.move_id:
                    autosort.undo_move(sess.conn, sess.user, body.move_id)
                    result = {"undone": 1, "errors": []}
                else:
                    result = autosort.undo_run(sess.conn, sess.user, body.run_id)
                sess.folders = _folder_dicts(sess.conn)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            except (OSError, core.imaplib.IMAP4.error) as exc:
                raise HTTPException(502, f"IMAP error: {exc}") from exc
        return result

    @app.post("/api/autosort/settings")
    def autosort_settings(body: AutosortSettingsIn) -> dict[str, Any]:
        sess = _session(body.sid)
        name = body.ai_model.strip()
        if name:
            info = next((m for m in llm.list_models() if m["name"] == name), None)
            if info is None:
                raise HTTPException(400, f"Model {name!r} not found.")
            if info["encrypted"]:
                raise HTTPException(400, "Encrypted model configs can't run "
                                         "unattended - use a non-encrypted one.")
        try:
            return sortstore.update_settings(
                sess.user, ai_model=name, ai_min_confidence=body.ai_min_confidence,
                ai_max_calls=body.ai_max_calls)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/autosort/review")
    def autosort_review(body: AutosortReviewIn) -> dict[str, Any]:
        sess = _session(body.sid)
        try:
            sortstore.save_rule(sess.user, body.sender, body.category, "user")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        sortstore.clear_review(sess.user, body.sender)
        return {"sender": body.sender.strip().lower(), "category": body.category}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m unittest tests.test_webapp -v`
Expected: all PASS. If `test_autosort_job_args` fails on a missing `JobIn` field, read the `Match` and `Options` models (grep `class Match` and `class Options` in `webapp.py`) and add the required fields to the test JSON.

- [ ] **Step 5: Commit**

```bash
git add src/imap_cleanup_tool/webapp.py tests/test_webapp.py
git commit -m "feat(web): add auto-sort API and scheduled auto-sort jobs"
```

---

### Task 11: Auto-sort tab in the web UI

**Files:**
- Modify: `src/imap_cleanup_tool/web/static/index.html`

**Interfaces:**
- Consumes: Task 10 endpoints; `/api/jobs/install` with `autosort: true`; JS helpers `api`, `$`, `toast`, `toastErr`, `confirmDialog`, `escapeHtml`, globals `SID`, `connectedProfile`; `/api/llm-models`.

- [ ] **Step 1: Add the tab buttons**

After the desktop tab `<div class="tab" data-tab="triage">…Inbox sort</div>` (line ~164) add:

```html
    <div class="tab" data-tab="autosort"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7h13l-3-3M21 17H8l3 3"/></svg>Auto-sort</div>
```

After the drawer nav item `<button class="navitem" data-tab="triage">…Inbox sort</button>` (line ~187) add:

```html
      <button class="navitem" data-tab="autosort"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7h13l-3-3M21 17H8l3 3"/></svg>Auto-sort</button>
```

- [ ] **Step 2: Add the tab panel**

Directly above `<!-- ============================ SCHEDULING TAB ============================ -->` add:

```html
<!-- ============================ AUTO-SORT TAB ============================ -->
<div id="tab-autosort" class="hidden space-y-5">
  <section class="bg-panel border border-line rounded-xl p-5">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div><h2 class="font-display text-xl font-semibold text-brand-light">Auto-sort</h2>
        <p class="text-muted text-sm mt-2 max-w-2xl">Sort new INBOX mail into Social, News, Promotions, Notifications, Receipts and CC. Nothing is deleted. Move a message back in any mail app, and the tool remembers that sender.</p></div>
      <div class="flex flex-wrap gap-2">
        <button id="autosortPreview" type="button" class="px-4 py-2 rounded-lg border border-line text-cream hover:border-brand">Preview</button>
        <button id="autosortRunNow" type="button" class="px-4 py-2 rounded-lg border border-line text-cream hover:border-brand">Run now</button>
        <button id="autosortEnable" type="button" class="px-4 py-2 rounded-lg bg-brand text-ink font-semibold hover:bg-brand-light">Turn on (every 15 min)</button>
      </div>
    </div>
    <p id="autosortStatus" class="text-sm text-muted mt-4">Connect in Cleanup first.</p>
    <div id="autosortPlan" class="text-sm mt-3"></div>
  </section>
  <section class="bg-panel border border-line rounded-xl p-5">
    <div class="flex items-center justify-between gap-3"><h2 class="font-display font-semibold text-brand-light">Recent moves</h2>
      <button id="autosortUndoRun" type="button" class="text-xs text-muted hover:text-cream">Undo last run</button></div>
    <div id="autosortMoves" class="text-sm text-muted mt-4">No moves yet.</div>
  </section>
  <section class="bg-panel border border-line rounded-xl p-5">
    <h2 class="font-display font-semibold text-brand-light">AI review</h2>
    <p class="text-sm text-muted mt-2">The AI was not sure about these senders. Pick a folder once. The tool remembers it.</p>
    <div id="autosortReview" class="text-sm text-muted mt-4">Nothing to review.</div>
  </section>
  <section class="bg-panel border border-line rounded-xl p-5">
    <h2 class="font-display font-semibold text-brand-light">AI fallback</h2>
    <p class="text-sm text-muted mt-2">Used only for senders no rule covers. One call per sender. Only sender, subjects and header flags are sent.</p>
    <div class="grid grid-cols-1 sm:grid-cols-3 gap-3 mt-4">
      <label class="text-xs text-muted">Model <select id="autosortModel" class="field mt-1"></select></label>
      <label class="text-xs text-muted">Minimum confidence <input id="autosortConfidence" type="number" min="0.05" max="1" step="0.05" class="field mt-1" value="0.8"></label>
      <label class="text-xs text-muted">AI calls per run <input id="autosortMaxCalls" type="number" min="0" max="500" class="field mt-1" value="50"></label>
    </div>
    <button id="autosortSaveSettings" type="button" class="mt-3 px-4 py-2 rounded-lg border border-line text-cream hover:border-brand">Save AI settings</button>
  </section>
</div>
```

- [ ] **Step 3: Add the JS**

Above `/* ---------- events ---------- */` add:

```javascript
/* ---------- Auto-sort ---------- */
let autosortLastRun='';
function renderAutosortPlan(r){
  const verb=r.dry_run?'Would move':'Moved';
  const items=(r.planned||[]).slice(0,100).map(p=>'<div class="border-b border-line/70 py-2"><span class="text-cream">'+escapeHtml(p.subject||'(no subject)')+'</span> <span class="text-muted text-xs">'+escapeHtml(p.sender)+'</span><div class="text-xs text-muted">'+verb+' to '+escapeHtml(p.folder)+' · '+escapeHtml(p.layer)+': '+escapeHtml(p.reason)+'</div></div>').join('');
  const notes=(r.skipped||[]).concat(r.ai_note?['AI: '+r.ai_note]:[]).map(n=>'<div class="text-warn text-xs mt-1">'+escapeHtml(n)+'</div>').join('');
  $('autosortPlan').innerHTML=(items||'<p class="text-muted">Nothing to move.</p>')+notes;
}
async function loadAutosort(){
  if(!SID){ $('autosortStatus').textContent='Connect in Cleanup first.'; return; }
  try{
    const d=await api('/api/autosort/state/'+encodeURIComponent(SID),null,'GET');
    $('autosortConfidence').value=d.settings.ai_min_confidence;
    $('autosortMaxCalls').value=d.settings.ai_max_calls;
    const models=(await (await fetch('/api/llm-models')).json()).models||[];
    $('autosortModel').innerHTML='<option value="">No AI (rules only)</option>'+models.filter(m=>!m.encrypted).map(m=>'<option value="'+escapeHtml(m.name)+'" '+(m.name===d.settings.ai_model?'selected':'')+'>'+escapeHtml(m.name+' ('+m.model+')')+'</option>').join('');
    $('autosortStatus').textContent=d.settings.started_at?'Sorting mail received since '+d.settings.started_at+'.':'Not started. Click Preview first.';
    autosortLastRun=d.moves.length?d.moves[0].run_id:'';
    $('autosortMoves').innerHTML=d.moves.length?d.moves.map(m=>'<div class="flex items-center justify-between gap-3 border-b border-line py-2"><span class="break-all"><span class="text-cream">'+escapeHtml(m.subject||'(no subject)')+'</span> <small class="text-muted">'+escapeHtml(m.sender)+' → '+escapeHtml(m.target_folder)+' ('+escapeHtml(m.layer)+')</small></span>'+(m.undone_at?'<small class="text-muted">Undone</small>':'<button class="autosortUndo text-brand-light text-xs hover:underline" data-id="'+m.id+'" type="button">Undo</button>')+'</div>').join(''):'No moves yet.';
    $('autosortMoves').querySelectorAll('.autosortUndo').forEach(b=>{ b.onclick=()=>autosortUndo({move_id:Number(b.dataset.id)}); });
    $('autosortReview').innerHTML=d.review.length?d.review.map((r,i)=>'<div class="flex flex-wrap items-center justify-between gap-3 border-b border-line py-2"><span class="break-all text-cream">'+escapeHtml(r.sender)+' <small class="text-muted">AI: '+escapeHtml(r.category)+' ('+Math.round(r.confidence*100)+'%) '+escapeHtml(r.reason)+'</small></span><span class="flex gap-2"><select class="field text-xs autosortPick" data-i="'+i+'"><option value="inbox">Inbox</option>'+Object.entries(SORT_FOLDERS).map(([k,v])=>'<option value="'+k+'" '+(k===r.category?'selected':'')+'>'+v+'</option>').join('')+'</select><button class="autosortPickSave text-brand-light text-xs hover:underline" data-i="'+i+'" type="button">Save</button></span></div>').join(''):'Nothing to review.';
    $('autosortReview').querySelectorAll('.autosortPickSave').forEach(b=>{ b.onclick=async()=>{
      const r=d.review[Number(b.dataset.i)], category=$('autosortReview').querySelector('.autosortPick[data-i="'+b.dataset.i+'"]').value;
      try{ await api('/api/autosort/review',{sid:SID,sender:r.sender,category}); await loadAutosort(); }catch(e){ toastErr(e.message); }
    }; });
  }catch(e){ $('autosortStatus').textContent='Could not load auto-sort: '+e.message; }
}
async function autosortRun(dryRun){
  if(!SID){ toastErr('Connect in Cleanup first.'); return; }
  $('autosortStatus').textContent=dryRun?'Previewing…':'Sorting…';
  try{ const r=await api('/api/autosort/run',{sid:SID,dry_run:dryRun}); renderAutosortPlan(r);
    if(!dryRun) toast('Moved '+r.moved+' message(s).','border-ok/50 bg-ok/15 text-ok');
    await loadAutosort(); }
  catch(e){ $('autosortStatus').textContent='Auto-sort failed: '+e.message; toastErr(e.message); }
}
async function autosortUndo(body){
  try{ const r=await api('/api/autosort/undo',Object.assign({sid:SID},body));
    toast('Moved '+r.undone+' message(s) back to INBOX.','border-ok/50 bg-ok/15 text-ok');
    (r.errors||[]).forEach(e=>toastErr(e)); await loadAutosort(); }
  catch(e){ toastErr(e.message); }
}
if($('autosortPreview')) $('autosortPreview').onclick=()=>autosortRun(true);
if($('autosortRunNow')) $('autosortRunNow').onclick=async()=>{
  if(!(await confirmDialog({title:'Sort INBOX now?',message:'Move matching new INBOX mail to the sort folders. Nothing is deleted, and every move can be undone.',okText:'Sort now'}))) return;
  autosortRun(false);
};
if($('autosortUndoRun')) $('autosortUndoRun').onclick=()=>{ if(autosortLastRun) autosortUndo({run_id:autosortLastRun}); else toastErr('No run to undo.'); };
if($('autosortSaveSettings')) $('autosortSaveSettings').onclick=async()=>{
  try{ await api('/api/autosort/settings',{sid:SID,ai_model:$('autosortModel').value,
    ai_min_confidence:Number($('autosortConfidence').value),ai_max_calls:Number($('autosortMaxCalls').value)});
    toast('AI settings saved.','border-ok/50 bg-ok/15 text-ok'); }
  catch(e){ toastErr(e.message); }
};
if($('autosortEnable')) $('autosortEnable').onclick=async()=>{
  if(!connectedProfile){ toastErr('Connect with a saved, non-encrypted profile to schedule auto-sort.'); return; }
  if(!(await confirmDialog({title:'Turn on auto-sort?',message:'Install a system task that sorts new INBOX mail every 15 minutes with profile '+connectedProfile+'. Run Preview first to check the result.',okText:'Turn on'}))) return;
  try{ const d=await api('/api/jobs/install',{name:'autosort',profile:connectedProfile,kind:'interval',minutes:15,autosort:true});
    toast('Auto-sort is on.','border-ok/50 bg-ok/15 text-ok'); $('autosortStatus').textContent=d.message||'Auto-sort is on.'; }
  catch(e){ toastErr(e.message); }
};
```

- [ ] **Step 4: Register the tab**

Change `const TABS=['cleanup','triage','scheduling',…]` to include `'autosort'` after `'triage'`, and in `switchTab` add after the triage line:

```javascript
  if(name==='autosort'){ loadAutosort(); }
```

- [ ] **Step 5: Check the page in a browser**

Run: `.venv/bin/imap-cleanup-tool-web` (use the `run` skill if the port or flags differ). Open the printed URL. Connect with a test account or profile. Open **Auto-sort**. Click **Preview**. Expected: a list of "Would move … to INBOX.News" lines or "Nothing to move.", no console errors, and the Inbox sort tab still shows Social plus the other folders. Take a screenshot as proof.

- [ ] **Step 6: Commit**

```bash
git add src/imap_cleanup_tool/web/static/index.html
git commit -m "feat(web): add Auto-sort tab"
```

---

### Task 12: Docs

**Files:**
- Modify: `README.md` (after the Inbox sort section, line ~470)
- Modify: `TRIAGE_RULES.md` (append)
- Modify: `CONFIG_REFERENCE.md` (CLI flag table)

- [ ] **Step 1: README section**

Add after the Inbox sort section in `README.md`:

```markdown
## Auto-sort

Auto-sort files new INBOX mail into `INBOX.Social`, `INBOX.News`,
`INBOX.Promotions`, `INBOX.Notifications`, `INBOX.Receipts` and `INBOX.CC`.
It moves mail only. It never deletes and never uses Trash.

How it decides, first match wins:

1. Flagged mail and protected subjects (sign-in codes, passwords, calendar
   replies, travel, medical, legal) stay in INBOX.
2. Your own choices, and senders you moved back yourself.
3. People you wrote to (read from your Sent folder) stay in INBOX.
4. Rule packs and headers: receipts, social networks, automated notices,
   mailing lists and newsletters, marketing mail, mail where you are only in Cc.
5. Optional AI fallback for unknown senders (one call per sender, headers and
   subjects only). Unsure answers wait in the Auto-sort tab for your choice.
6. Everything else stays in INBOX.

Teach it from any mail app: move a sorted message to another sort folder, or
back to INBOX. The next run remembers that sender. Every move is logged and can
be undone from the Auto-sort tab.

Only mail received after the first run is sorted. Use `--backlog` once to sort
older mail.

```bash
imap-cleanup-tool --profile work --autosort --dry-run   # preview
imap-cleanup-tool --profile work --autosort             # sort now
imap-cleanup-tool --profile work --autosort --backlog   # include older mail
```

Rule data comes from inpector/sieve-filters (CC0), poli0981/proton-sieve-filters
(CC0) and scrothers/sieve-filters (MIT). See `THIRD_PARTY_NOTICES.md`.
```

- [ ] **Step 2: TRIAGE_RULES.md**

Append to `TRIAGE_RULES.md`:

```markdown
# Auto-sort rules

`--autosort` moves new INBOX mail to sort folders. It never deletes mail.

- Layer order: flagged, protected, your rule, learned rule, Sent trust,
  receipts, social, notifications, news, promotions, CC, saved AI rule, AI
  fallback, INBOX.
- `List-Unsubscribe` alone and transactional ESP headers alone never pick a
  folder.
- A message that auto-sort moved once is never moved again by auto-sort.
- Learning looks at mail received in the last 30 days in INBOX and the sort
  folders. A message you moved elsewhere teaches nothing.
- AI rules never replace your own or learned rules. An AI answer below the
  minimum confidence (default 0.8) only adds the sender to the review list.
```

- [ ] **Step 3: CONFIG_REFERENCE.md**

Find the CLI flag table (`grep -n "ai-review-obsolete" CONFIG_REFERENCE.md README.md`) and add rows in the same format:

```markdown
| `--autosort` | Sort new INBOX mail into Social, News, Promotions, Notifications, Receipts and CC folders. Moves only. Combine with `--dry-run` to preview. |
| `--backlog` | With `--autosort`: also sort mail received before auto-sort first ran. |
```

- [ ] **Step 4: Run the full suite one last time**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add README.md TRIAGE_RULES.md CONFIG_REFERENCE.md
git commit -m "docs: describe auto-sort"
```

---

## Self-review notes

- Spec coverage: folders (T5), rule packs (T1), header signals and regexes (T2), store and migration (T3), layer chain incl. Gmail/Fastmail hints (T4, T8), AI fallback incl. threshold, budget, encrypted skip, cost (T6, T8), learning (T7), Sent trust (T7), run with dry run, lock, cutoff, UIDVALIDITY, flagged, folder-create failure, missing Message-ID (T8), undo (T7, T10, T11), CLI (T9), web API and scheduling (T10), UI (T5, T11), docs and notices (T1, T12).
- Out of scope, per spec: BlackHole, snooze, no-reply follow-up, digest.
