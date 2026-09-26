# Auto-sort core loop — design

Date: 2026-09-25
Status: approved design, not yet planned
Branch context: `feature/inbox-triage-rules`

## Goal

Give the tool the core SaneBox loop: new INBOX mail is sorted into category
folders automatically, the tool learns from the user's own folder moves in any
mail client, senders the user writes to stay in INBOX, and a rule-pack plus
AI fallback covers senders nobody has trained yet. The target is inbox zero
without deleting anything.

This is part 1 of 5. Out of scope here, each with its own later spec:
BlackHole (folder-as-ban), snooze folders, no-reply follow-up, daily digest.

## Current state

- `triage.py` previews INBOX headers and suggests `INBOX.Social` or
  `INBOX.Other`. The user moves one message at a time from the web UI.
- `inpector_rules.json` vendors CC0 header rules from
  `inpector/sieve-filters` at commit `fe5a0ce`.
- `sender_rule` in `triage_rules.sqlite` stores per-account sender choices.
- `move_checked` moves one message safely (UID + UIDVALIDITY re-check, `MOVE`
  or `COPY` + `STORE \Deleted` + `UID EXPUNGE`), never to Trash.
- `scheduler.py` runs OS-level jobs, including `interval` jobs.
- `llm.py` / `ai.py` hold saved LLM models (litellm) and cost logging.

## Decisions

| Topic | Decision |
|---|---|
| Autonomy | Scheduled job moves mail. It never deletes and never uses Trash. |
| Folders | Full set: Social, News, Promotions, Notifications, Receipts, CC. |
| Training | Watch the user's folder moves via `Message-ID`. |
| Classifier | Hybrid: deterministic layers first, AI fallback for unknown senders. |

## Folders

| Category | Folder |
|---|---|
| inbox | `INBOX` (no move) |
| social | `INBOX.Social` |
| news | `INBOX.News` |
| promotions | `INBOX.Promotions` |
| notifications | `INBOX.Notifications` |
| receipts | `INBOX.Receipts` |
| cc | `INBOX.CC` |

Folder names use the server's hierarchy delimiter (the existing `INBOX.`
prefix assumes `.`; resolve the real delimiter from `LIST`). Missing folders
are created on first use. The existing `INBOX.Other` is left untouched; the
tool no longer writes to it. The manual Inbox sort tab maps its old `other`
choice to `promotions`.

## Architecture

New module `src/imap_cleanup_tool/autosort.py` owns one run. Rule data and
matching stay in `triage.py` (extended). A new data module loads rule packs.

```
autosort.run(conn, account, options)
  1. learn()     -> read move_log, find moved messages, update sender rules
  2. trust()     -> read new \Sent headers, record trusted recipients
  3. classify()  -> new INBOX UIDs through the layer chain
  4. move()      -> move_checked per message, append move_log rows
```

### Units

- `rulepacks.py` — loads `inpector_rules.json` and the new
  `poli0981_rules.json`, merges them into one lookup: domain -> category and
  per-category subject regexes. Pure, no IMAP.
- `triage.py` — `classify()` becomes the layer chain below. Returns
  `(category, layer, reason)`.
- `signals.py` — header-signal detection. Input: parsed header dict. Output:
  list of `(category, strength)`. Pure.
- `ai_sort.py` — AI fallback per sender. Builds the prompt, calls the saved
  model via the existing litellm path, validates strict JSON.
- `autosort.py` — run orchestration, state, locking, move log, undo.
- `cli.py` — `--autosort`, `--dry-run`, `--backlog`.
- `webapp.py` + `index.html` — Auto-sort tab.

### Storage (`triage_rules.sqlite`)

- `sender_rule`: add `source TEXT NOT NULL DEFAULT 'user'` with values
  `user`, `learned`, `ai`, `sent`; add `confidence REAL`, `updated_at TEXT`.
  Migration adds columns with defaults; existing rows become `user`.
- `move_log`: `id, run_id, account, message_id, uid, sender, subject,
  source_folder, target_folder, layer, reason, moved_at, undone_at`.
- `sync_state`: `account, folder, uidvalidity, last_uid, updated_at`.
- `autosort_account`: `account, enabled, started_at` — `started_at` is the
  cutoff; mail dated before it is never auto-moved.

## Classification layers

First match wins. Order:

1. **Flagged** — message has `\Flagged`: stay in INBOX.
2. **Protected** — existing `core._PROTECTED_SUBJECT_HINT`,
   `_EXTRA_PROTECTED_SUBJECT`, calendar replies, inpector `Security` group:
   stay in INBOX.
