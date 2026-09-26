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
