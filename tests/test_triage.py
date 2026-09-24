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
        self.assertFalse(any(c[0] in {"MOVE", "STORE", "COPY"} for c in conn.calls))

    def test_sender_rule_learns_but_protected_subject_stays_inbox(self):
        triage.train_sender("me@example.com", "security-noreply@linkedin.com",
                            "social")
        rules = triage.sender_rules("me@example.com")
        conn = TriageConn()
        rows = triage.preview(conn, "me@example.com")["rows"]
        self.assertEqual(rows[1]["category"], "inbox")
        self.assertEqual(rules["security-noreply@linkedin.com"], "social")
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
