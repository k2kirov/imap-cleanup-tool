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
            conn.execute("INSERT INTO sender_rule VALUES (?, ?, ?)",
                         (A, "c@d.test", "other"))
            conn.commit()
        self.assertEqual(sortstore.rules(A), {
            "a@b.test": {"category": "social", "source": "user", "confidence": None},
            "c@d.test": {"category": "promotions", "source": "user", "confidence": None}})
        self.assertEqual(triage.sender_rules(A)["c@d.test"], "promotions")

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