3. **User / learned sender rule** — source `user` or `learned`.
4. **Sent trust** — sender is a recipient of the user's sent mail: INBOX.
5. **Receipts** — inpector `Deliveries`, `Finances`, `Fix Costs`; poli0981
   `invoice`, `shipping`; receipts subject regex.
6. **Social** — social domain (poli0981 `social`, inpector social set,
   `facebookmail.com`, `redditmail.com`, `linkedin.com`).
7. **Notifications** — `Auto-Submitted: auto-generated`, or a no-reply local
   part without `List-Unsubscribe`, or notifications subject regex.
8. **News** — `List-Id` with `List-Post`, `X-Mailman-Version`, `X-BeenThere`,
   newsletter-platform sender (substack, beehiiv, convertkit/kit,
   buttondown, ghost.io, `cmail19.com`, `cmail20.com`), poli0981 `news`,
   inpector `Mailinglists`.
9. **Promotions** — marketing ESP headers (Klaviyo `X-Kmail-*`, ExactTarget
   `X-SFMC-Stack`, Emarsys/MailUp `X-CSA-Complaints`, Mailchimp `X-MC-User`),
   `X-Feedback-ID` containing `campaign`, `List-Unsubscribe-Post:
   List-Unsubscribe=One-Click` together with the promotions subject regex,
   poli0981 `shopping`, inpector `Shopping`.
10. **CC** — the account address appears in `Cc` and not in `To`.
11. **AI sender rule** — saved rule with source `ai` and confidence at or
    above the threshold.
12. **AI fallback** — see below.
13. **Default** — stay in INBOX.

Weak signals never decide alone: `List-Unsubscribe` alone, and SendGrid /
SES / Mailgun / Postmark headers alone.

Optional server-specific signals, used only after a capability check and
only as input to layers 6-9: Gmail `X-GM-RAW "category:<name>"` search;
Fastmail `X-ME-VSCategory` header.

### Subject regexes

English + German, case-insensitive, subject only:

```python
RECEIPTS = r"\b(receipt|invoice|order (confirm|#|no\.?|number)|your order|payment (received|confirm)|thank(s| you) for your (order|purchase|payment)|shipped|shipping confirm|out for delivery|delivered|tracking|refund|subscription renewed|renewal|Rechnung|Quittung|Beleg|Bestellbestätigung|Ihre Bestellung|Deine Bestellung|Bestellung (Nr|eingegangen)|Zahlungsbestätigung|Zahlung erhalten|versandt|verschickt|Versandbestätigung|Sendungsverfolgung|zugestellt|Lieferung|Gutschrift|Rückerstattung|Abo verlängert)\b"

NOTIFICATIONS = r"\b(notification|reminder|alert|update[sd]?|mentioned you|commented|replied|new (message|comment|follower)|invited you|shared .* with you|your (report|statement|weekly|monthly)|build (failed|passed)|digest|status|Benachrichtigung|Erinnerung|Hinweis|Mitteilung|Aktualisierung|neue Nachricht|hat dich erwähnt|Kontoauszug|Monatsübersicht|Wochenbericht|Statusmeldung)\b"

PROMOTIONS = r"(\d{1,2}\s?%|\b(sale|deal|offer|discount|coupon|promo|save|free shipping|limited time|last chance|ends (today|tonight)|black friday|cyber monday|exclusive|new arrivals|Angebot|Rabatt|Gutschein|Aktion|Sonderangebot|reduziert|gratis|kostenlos|versandkostenfrei|nur heute|letzte Chance|Schnäppchen|Sale|Neuheiten|exklusiv)\b)"
```

`your account` is deliberately absent from NOTIFICATIONS; layer 2 handles
account-security mail.

## Rule packs

| Source | License | Use |
|---|---|---|
| `inpector/sieve-filters` | CC0-1.0 | Already vendored. Base set. |
| `poli0981/proton-sieve-filters` `data/categories/*.yml` | CC0-1.0 (`data/LICENSE`) | Vendor `social`, `news`, `invoice`, `shipping`, `shopping` as `poli0981_rules.json`, pinned commit SHA. Drop `kind: block` entries. Skip `kind: ceded` entries for that category. |
| `scrothers/sieve-filters` | MIT | Add `facebookmail.com`, `redditmail.com`, and its retailer marketing subdomains. Keep the MIT notice. |
| `bigio/spamassassin-esp` | Apache-2.0 | Reference only for ESP header names; patterns written by hand. |

