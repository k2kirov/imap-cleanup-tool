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
