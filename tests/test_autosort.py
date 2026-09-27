"""Auto-sort runs against a multi-folder IMAP fake."""

import imaplib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from imap_cleanup_tool import ai_sort, autosort, core, llm, scheduler, sortstore, triage
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

    def test_user_rule_provenance_kept_when_category_matches(self):
        box = FakeMailbox(("INBOX", "INBOX.News", "INBOX.Promotions"))
        box.add("INBOX", sender="n@brand.test", subject="s", message_id="<m4@x>")
        self.log("INBOX.News", "<m4@x>")
        sortstore.save_rule(A, "n@brand.test", "inbox", "user")
        self.assertEqual(autosort.learn(box, A, now=NOW), 0)
        self.assertEqual(sortstore.rules(A)["n@brand.test"]["source"], "user")


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

    def test_undo_run_reports_imap_errors_without_crashing(self):
        box = FakeMailbox(("INBOX", "INBOX.News"))
        for mid in ("<e1@x>", "<e2@x>"):
            box.add("INBOX.News", sender="n@brand.test", subject="s", message_id=mid)
            self.log("INBOX.News", mid, run="runY")
        real_move_uid = triage.move_uid

        def fake_move_uid(conn, uid, destination):
            fake_move_uid.calls += 1
            if fake_move_uid.calls == 1:
                raise imaplib.IMAP4.error("down")
            return real_move_uid(conn, uid, destination)

        fake_move_uid.calls = 0
        with mock.patch.object(triage, "move_uid", side_effect=fake_move_uid):
            result = autosort.undo_run(box, A, "runY")
        self.assertEqual(result["undone"], 1)
        self.assertEqual(len(result["errors"]), 1)


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


if __name__ == "__main__":
    unittest.main()