Not used (no license): jsit gist, loricandre/awesome-sieve-filters,
linuz90/gmail-newsletters-filter, joshuaspence/gmail-filters.

A script `scripts/update_rulepacks.py` regenerates `poli0981_rules.json` from
a given commit so updates are reproducible. It is a dev tool, not run at
runtime; the app never fetches rules from the network.

## AI fallback

- Runs only when layers 1-11 miss.
- Unit: one sender. All unmatched messages from that sender in this run go in
  one call. Payload: sender, up to 5 recent subjects, header flags (list,
  bulk, auto-submitted, ESP detected, CC-only). No bodies.
- Output: strict JSON `{"category", "confidence", "reason"}`; category in the
  seven values above.
- Move only when `confidence >= ai_min_confidence` (default 0.8). Below it:
  stay in INBOX and list the sender in the web UI review list.
- Saved as `sender_rule` with source `ai`. `user` and `learned` rules are
  never overwritten by `ai`.
- Model: a saved `llm.py` model named in autosort settings. A model with an
  encrypted key cannot run unattended; the layer is skipped and the run log
  says so.
- Budget: `ai_max_calls_per_run` (default 50). Costs via `llm.log_cost`.
- Timeout or invalid JSON: stay in INBOX, continue the run.

## Learning from moves

- Each run reads `move_log` rows from the last 30 days with `undone_at` null.
- For each row: `UID SEARCH HEADER Message-ID <id>` in the target folder,
  then INBOX, then the other sort folders.
- Found in a different sort folder: sender rule = that category, source
  `learned`.
- Found in INBOX: sender rule = `inbox`, source `learned`.
- Not found anywhere: no change.
- Rows without a `Message-ID` are not learnable; log that at move time.

## Sent trust

- Folder: special-use `\Sent` via the existing core helper.
- First run: last 12 months. Later runs: UIDs above `sync_state.last_uid`.
- Reads `To` and `Cc`; stores each address as a `sent` sender rule with
  category `inbox`. A `user` or `learned` rule for the same sender wins.

## Run, CLI, scheduling

- `imap-cleanup --autosort` runs one pass for the configured account.
- `--dry-run` classifies and writes a report, moves nothing.
- `--backlog` also processes mail dated before `started_at`; one-time use.
- Scheduling reuses `scheduler.build_schedule("interval", minutes=15)`.
- One lock file per account under `config_dir()`; a second run exits early.

## Web UI

New Auto-sort tab:

- Enable per account. Enabling always starts with a dry run; the user reviews
  the result and then confirms.
- Folder map (read-only in this part).
- Recent moves with layer and reason, per-row Undo, "Undo last run".
- AI review list: low-confidence senders with a one-click category choice
  (writes a `user` rule).
- AI settings: model, min confidence, max calls per run.

## Errors and safety

- Never Trash, never delete. `\Deleted` only inside the existing
  `COPY` + `UID EXPUNGE` fallback of `move_checked`.
- UIDVALIDITY change: reset `sync_state` for that folder, skip moves this run.
- Dropped IMAP connection: stop; completed moves stay logged; next run
  resumes from `last_uid`.
- Folder creation failure: skip that category, log it.
- Mail dated before `started_at` is never moved unless `--backlog`.
- `\Flagged` mail is never moved.

## Undo

- Per-row undo: find the message by `Message-ID` in `target_folder`, move it
  to INBOX, set `undone_at`, save sender as `inbox` with source `user`.
- Undo last run: the same for every row with that `run_id`.

## Testing

Pytest, following `tests/test_triage.py` and the fake-IMAP patterns already
in `tests/`.

- Layer order: protected and flagged beat every layer; user beats AI.
- Rule packs: `block` dropped, `ceded` respected, subdomain match.
- Header signals: each row of the signal table maps to its category; weak
  signals alone do not decide.
- Subject regexes: English and German positives and negatives, including
  security-code subjects not matching Notifications.
- Learning: moved to another folder, moved to INBOX, not found, missing
  Message-ID.
- Sent trust: first run window, incremental UIDs, user rule precedence.
- AI: stub model, threshold, invalid JSON, budget cap, encrypted-key skip.
- Run: dry run moves nothing, lock file, UIDVALIDITY reset, cutoff date.
- Undo: single row, whole run, sender retrained.
- CLI flags and web app endpoints.
