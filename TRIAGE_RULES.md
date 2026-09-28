# Obsolete inbox review rules

The CLI uses these rules when `--ai-review-obsolete` is set. This mode requires
`--ai-cleanup --ai-report-only`. It cannot move or delete mail.

## Candidate selection

The normal heuristic score remains one way to select a sender. The extra mode
also selects senders whose subjects clearly name a social activity alert,
newsletter, webinar, course promotion, or special offer. It also selects a
routine service alert when its dated header is more than 180 days old and its
subject names a completed or failed task, backup, or sync, a scheduled
maintenance notice, or a routine status report. A generic "update", "alert",
or "notification" does not qualify. A protected subject
from the same sender blocks this extra selection route. The code sends the
selected sender and up to `--ai-sample` subjects to the saved model.

Selection means **review**, not approval to delete. The score, unread ratio,
sender domain, and `List-Unsubscribe` header cannot prove that a message is
obsolete.

## Model decision rules

1. Mark `delete=true` only when the sampled messages clearly show low-value
   bulk mail. User-approved examples from `ai_obsolete_examples` apply only
   in report-only obsolete review mode.
2. A read message can still be obsolete. Reading status is not a keep rule.
3. Keep sign-in codes, password resets, security alerts, orders, billing,
   receipts, shipping, appointments, travel, legal, medical, and personal mail.
   Keep unresolved incidents, outages, and support cases.
4. Keep a sender if its samples mix low-value and protected mail. Keep it when
   the samples do not cover enough of that sender's messages.
5. Do not treat an old date alone as proof. Review old calendar invitations
   separately, even when their event date has passed.
6. `delete=true` refers to matching messages in the selected folders. It does
   not block future mail from that sender.
7. Return one strict JSON verdict per sender. Give a short reason and a
   confidence value. When uncertain, return `delete=false`.

## Safety boundary

This mode remains report-only because the current model decides at sender
level. A sender can mix message types. The report carries only header data,
not message bodies or proof of sender authenticity. Review individual messages
before moving them.

This mode writes a CSV report. It does not add review candidates to the
actionable Spam addresses list.

The report marks a timed-out or invalid model answer as `delete=false` with a
manual-review reason. Other sender results remain in the report.

Only a saved affirmative model verdict may skip a later model call. A sender
that the heuristic selected without a model verdict does not count as
confirmed spam.

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
