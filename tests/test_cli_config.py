"""Regression tests for CLI configuration precedence and AI scan scope."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from imap_cleanup_tool import cli, scheduler


class CliConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def write_config(self, name, data):
        path = Path(self.tmp.name) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_explicit_default_values_override_config(self):
        path = self.write_config("settings.json", {
            "port": 143, "timeout": 30, "scan_mode": "full",
            "batch_size": 10, "ai_threshold": 8.0, "ai_sample": 2,
        })
        args = cli.parse_args([
            "--config", str(path), "--port", "993", "--timeout", "120",
            "--scan-mode", "search", "--batch-size", "500",
            "--ai-threshold", "6", "--ai-sample", "5",
        ])
        self.assertEqual((args.port, args.timeout, args.scan_mode,
                          args.batch_size, args.ai_threshold, args.ai_sample),
                         (993, 120, "search", 500, 6.0, 5))

    def test_profile_config_applies_before_boolean_defaults(self):
        self.write_config("profiles/p.json", {
            "move": True, "dest_folder": "Trash", "dry_run": True,
            "local_cache": True, "rules": ["sender contains x"],
        })
        with mock.patch.object(scheduler, "config_dir",
                               return_value=Path(self.tmp.name)):
            args = cli.parse_args(["--profile", "p"])
        self.assertTrue(args.move)
        self.assertTrue(args.dry_run)
        self.assertTrue(args.local_cache)
        self.assertEqual(args.rule, "(sender contains x)")

    def test_ai_scan_all_does_not_run_rule_cleanup(self):
        config = self.write_config("settings.json", {
            "rules": ["sender contains x"], "ai_scan_all": True,
        })
        with (mock.patch.object(cli.importlib.util, "find_spec", return_value=object()),
              mock.patch.object(cli.core, "connect", return_value=object()),
              mock.patch.object(cli.core, "safe_logout"),
              mock.patch.object(cli, "_run_operation") as operation,
              mock.patch.object(cli, "_run_ai", return_value=0) as run_ai):
            code = cli.main([
                "--config", str(config), "--host", "h", "--user", "u",
                "--password", "p", "--ai-cleanup", "--ai-model", "model",
                "--yes",
            ])
        self.assertEqual(code, 0)
        operation.assert_not_called()
        run_ai.assert_called_once()

    def test_saved_profile_move_and_no_cache_reach_operation(self):
        self.write_config("profiles/p.json", {
            "move": True, "dest_folder": "Trash",
            "rules": ["sender contains x"],
        })
        profile = {
            "host": "h", "port": 993, "user": "u", "password": "p",
            "timeout": 120, "local_cache": True, "auth_method": "password",
        }
        with (mock.patch.object(scheduler, "config_dir",
                                return_value=Path(self.tmp.name)),
              mock.patch("imap_cleanup_tool.profiles.load_profile",
                         return_value=profile),
              mock.patch.object(cli.core, "connect", return_value=object()),
              mock.patch.object(cli.core, "safe_logout"),
              mock.patch.object(cli, "_run_operation") as operation):
            code = cli.main(["--profile", "p", "--no-cache", "--yes"])
        self.assertEqual(code, 0)
        args = operation.call_args.args[1]
        self.assertTrue(args.move)
        self.assertEqual(args.dest_folder, "Trash")
        self.assertFalse(args.local_cache)

    def test_obsolete_review_requires_report_only(self):
        with mock.patch.object(cli.importlib.util, "find_spec", return_value=object()):
            code = cli.main(["--ai-cleanup", "--ai-review-obsolete"])
        self.assertEqual(code, 2)

    def test_config_can_supply_obsolete_examples(self):
        path = self.write_config("settings.json", {
            "ai_obsolete_examples": ["LinkedIn profile-view alerts"],
        })
        args = cli.parse_args(["--config", str(path)])
        self.assertEqual(args.ai_obsolete_example,
                         ["LinkedIn profile-view alerts"])


if __name__ == "__main__":
    unittest.main()
