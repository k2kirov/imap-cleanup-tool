"""Regenerate src/imap_cleanup_tool/poli0981_rules.json from a pinned commit.

Dev tool only: it needs PyYAML and network access. The app never downloads
rules at runtime.

Usage: python scripts/update_rulepacks.py <commit-sha>
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

REPO = "poli0981/proton-sieve-filters"
# poli0981 category file -> auto-sort category.
CATEGORY_MAP = {"social": "social", "news": "news", "invoice": "receipts",
                "shipping": "receipts", "shopping": "promotions"}
OUT = (Path(__file__).resolve().parent.parent / "src" / "imap_cleanup_tool"
       / "poli0981_rules.json")


def _fetch(commit: str, name: str) -> dict:
    import yaml  # dev-only dependency

    url = (f"https://raw.githubusercontent.com/{REPO}/{commit}"
           f"/data/categories/{name}.yml")
    with urllib.request.urlopen(url, timeout=30) as resp:
        return yaml.safe_load(resp.read().decode("utf-8"))


def convert(commit: str, files: dict[str, dict]) -> dict:
    """Keep only ``kind: allow`` domains; ``ceded``, ``block`` and examples go."""
    categories: dict[str, list[dict]] = {}
    for name, data in files.items():
        target = CATEGORY_MAP[name]
        for entry in data.get("domains", []):
            if entry.get("kind") != "allow":
                continue
            categories.setdefault(target, []).append({
                "match": entry["match"].strip().lower(),
                "scope": entry.get("scope", "subdomains")})
    for target, entries in categories.items():
        unique = {(e["match"], e["scope"]): e for e in entries}
        categories[target] = sorted(unique.values(),
                                    key=lambda e: (e["match"], e["scope"]))
    return {"source": f"https://github.com/{REPO}", "commit": commit,
            "license": "CC0-1.0",
            "scope": "Sender domains only, from data/categories/*.yml. "
                     "Keyword groups are not used.",
            "categories": categories}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    commit = argv[1]
    files = {name: _fetch(commit, name) for name in CATEGORY_MAP}
    OUT.write_text(json.dumps(convert(commit, files), indent=2,
                              ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
