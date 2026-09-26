"""AI fallback: strict verdicts, budget, cost logging, model checks."""

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from imap_cleanup_tool import ai_sort, llm

CFG = {"name": "local", "model": "ollama/llama3", "api_base": "", "api_key": "",
       "encrypted": False, "track_costs": True, "cost_input": 1.0, "cost_output": 2.0}


class FakeLiteLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def completion(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.replies.pop(0)))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))


def groups():
    return {"b@y.test": [{"subject": "Hi", "signals": frozenset()}],
            "a@x.test": [{"subject": "Issue 1", "signals": frozenset({"list_id"})}]}


class ValidateTests(unittest.TestCase):
    def test_accepts_wrapped_json(self):
        self.assertEqual(ai_sort.validate(
            'Sure: {"category": "News", "confidence": 0.9, "reason": "list"}'),
            {"category": "news", "confidence": 0.9, "reason": "list"})

    def test_rejects_bad_answers(self):
        for content in ("no json", '{"category": "trash", "confidence": 0.9}',
                        '{"category": "news", "confidence": 1.4}',
                        '{"category": "news", "confidence": "high"}'):
            with self.assertRaises(ValueError, msg=content):
                ai_sort.validate(content)


class ClassifyTests(unittest.TestCase):
    def test_one_call_per_sender_and_cost_logged(self):
        fake = FakeLiteLLM(['{"category":"news","confidence":0.9,"reason":"list"}',
                            '{"category":"inbox","confidence":0.7,"reason":"person"}'])
        with mock.patch.object(llm, "log_cost") as log_cost:
            verdicts, errors = ai_sort.classify_senders(groups(), CFG, max_calls=50,
                                                        litellm=fake)
        self.assertEqual(errors, [])
        self.assertEqual(verdicts["a@x.test"]["category"], "news")
        self.assertEqual(verdicts["b@y.test"]["category"], "inbox")
        self.assertEqual(len(fake.calls), 2)
        payload = json.loads(fake.calls[0]["messages"][1]["content"])
        self.assertEqual(payload, {"sender": "a@x.test", "messages": [
            {"subject": "Issue 1", "flags": ["list_id"]}]})
        log_cost.assert_called_with("local", 10, 5, 2e-05)

    def test_budget_limits_calls(self):
        fake = FakeLiteLLM(['{"category":"news","confidence":0.9,"reason":"x"}'])
        with mock.patch.object(llm, "log_cost"):
            verdicts, _ = ai_sort.classify_senders(groups(), CFG, max_calls=1,
                                                   litellm=fake)
        self.assertEqual(list(verdicts), ["a@x.test"])

    def test_bad_reply_is_an_error_not_a_verdict(self):
        fake = FakeLiteLLM(["nonsense", '{"category":"cc","confidence":0.8,"reason":"x"}'])
        with mock.patch.object(llm, "log_cost"):
            verdicts, errors = ai_sort.classify_senders(groups(), CFG, max_calls=5,
                                                        litellm=fake)
        self.assertEqual(list(verdicts), ["b@y.test"])
        self.assertTrue(errors[0].startswith("a@x.test:"))


class LoadModelTests(unittest.TestCase):
    def test_skips(self):
        with self.assertRaisesRegex(ai_sort.Skip, "No AI model"):
            ai_sort.load_model("")
        with mock.patch.object(llm, "load_model", return_value={**CFG, "encrypted": True}):
            with self.assertRaisesRegex(ai_sort.Skip, "Encrypted"):
                ai_sort.load_model("local")
        with mock.patch.object(llm, "load_model", side_effect=llm.LLMError("No model config")):
            with self.assertRaisesRegex(ai_sort.Skip, "No model config"):
                ai_sort.load_model("gone")
        with mock.patch.object(llm, "load_model", return_value=CFG):
            self.assertEqual(ai_sort.load_model("local"), CFG)


if __name__ == "__main__":
    unittest.main()
