"""Vendored rule packs: conversion, merge priority and domain lookup."""

import importlib.util
import unittest
from pathlib import Path
from unittest import mock

from imap_cleanup_tool import rulepacks

ROOT = Path(__file__).resolve().parent.parent


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "update_rulepacks", ROOT / "scripts" / "update_rulepacks.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConvertTests(unittest.TestCase):
    def test_convert_keeps_only_allow_entries(self):
        script = _load_script()
        files = {
            "social": {"domains": [
                {"match": "Discord.com", "kind": "allow", "scope": "subdomains"},
                {"match": "linkedin.com", "kind": "ceded", "scope": "subdomains",
                 "ceded_to": "work"},
                {"match": "faceb00k.com", "kind": "block", "scope": "exact"},
                {"match": "example.tld", "kind": "tld-example", "scope": "exact"},
                {"match": "discord.com", "kind": "allow", "scope": "subdomains"},
            ]},
            "invoice": {"domains": [
                {"match": "paddle.com", "kind": "allow", "scope": "exact"}]},
        }
        out = script.convert("abc123", files)
        self.assertEqual(out["commit"], "abc123")
        self.assertEqual(out["license"], "CC0-1.0")
        self.assertEqual(out["categories"], {
            "social": [{"match": "discord.com", "scope": "subdomains"}],
            "receipts": [{"match": "paddle.com", "scope": "exact"}],
        })


class DomainCategoryTests(unittest.TestCase):
    def tearDown(self):
        rulepacks._tables.cache_clear()
        rulepacks._groups.cache_clear()
        rulepacks._terms_regex.cache_clear()

    def test_vendored_packs_load(self):
        self.assertEqual(rulepacks.domain_category("promo.newegg.com")[0],
                         "promotions")
        self.assertEqual(rulepacks.domain_category("mail.facebookmail.com")[0],
                         "social")
        self.assertEqual(rulepacks.domain_category("dhl.de")[0], "receipts")
        self.assertEqual(rulepacks.domain_category("news.substack.com")[0], "news")
        self.assertIsNone(rulepacks.domain_category("facebookmail.com.evil.test"))
        self.assertIsNone(rulepacks.domain_category(""))

    def _fake(self, poli):
        fake = {
            "inpector_rules.json": {"groups": [
                {"name": "Shopping", "domains": ["shop.test"],
                 "subject_contains": []}]},
            "poli0981_rules.json": {"categories": poli},
            "scrothers_rules.json": {"categories": {}},
        }
        return mock.patch.object(rulepacks, "_load", side_effect=lambda n: fake[n])

    def test_receipts_beat_promotions_for_same_domain(self):
        with self._fake({"receipts": [{"match": "shop.test", "scope": "subdomains"}]}), \
             mock.patch.object(rulepacks, "_BUILTIN_SOCIAL", ()):
            rulepacks._tables.cache_clear()
            self.assertEqual(rulepacks.domain_category("a.shop.test"),
                             ("receipts", "poli0981 receipts"))

    def test_exact_scope_does_not_match_subdomains(self):
        with self._fake({"social": [{"match": "only.test", "scope": "exact"}]}), \
             mock.patch.object(rulepacks, "_BUILTIN_SOCIAL", ()):
            rulepacks._tables.cache_clear()
            self.assertEqual(rulepacks.domain_category("only.test"),
                             ("social", "poli0981 social"))
            self.assertIsNone(rulepacks.domain_category("a.only.test"))


class SubjectTermTests(unittest.TestCase):
    def test_protected_group_uses_word_boundaries(self):
        self.assertEqual(rulepacks.protected_group(
            "example.com", "Your one-time password"), "Security")
        self.assertEqual(rulepacks.protected_group("bahn.de", "Newsletter"),
                         "Travelling")
        self.assertIsNone(rulepacks.protected_group(
            "example.com", "Spotify weekly mix"))

    def test_receipt_subject_terms(self):
        self.assertTrue(rulepacks.receipt_subject("Ihre Bestellung ist unterwegs"))
        self.assertFalse(rulepacks.receipt_subject("Border control news"))


if __name__ == "__main__":
    unittest.main()
