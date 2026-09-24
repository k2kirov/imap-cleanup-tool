"""Proof that inbox triage stays header-only and moves only matched UIDs."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from imap_cleanup_tool import triage


class TriageConn:
    capabilities = ("IMAP4REV1", "MOVE")

    def __init__(self):
        self.messages = {
            "1": ("messages-noreply@linkedin.com", "5 people viewed your profile",
                  "Wed, 23 Sep 2026 19:54:20 +0000"),
            "2": ("security-noreply@linkedin.com", "Sign in alert for your account",
                  "Mon, 30 May 2022 07:10:01 +0000"),
            "3": ("offers@example.com", "Special offer for a course",
                  "Mon, 1 Jan 2024 10:00:00 +0000"),
        }
        self.calls = []
        self.validity = b"42"

    def select(self, folder, readonly=False):
        self.calls.append(("SELECT", folder, readonly))
        return "OK", [b"3"]

    def response(self, name):
        return name, [self.validity]

    def list(self):
        return "OK", [b'(\\HasNoChildren) "." "INBOX"',
                      b'(\\HasNoChildren) "." "INBOX.Social"',
                      b'(\\HasNoChildren) "." "INBOX.Other"']

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "SEARCH":
            return "OK", [b"1 2 3"]
        if command == "FETCH":
            parts = []
            for raw in args[0].split(b","):
                uid = raw.decode()
                if uid not in self.messages:
                    continue
                sender, subject, date = self.messages[uid]
                header = (f"From: <{sender}>\r\nSubject: {subject}\r\n"
                          f"Date: {date}\r\n\r\n").encode()
                parts.append((f"1 (UID {uid} BODY[] {{{len(header)}}})".encode(),
                              header))
            return "OK", parts
        if command == "MOVE":
            del self.messages[args[0].decode()]
            return "OK", [b"moved"]
        if command == "COPY":
            return "OK", [b"copied"]
        if command == "STORE":
            return "OK", [b"flagged"]
        if command == "EXPUNGE":
            del self.messages[args[0].decode()]
            return "OK", [b"expunged"]
        raise AssertionError(command)


class TriageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patch = mock.patch.object(triage, "rules_path",
                                  return_value=Path(self.temp.name) / "rules.sqlite")
        patch.start()
        self.addCleanup(patch.stop)

    def test_preview_classifies_social_other_and_protected(self):
        conn = TriageConn()
        report = triage.preview(conn, "me@example.com")
        self.assertEqual(report["uidvalidity"], "42")
        self.assertEqual({r["uid"]: r["category"] for r in report["rows"]},
                         {"1": "social", "2": "inbox", "3": "other"})
        self.assertIn("Jobs and Recruting", report["rows"][0]["source_matches"])
        self.assertEqual(report["source_coverage"]["commit"],
                         "fe5a0ce637504c6baf70514f13f369f09d234de0")
        self.assertFalse(any(c[0] in {"MOVE", "STORE", "COPY"} for c in conn.calls))
        self.assertTrue(all("BODY.PEEK[HEADER.FIELDS" in str(c)
                            for c in conn.calls if c[0] == "FETCH"))

    def test_sieve_source_social_domains_need_an_activity_subject(self):
        domains = ("facebook.com", "flickr.com", "instagram.com",
                   "pinterest.com", "reddit.com", "tiktok.com",
                   "tumblr.com", "twitter.com", "x.com")
        for domain in domains:
            with self.subTest(domain=domain):
                row = {"sender": f"notice@updates.{domain}",
                       "subject": "Someone commented on your post", "date": ""}
                self.assertEqual(triage.classify(row),
                                 ("social", "From (address) matches social domain; Subject matches activity"))
                row["subject"] = "New terms of service"
                self.assertEqual(triage.classify(row)[0], "inbox")

    def test_spamsieve_match_rule_fields_and_styles(self):
        row = {"sender": "News@Sub.Example.com", "subject": "Your New Follower"}
        cases = (
            ("From (address)", "Is Equal to", "news@sub.example.com", True),
            ("From (address)", "Ends with", "@sub.example.com", True),
            ("From (address)", "Starts with", "news@", True),
            ("Subject", "Contains", "new follower", True),
            ("Subject", "Matches Regex", r"\bnew follower\b", True),
            ("From (address)", "Ends with", "@example.com", False),
        )
        for field, style, value, expected in cases:
            with self.subTest(field=field, style=style, value=value):
                self.assertEqual(triage.MatchRule(field, style, value).matches(row),
                                 expected)
        with self.assertRaisesRegex(ValueError, "location"):
            triage.MatchRule("Body", "Contains", "test").matches(row)
        with self.assertRaisesRegex(ValueError, "match style"):
            triage.MatchRule("Subject", "Sounds like", "test").matches(row)

    def test_social_domain_match_has_a_label_boundary(self):
        row = {"sender": "notice@fakefacebook.com",
               "subject": "Someone commented on your post", "date": ""}
        self.assertEqual(triage.classify(row)[0], "inbox")

    def test_social_security_subject_stays_inbox(self):
        row = {"sender": "notice@updates.reddit.com",
               "subject": "Security alert: someone commented on your post",
               "date": ""}
        self.assertEqual(triage.classify(row),
                         ("inbox", "Protected subject or source rule"))

    def test_inpector_header_rules_protect_important_mail(self):
        cases = (("notice@banking.n26.com", "Your news", "Finances"),
                 ("info@dhl.com", "Parcel on the way", "Deliveries"),
                 ("notice@okta.com", "New device", "Security"),
                 ("info@booking.com", "Your trip", "Travelling"))
        for sender, subject, source in cases:
            with self.subTest(source=source):
                row = {"sender": sender, "subject": subject, "date": ""}
                self.assertIn(source, triage.source_matches(row))
                self.assertEqual(triage.classify(row)[0], "inbox")

    def test_inpector_leisure_and_list_rules_only_suggest_other(self):
        leisure = {"sender": "news@twitch.tv", "subject": "Live stream today",
                   "date": ""}
        self.assertEqual(triage.classify(leisure),
                         ("other", "inpector Free Time rule"))
        mailing = {"sender": "news@example.com", "subject": "Monthly notes",
                   "date": "", "list_id": True}
        self.assertEqual(triage.classify(mailing),
                         ("other", "inpector Mailinglists rule"))
        mailing["subject"] = "Your payment receipt"
        self.assertEqual(triage.classify(mailing)[0], "inbox")

    def test_inpector_mailing_list_header_variants(self):
        for line in ("List-Unsubscribe-Post: List-Unsubscribe=One-Click",
                     "X-BeenThere: list@example.com",
                     "Precedence: list",
                     "To: members@lists.example.com"):
            with self.subTest(header=line):
                header = ("From: news@example.com\r\nSubject: Monthly notes\r\n"
                          + line + "\r\n\r\n").encode()
                row = triage._parse_part((b"1 (UID 8 BODY[])", header))
                self.assertIn("Mailinglists", triage.source_matches(row))

    def test_inpector_domain_match_rejects_lookalike(self):
        row = {"sender": "news@faketwitch.tv", "subject": "Live stream today",
               "date": ""}
        self.assertNotIn("Free Time", triage.source_matches(row))
        self.assertEqual(triage.classify(row)[0], "inbox")

    def test_security_hints_are_review_only(self):
        header = (b"From: Customer Support <notice@paypaI-alert.xyz>\r\n"
                  b"Subject: Verify your account - invoice #123456\r\n"
                  b"Authentication-Results: mx.example; dmarc=fail; spf=fail\r\n"
                  b"X-Spam-Score: 6\r\n\r\n")
        row = triage._parse_part((b"1 (UID 8 BODY[])", header))
        hints = triage.review_hints(row)
        self.assertIn("Block common spam tlds", hints)
        self.assertIn("Common Spam: spam headers", hints)
        self.assertIn("Common Spam: DMARC alignment failure", hints)
        self.assertIn("Common Spam: SPF-only failure", hints)
        self.assertIn("Common Spam: Typosquatted brand domains", hints)
        self.assertIn("Common Spam: Phishing subject lines", hints)
        self.assertIn("Common Spam: Impersonated support addresses", hints)
        self.assertIn("Common Spam: Invoice number pattern", hints)
        self.assertEqual(triage.classify(row)[0], "inbox")

    def test_security_hints_do_not_trust_missing_or_passing_auth_headers(self):
        row = {"sender": "notice@example.com", "subject": "A regular note",
               "date": "", "review_headers": {}}
        self.assertEqual(triage.review_hints(row), [])
        row["review_headers"] = {"authentication-results":
            "mx.example; dmarc=fail; dmarc=pass; spf=fail; dkim=pass"}
        self.assertNotIn("Common Spam: DMARC alignment failure",
                         triage.review_hints(row))
        self.assertNotIn("Common Spam: SPF-only failure",
                         triage.review_hints(row))

    def test_shipping_and_attachment_hints_keep_sender_boundaries(self):
        header = (b"From: <alerts@fakedhl.com>\r\n"
                  b"Subject: DHL package\r\n"
                  b"Content-Disposition: attachment; filename=run.js\r\n\r\n")
        row = triage._parse_part((b"1 (UID 8 BODY[])", header))
        self.assertIn("Common Spam: False Shipping notifications",
                      triage.review_hints(row))
        self.assertIn("Filter malicous attachments (top-level header only)",
                      triage.review_hints(row))
        row["sender"] = "alerts@dhl.com"
        self.assertNotIn("Common Spam: False Shipping notifications",
                         triage.review_hints(row))

    def test_source_coverage_reports_checks_that_need_more_than_headers(self):
        names = {entry["name"] for entry in triage.source_coverage()["not_evaluated"]}
        self.assertIn("PDF Bills", names)
        self.assertIn("Filter abused standard addresses", names)
        self.assertIn("Last Rule CatchAll", names)

    def test_sender_rule_learns_but_protected_subject_stays_inbox(self):
        triage.train_sender("me@example.com", "security-noreply@linkedin.com",
                            "social")
        rules = triage.sender_rules("me@example.com")
        conn = TriageConn()
        rows = triage.preview(conn, "me@example.com")["rows"]
        self.assertEqual(rows[1]["category"], "inbox")
        self.assertEqual(rules["security-noreply@linkedin.com"], "social")
        self.assertEqual(triage.sender_rule_definitions("me@example.com"), [{
            "match_field": "From (address)", "match_style": "Is Equal to",
            "text_to_match": "security-noreply@linkedin.com", "category": "social"}])
        triage.forget_sender("me@example.com", "security-noreply@linkedin.com")
        self.assertEqual(triage.sender_rules("me@example.com"), {})

    def test_calendar_invite_stays_inbox(self):
        category, _ = triage.classify({
            "sender": "events@example.com", "date": "Mon, 1 Jan 2024 10:00:00 +0000",
            "subject": "Invitation: Webinar next Tuesday"})
        self.assertEqual(category, "inbox")

    def test_medical_digest_stays_inbox(self):
        category, _ = triage.classify({
            "sender": "updates@clinic.example", "date": "Mon, 1 Jan 2024 10:00:00 +0000",
            "subject": "Weekly digest of your medical test results"})
        self.assertEqual(category, "inbox")

    def test_move_checks_uid_and_headers(self):
        conn = TriageConn()
        row = triage.preview(conn, "me@example.com")["rows"][0]
        with self.assertRaisesRegex(ValueError, "Scan again"):
            triage.move_checked(conn, uid="1", uidvalidity="42", sender=row["sender"],
                                subject="wrong", date=row["date"], category="social")
        self.assertIn("1", conn.messages)
        folder = triage.move_checked(conn, uid="1", uidvalidity="42",
                                     sender=row["sender"], subject=row["subject"],
                                     date=row["date"], category="social")
        self.assertEqual(folder, "INBOX.Social")
        self.assertNotIn("1", conn.messages)

    def test_move_rejects_stale_uidvalidity_and_trash(self):
        conn = TriageConn()
        row = triage.preview(conn, "me@example.com")["rows"][0]
        with self.assertRaisesRegex(ValueError, "Choose Social or Other"):
            triage.move_checked(conn, uid="1", uidvalidity="42", sender=row["sender"],
                                subject=row["subject"], date=row["date"],
                                category="trash")
        with self.assertRaisesRegex(ValueError, "Inbox identity changed"):
            triage.move_checked(conn, uid="1", uidvalidity="old", sender=row["sender"],
                                subject=row["subject"], date=row["date"],
                                category="social")
        self.assertIn("1", conn.messages)

    def test_server_without_safe_move_is_rejected_before_copy(self):
        conn = TriageConn()
        conn.capabilities = ("IMAP4REV1",)
        row = triage.preview(conn, "me@example.com")["rows"][0]
        with self.assertRaisesRegex(ValueError, "MOVE or UIDPLUS"):
            triage.move_checked(conn, uid="1", uidvalidity="42", sender=row["sender"],
                                subject=row["subject"], date=row["date"],
                                category="social")
        self.assertFalse(any(c[0] == "COPY" for c in conn.calls))

    def test_uidplus_fallback_expunges_only_the_chosen_uid(self):
        conn = TriageConn()
        conn.capabilities = ("IMAP4REV1", "UIDPLUS")
        row = triage.preview(conn, "me@example.com")["rows"][0]
        triage.move_checked(conn, uid="1", uidvalidity="42", sender=row["sender"],
                            subject=row["subject"], date=row["date"],
                            category="social")
        self.assertIn(("EXPUNGE", b"1"), conn.calls)
        self.assertEqual(set(conn.messages), {"2", "3"})
