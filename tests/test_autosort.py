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


    def test_newer_user_rule_is_not_reverted_by_an_older_move(self):
        box = FakeMailbox(("INBOX", "INBOX.News", "INBOX.Promotions"))
        box.add("INBOX", sender="n@brand.test", subject="s", message_id="<m5@x>")
        with mock.patch.object(sortstore, "_now", return_value="2026-09-20T10:00:00+00:00"):
            self.log("INBOX.News", "<m5@x>")
        with mock.patch.object(sortstore, "_now", return_value="2026-09-21T10:00:00+00:00"):
            sortstore.save_rule(A, "n@brand.test", "promotions", "user")
        self.assertEqual(autosort.learn(box, A, now=NOW), 0)
        rule = sortstore.rules(A)["n@brand.test"]
        self.assertEqual((rule["category"], rule["source"]), ("promotions", "user"))

    def test_older_user_rule_is_updated_by_a_newer_move(self):
        box = FakeMailbox(("INBOX", "INBOX.News", "INBOX.Promotions"))
        box.add("INBOX", sender="n@brand.test", subject="s", message_id="<m6@x>")
        with mock.patch.object(sortstore, "_now", return_value="2026-09-19T10:00:00+00:00"):
            sortstore.save_rule(A, "n@brand.test", "news", "user")
        with mock.patch.object(sortstore, "_now", return_value="2026-09-20T10:00:00+00:00"):
            self.log("INBOX.News", "<m6@x>")
        self.assertEqual(autosort.learn(box, A, now=NOW), 1)
        self.assertEqual(sortstore.rules(A)["n@brand.test"]["category"], "inbox")

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


    def test_undo_refuses_unsafe_message_id_without_searching(self):
        box = FakeMailbox(("INBOX", "INBOX.News"))
        for bad in ('<a"b@x>', "<a\\b@x>", "<a@x>\r\nX", "<a@x>\nX"):
            move_id = self.log("INBOX.News", bad)
            with self.assertRaisesRegex(ValueError, "unusual Message-ID"):
                autosort.undo_move(box, A, move_id)
        self.assertFalse(any(c[0] == "SEARCH" for c in box.calls))

NEWS = "List-Id: <l.test>\r\nList-Post: <mailto:l@l.test>\r\n"
CFG = {"name": "local", "model": "ollama/llama3", "api_base": "", "api_key": "",
       "encrypted": False, "track_costs": False, "cost_input": 0, "cost_output": 0}


class FakeLiteLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def completion(self, **kwargs):
        from types import SimpleNamespace
        self.calls.append(kwargs)
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

    def test_missing_litellm_is_reported_not_raised(self):
        sortstore.update_settings(A, ai_model="local")
        box = FakeMailbox()
        box.add("INBOX", sender="hello@shop.test", subject="Hello", message_id="<l1@x>")
        with mock.patch.object(llm, "load_model", return_value=CFG), \
                mock.patch.object(ai_sort, "find_spec", return_value=None):
            result = autosort.run(box, A, now=NOW)
        self.assertIn("[ai]", result.ai_note)
        self.assertEqual(sortstore.get_state(A, "INBOX")["last_uid"], 1)

    def test_skip_from_classify_is_reported_not_raised(self):
        sortstore.update_settings(A, ai_model="local")
        box = FakeMailbox()
        box.add("INBOX", sender="hello@shop.test", subject="Hello", message_id="<l2@x>")
        skip = ai_sort.Skip("Install the [ai] extra to use the AI layer.")
        with mock.patch.object(ai_sort, "load_model", return_value=CFG), \
                mock.patch.object(ai_sort, "classify_senders", side_effect=skip):
            result = autosort.run(box, A, now=NOW)
        self.assertIn("[ai]", result.ai_note)
        self.assertEqual(len(box.folders["INBOX"]), 1)
        self.assertEqual(sortstore.get_state(A, "INBOX")["last_uid"], 1)

    def test_zero_ai_budget_turns_ai_off_and_advances_cursor(self):
        sortstore.update_settings(A, ai_model="local", ai_max_calls=0)
        box = FakeMailbox()
        box.add("INBOX", sender="a@one.test", subject="Hello", message_id="<z1@x>")
        box.add("INBOX", sender="b@two.test", subject="Hello", message_id="<z2@x>")
        fake = FakeLiteLLM([])
        with mock.patch.object(ai_sort, "load_model", return_value=CFG):
            result = autosort.run(box, A, now=NOW, litellm=fake)
        self.assertEqual(fake.calls, [])
        self.assertIn("AI calls per run is 0", result.ai_note)
        self.assertNotIn("AI budget", " ".join(result.skipped))
        self.assertEqual(sortstore.get_state(A, "INBOX")["last_uid"], 2)

    def test_folded_message_id_is_normalized_and_learnable(self):
        box = FakeMailbox(("INBOX", "INBOX.News"))
        box.add("INBOX", sender="writer@substack.com", subject="Issue F",
                extra="Message-ID:\r\n <fold@x>\r\n")
        box.add("INBOX", sender="writer2@substack.com", subject="Issue C",
                extra="Message-ID: (note) <comment@x>\r\n")
        self.assertEqual(autosort.run(box, A, now=NOW).moved, 2)
        self.assertEqual(sorted(m["message_id"] for m in sortstore.moves(A)),
                         ["<comment@x>", "<fold@x>"])
        # The user drags the folded one back to INBOX.
        uid = next(u for u, m in box.folders["INBOX.News"].items()
                   if "fold@x" in m["header"])
        box.folders["INBOX"]["99"] = box.folders["INBOX.News"].pop(uid)
        self.assertEqual(autosort.learn(box, A, now=NOW), 1)
        self.assertEqual(sortstore.rules(A)["writer@substack.com"]["category"], "inbox")

    def test_sender_awaiting_review_is_not_asked_again(self):
        sortstore.update_settings(A, ai_model="local")
        sortstore.add_review(A, "maybe@odd.test", "news", 0.4, "unsure")
        box = FakeMailbox()
        box.add("INBOX", sender="maybe@odd.test", subject="Hi", message_id="<rv1@x>")
        fake = FakeLiteLLM([])
        with mock.patch.object(ai_sort, "load_model", return_value=CFG):
            result = autosort.run(box, A, now=NOW, litellm=fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual(result.moved, 0)
        self.assertEqual(len(box.folders["INBOX"]), 1)
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

    def test_failed_move_is_skipped_and_retried_next_run(self):
        box = FakeMailbox()
        box.add("INBOX", sender="writer@substack.com", subject="Issue A", message_id="<fm1@x>")
        box.add("INBOX", sender="writer2@substack.com", subject="Issue B", message_id="<fm2@x>")
        real_move_uid = triage.move_uid
        calls = {"n": 0}

        def fake_move_uid(conn, uid, destination):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("boom")
            return real_move_uid(conn, uid, destination)

        with mock.patch.object(triage, "move_uid", side_effect=fake_move_uid):
            result = autosort.run(box, A, now=NOW)
        self.assertEqual(result.moved, 1)
        self.assertIn("boom", " ".join(result.skipped))
        self.assertEqual(sortstore.get_state(A, "INBOX")["last_uid"], 0)

        with mock.patch.object(triage, "move_uid", side_effect=fake_move_uid):
            second = autosort.run(box, A, now=NOW)
        self.assertEqual(second.moved, 1)

    def test_missing_move_and_uidplus_raises_before_any_moves(self):
        box = FakeMailbox(capabilities=("IMAP4REV1",))
        box.add("INBOX", sender="writer@substack.com", subject="Issue", message_id="<mv1@x>")
        with self.assertRaisesRegex(ValueError, "MOVE or UIDPLUS for auto-sort"):
            autosort.run(box, A, now=NOW)
        self.assertEqual(len(box.folders["INBOX"]), 1)
        result = autosort.run(box, A, dry_run=True, now=NOW)
        self.assertEqual([p["folder"] for p in result.planned], ["INBOX.News"])

    def test_dry_run_previews_whole_inbox_before_first_real_run(self):
        sortstore.update_settings(A, started_at=None)
        box = FakeMailbox()
        box.add("INBOX", sender="writer@substack.com", subject="Old issue",
                message_id="<pv1@x>", received=datetime(2026, 8, 1, tzinfo=timezone.utc))
        result = autosort.run(box, A, dry_run=True, now=NOW)
        self.assertEqual([p["folder"] for p in result.planned], ["INBOX.News"])
        self.assertIsNone(sortstore.get_settings(A)["started_at"])
        self.assertEqual(sortstore.rules(A), {})
        self.assertIsNone(sortstore.get_state(A, "INBOX"))
        self.assertIsNone(sortstore.get_state(A, "sent:Sent"))
        self.assertEqual(sortstore.moves(A), [])
        self.assertEqual(sortstore.reviews(A), [])

    def test_dry_run_previews_sent_trust_without_saving(self):
        box = FakeMailbox()
        box.add("Sent", sender=A, subject="Hi", to="friend@x.test")
        box.add("INBOX", sender="friend@x.test", subject="Newsletter", message_id="<pv2@x>",
                extra=NEWS)
        result = autosort.run(box, A, dry_run=True, now=NOW)
        self.assertEqual(result.trusted, 1)
        self.assertEqual([p["folder"] for p in result.planned], [])
        self.assertEqual(sortstore.rules(A), {})

    def test_dry_run_lists_ai_pending_without_calling_model(self):
        sortstore.update_settings(A, ai_model="local")
        box = FakeMailbox()
        box.add("INBOX", sender="hello@shop.test", subject="Hello", message_id="<pv3@x>")
        fake = FakeLiteLLM([])
        with mock.patch.object(ai_sort, "load_model", return_value=CFG):
            result = autosort.run(box, A, now=NOW, dry_run=True, litellm=fake)
        self.assertEqual(result.ai_pending, ["hello@shop.test"])
        self.assertEqual(sortstore.reviews(A), [])
        self.assertEqual(sortstore.rules(A), {})


if __name__ == "__main__":
    unittest.main()
