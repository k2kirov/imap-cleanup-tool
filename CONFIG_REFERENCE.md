# IMAP Cleanup Tool - Configuration Reference (`config.json`)

You can place `config.json` in your workspace directory (auto-detected), or pass any custom file using `--config <path>`.

---

## Complete Configuration Schema

```json
{
  // ==========================================
  // 1. Connection & Profile
  // ==========================================
  "profile": "forkpoint",            // Name of a saved connection profile (loads host, port, user, password)
  "host": "mail.forkpoint.com",      // (Optional if profile is set) IMAP server hostname
  "port": 993,                       // (Optional if profile is set) IMAP port (993 for SSL/TLS)
  "user": "user@example.com",        // (Optional if profile is set) IMAP username
  "password": "secret_password",     // (Optional if profile is set) Plaintext IMAP password
  "timeout": 120,                    // Socket timeout in seconds (default: 120)

  // ==========================================
  // 2. Folder Targeting
  // ==========================================
  "folders": ["INBOX"],              // Array of folders to scan, e.g. ["INBOX", "INBOX.Archive"]

  // ==========================================
  // 3. Actions (Move vs Delete vs Trash)
  // ==========================================
  "move": true,                      // If true, moves matching emails instead of deleting them
  "dest_folder": "INBOX.Trash",      // Target destination folder when move: true (e.g. "INBOX.spam" or "INBOX.Trash")
  "gmail_trash": false,              // If true, applies Gmail's Trash label instead of permanent deletion
  "empty_folder": false,             // If true, empties all messages in the targeted folder(s)
  "expunge": false,                  // If true, immediately issues IMAP EXPUNGE after flagging \Deleted

  // ==========================================
  // 4. Rule-Based Filtering
  // ==========================================
  // Multiple rules are automatically combined with OR.
  // Fields: sender, subject, date, body, text, to, cc, bcc.
  // Operators: contains, is, starts, ends (for date).
  "rules": [
    "subject contains 'Confirm your Mailbox'",
    "subject contains 'MAIL EXPIRY'",
    "subject contains 'Mailbox password is due'",
    "subject contains 'administrator policy'",
    "subject contains 'Confirm your account'",
    "subject contains 'Re-validate your'",
    "subject contains 'Action Required: Mailbox'",
    "sender contains 'administrator-mailbox@forkpoint.com'",
    "sender contains 'ITsupport@forkpoint.com'",
    "body contains 'password notice'",
    "text contains 'has restrictions'"
  ],

  // ==========================================
  // 5. Target Addresses & Domains
  // ==========================================
  "targets": [
    // Array of email addresses or @domains to match, or a string path to a targets.txt file
    // "spam@spammer.com",
    // "@marketing-agency.com"
  ],
  "include_subdomains": false,       // If true, matching @domain.com also matches @sub.domain.com
  "scan_mode": "search",             // "search" (fast, server-side IMAP search) or "full" (downloads headers locally)

  // ==========================================
  // 6. AI Cleanup (LLM Evaluation)
  // ==========================================
  "ai_cleanup": false,               // If true, enables AI triage to classify bulk/junk senders
  "ai_scan_all": true,               // If true, AI triage scans the ENTIRE folder instead of being restricted to matching "rules"
  "ai_review_obsolete": false,        // Add clear social/marketing hints and routine service alerts older than 180 days; report-only required
  "ai_obsolete_examples": ["LinkedIn profile-view alerts"], // User-approved low-value examples for this account's report
  "ai_model": "qwen3.5-9b-mlx",      // Name of saved LLM model config (e.g. "qwen3.5-9b-mlx", "gpt-4o-mini", "ollama-llama3")
  "ai_threshold": 6.0,               // Heuristic spam score threshold 0.0-10.0 (default: 6.0)
  "ai_sample": 5,                    // Number of sample message subjects sent to the LLM per sender (default: 5)
  "ai_exclude": [],                  // List of sender addresses to exclude from AI cleanup
  "ai_include_self": false,          // If true, includes your own email address in the AI report (default: false)
  "ai_weights": {                    // Custom weights for the 5 heuristic signals:
    "list_unsubscribe": 3.5,         // Weight for List-Unsubscribe header
    "unread_ratio": 3.0,             // Weight for percentage of unread emails
    "bulk": 1.5,                     // Weight for Precedence: bulk header
    "sender_pattern": 1.0,           // Weight for noreply/newsletter/marketing sender localparts
    "frequency": 1.0                 // Weight for sender frequency (messages/week)
  },
  "ai_report_only": false,           // If true, produces the AI report without moving or deleting anything
  "ai_report_csv": "ai_report.csv",  // File path to save the Excel-friendly AI triage CSV report
  "ai_flag_spam": false,             // If true, moves 1 message per sender to Spam to train server filters
  "ai_no_check_spam": false,         // If true, re-evaluates even senders with a saved affirmative model verdict

  // ==========================================
  // 7. Email Notifications
  // ==========================================
  "notify_profile": "forkpoint",     // Name of saved SMTP profile to send completion report emails from

  // ==========================================
  // 8. Performance, Cache & Execution
  // ==========================================
  "local_cache": true,               // Cache message headers locally in SQLite for repeat scans
  "clear_cache": false,              // Wipe cached headers before scanning
  "batch_size": 500,                 // Batch size for IMAP operations (default: 500)
  "dry_run": false,                  // If true, shows what would happen without altering emails
  "yes": false,                      // If true, skips interactive [y/N] confirmation prompt
  "verbose": false                   // If true, enables detailed debug logging
}
```

---

## How to Run with the Config

* **Standard run (auto-loads `config.json`):**
  ```bash
  .venv/bin/imap-cleanup-tool
  ```

* **Dry-run preview:**
  ```bash
  .venv/bin/imap-cleanup-tool --dry-run
  ```

* **Automated execution without confirmation prompt:**
  ```bash
  .venv/bin/imap-cleanup-tool --yes
  ```

* **Using a specific config file:**
  ```bash
  .venv/bin/imap-cleanup-tool --config path/to/custom-config.json
  ```

---

## Observing the Inbox

* **Live folder status (Total, Unread, Recent):**
  ```bash
  .venv/bin/imap-cleanup-tool --status
  ```

* **Census of all senders across the entire inbox (unfiltered):**
  ```bash
  .venv/bin/imap-cleanup-tool --list-senders
  ```

* **Full Inbox AI Report without deleting anything:**
  ```bash
  .venv/bin/imap-cleanup-tool --ai-cleanup --ai-report-only --ai-scan-all
  ```

* **Review clear obsolete subjects with the local model:**
  ```bash
  .venv/bin/imap-cleanup-tool --ai-cleanup --ai-report-only --ai-scan-all --ai-review-obsolete --ai-no-check-spam
  ```
  This mode makes a report only. It never moves or deletes mail.

* **Bypass config rules on demand:**
  ```bash
  .venv/bin/imap-cleanup-tool --no-rules
  ```
