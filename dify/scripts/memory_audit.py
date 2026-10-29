"""Print one adventurer's grudge ledger and its revision history from PowerContext.

Resolves the same `idempotency_key` (`grudge-inn-<player>`) used by the Dify Chatflow's
`pc_scope` node, then prints `/v1/memory/entries/list` and `/v1/memory/changes`.

Usage:
    python dify/scripts/memory_audit.py --player shen
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from pc_http_demo import ensure_powercontext_available  # noqa: E402


def post(base_url: str, path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        raise SystemExit(f"POST {path} -> HTTP {error.code}\n{body}") from error


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
    print(f"## scope\n{json.dumps(scope, ensure_ascii=False, indent=2)}")

    entries = post(base_url, "/v1/memory/entries/list", {"scope_id": scope_id})
    print(f"## entries ({len(entries.get('entries') or [])})\n{json.dumps(entries, ensure_ascii=False, indent=2)}")

    changes = post(base_url, "/v1/memory/changes", {"scope_id": scope_id, "since_revision": 0})
    print(f"## changes\n{json.dumps(changes, ensure_ascii=False, indent=2)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
