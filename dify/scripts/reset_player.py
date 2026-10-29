"""Retire every active entry in one guest's ledger so the verification script can be re-run.

PowerContext keeps immutable revision history: retiring entries leaves the scope clean
for `prepare` and `entries/list` while preserving prior revisions in `/v1/memory/changes`.

Usage:
    python dify/scripts/reset_player.py --player shen
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from memory_audit import post  # noqa: E402
from pc_http_demo import ensure_powercontext_available  # noqa: E402


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--player", default="shen")
    args = parser.parse_args(argv[1:])

    base_url = ensure_powercontext_available(args.base_url)
    player = args.player.strip() or "guest"
    scope = post(
        base_url,
        "/v1/scopes",
        {
            "title": f"碎锚酒馆 · {player}",
            "summary": f"碎锚酒馆老板老巴恩的恩怨账本，记录玩家 {player} 在此留下的每一笔恶作剧、欠账与人情。",
            "idempotency_key": f"grudge-inn-{player}",
        },
    )
    scope_id = scope["scope_id"]
    retired = 0
    while True:
        listed = post(base_url, "/v1/memory/entries/list", {"scope_id": scope_id})
        active = listed.get("entries") or []
        if not active:
            break
        post(
            base_url,
            "/v1/memory/entries/retire",
            {"scope_id": scope_id, "citation": active[0]["citation"], "reason": "reset for verification"},
        )
        retired += 1
    print(f"scope={scope_id} retired={retired}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
