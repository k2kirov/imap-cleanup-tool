"""AI fallback for auto-sort: one LLM call per unknown sender, strict JSON.

Only sender, subjects and header flags are sent. Message bodies never leave
the mailbox.
"""

from __future__ import annotations

import json

from . import ai, llm
from .rulepacks import CATEGORIES

ALLOWED = frozenset((*CATEGORIES, "inbox"))
SAMPLE_SIZE = 5
SYSTEM_PROMPT = (
    "You sort email for a busy person. For one sender you get up to five recent "
    "subjects and header flags. Pick exactly one category: inbox (a person wrote "
    "it, or it needs action or attention), social (social network activity), "
    "news (newsletters, mailing lists), promotions (marketing, sales), "
    "notifications (automated app or service notices), receipts (orders, "
    "invoices, shipping), cc (the person is only copied). When unsure, pick "
    "inbox with low confidence. Reply with JSON only: "
    '{"category":"...","confidence":0.0,"reason":"..."}')


class Skip(Exception):
    """The AI layer cannot run; the message says why."""


def load_model(name: str) -> dict:
    if not name:
        raise Skip("No AI model is set for auto-sort.")
    try:
        cfg = llm.load_model(name)
    except llm.LLMError as exc:
        raise Skip(str(exc)) from exc
    if cfg.get("encrypted"):
        raise Skip("Encrypted model configs can't run unattended.")
    return cfg


def _payload(sender: str, rows: list[dict]) -> str:
    return json.dumps({"sender": sender, "messages": [
        {"subject": r["subject"], "flags": sorted(r["signals"])}
        for r in rows[:SAMPLE_SIZE]]}, ensure_ascii=False)


def validate(content: str) -> dict:
    raw = ai._extract_json(content)
    if not isinstance(raw, dict):
        raise ValueError("reply is not a JSON object")
    category = str(raw.get("category", "")).strip().lower()
    if category not in ALLOWED:
        raise ValueError(f"unknown category {category!r}")
    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise ValueError("confidence is not a number") from exc
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence is out of range")
    return {"category": category, "confidence": confidence,
            "reason": str(raw.get("reason", ""))[:200]}


def classify_senders(groups: dict[str, list[dict]], cfg: dict, *, max_calls: int,
                     litellm=None) -> tuple[dict[str, dict], list[str]]:
    """Ask the model about each sender, up to ``max_calls`` calls."""
    if litellm is None:
        try:
            import litellm  # pylint: disable=import-outside-toplevel,redefined-outer-name
        except ImportError as exc:
            raise Skip("Install the [ai] extra to use the AI layer.") from exc
    base = {"model": cfg["model"], "timeout": ai.LLM_TIMEOUT}
    if cfg.get("api_key"):
        base["api_key"] = cfg["api_key"]
    if cfg.get("api_base"):
        base["api_base"] = cfg["api_base"]
    verdicts: dict[str, dict] = {}
    errors: list[str] = []
    for sender in sorted(groups)[:max(0, int(max_calls))]:
        # Build request outside try so payload/config errors surface immediately.
        kwargs = dict(base, messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _payload(sender, groups[sender])}])
        try:
            # Catch provider/network errors and bad replies; they must not stop the run.
            resp = ai._call_once(litellm, kwargs)
            usage = getattr(resp, "usage", None)
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            cost = ai._batch_cost(cfg, prompt_tokens, completion_tokens)
            if cost is not None:
                llm.log_cost(cfg["name"], prompt_tokens, completion_tokens, cost)
            verdicts[sender] = validate(resp.choices[0].message.content)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            errors.append(f"{sender}: {exc}")
    return verdicts, errors
