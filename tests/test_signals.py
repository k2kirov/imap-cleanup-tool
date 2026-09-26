"""Header-only category signals and subject patterns."""

import unittest

from imap_cleanup_tool import signals

ME = "me@example.com"


class DetectTests(unittest.TestCase):
    def test_list_and_bulk_headers(self):
        sig = signals.detect({"List-Id": "<dev.lists.example.org>",
                              "List-Post": "<mailto:dev@lists.example.org>",
                              "Precedence": "list"},
                             "dev@lists.example.org", ME)
        self.assertTrue({"list_id", "list_post", "bulk"} <= sig)

    def test_marketing_esp_and_one_click(self):
        sig = signals.detect({"X-Kmail-Message": "x",
                              "List-Unsubscribe": "<https://u.test>",
                              "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
                             "shop@brand.test", ME)
        self.assertTrue({"marketing_esp", "list_unsubscribe", "one_click"} <= sig)

    def test_campaign_feedback_id(self):
        self.assertIn("campaign_feedback", signals.detect(
            {"X-Feedback-ID": "12:34:campaign:ESP"}, "a@b.test", ""))
        self.assertNotIn("campaign_feedback", signals.detect(
            {"Feedback-ID": "a:b:c:SenderID"}, "a@b.test", ""))

    def test_auto_submitted_no_is_ignored(self):
        self.assertIn("auto_submitted", signals.detect(
            {"Auto-Submitted": "auto-generated"}, "a@b.test", ""))
        self.assertNotIn("auto_submitted", signals.detect(
            {"Auto-Submitted": "no"}, "a@b.test", ""))

    def test_cc_only(self):
        self.assertIn("cc_only", signals.detect(
            {"To": "Boss <boss@example.com>", "Cc": "Me <ME@example.com>"},
            "boss@example.com", ME))
        self.assertNotIn("cc_only", signals.detect(
            {"To": ME, "Cc": ME}, "x@y.test", ME))

    def test_newsletter_platform_and_noreply(self):
        self.assertIn("newsletter_platform",
                      signals.detect({}, "writer@mail.substack.com", ""))
        self.assertIn("noreply", signals.detect({}, "no-reply@service.test", ""))
        self.assertNotIn("noreply", signals.detect({}, "anna@service.test", ""))

    def test_fastmail_category(self):
        self.assertIn("fastmail_receipts", signals.detect(
            {"X-ME-VSCategory": "Purchases"}, "a@b.test", ""))


class SubjectPatternTests(unittest.TestCase):
    def test_receipts(self):
        for subject in ("Your order #123 has shipped", "Ihre Rechnung Nr. 42",
                        "Zahlungsbestätigung", "Out for delivery"):
            self.assertTrue(signals.RECEIPTS.search(subject), subject)
        self.assertIsNone(signals.RECEIPTS.search("Lunch tomorrow?"))

    def test_notifications_do_not_match_security_codes(self):
        self.assertTrue(signals.NOTIFICATIONS.search("Build passed"))
        self.assertTrue(signals.NOTIFICATIONS.search("Neue Nachricht von Anna"))
        self.assertIsNone(signals.NOTIFICATIONS.search(
            "Your security code is 123456"))

    def test_promotions(self):
        self.assertTrue(signals.PROMOTIONS.search("20% off everything"))
        self.assertTrue(signals.PROMOTIONS.search("Rabatt nur heute"))
        self.assertIsNone(signals.PROMOTIONS.search("Meeting notes"))


if __name__ == "__main__":
    unittest.main()
