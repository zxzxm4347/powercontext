"""Replay the 碎锚酒馆 memory flow against a running PowerContext server.

Mirrors the exact HTTP Request nodes in the Dify Chatflow:
`/v1/scopes`, `/v1/context/prepare`, `/v1/memory/entries/list`, and `/v1/memory/remember`.

Usage:
    python dify/scripts/pc_http_demo.py --base-url http://127.0.0.1:8000 --player shen
    python dify/scripts/pc_http_demo.py --player shen --reset
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

TURN_ONE = [
    {
        "kind": "offense",
        "text": "冒险者在碎锚酒馆吧台前故意打翻橡木麦酒桶，往麦酒里倒了一大把粗盐还拒不赔钱（原话：我往你的橡木麦酒桶里倒了一把粗盐，一文钱也不赔，你能拿我怎样？）",
        "reason": "玩家原话：我往你的橡木麦酒桶里倒了一把粗盐，一文钱也不赔，你能拿我怎样？",
    },
]
TURN_TWO = [
    {
        "kind": "favor",
        "text": "冒险者主动拎来三坛陈年朗姆酒向老巴恩赔礼道歉，并补齐了此前毁坏麦酒桶的金币（原话：上次往麦酒桶倒粗盐是我混账，今天拎来三坛陈年朗姆酒赔罪，酒钱双倍奉上。）",
        "reason": "玩家原话：上次往麦酒桶倒粗盐是我混账，今天拎来三坛陈年朗姆酒赔罪，酒钱双倍奉上。",
    },
]


ACTIVE_SERVER_URL: str | None = None


def ensure_powercontext_available(base_url: str) -> str:
    global ACTIVE_SERVER_URL
    if ACTIVE_SERVER_URL:
        return ACTIVE_SERVER_URL
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/health/ready", timeout=2) as resp:
            if resp.status == 200:
                ACTIVE_SERVER_URL = base_url
                return base_url
    except Exception:
        pass
    import run_powercontext

    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8000
    adapter = run_powercontext.get_global_adapter(prefer_seekdb=True)
    try:
        srv = run_powercontext.start_http_server(host, port, adapter)
    except OSError:
        srv = run_powercontext.start_http_server("127.0.0.1", 0, adapter)
        port = srv.server_address[1]
        base_url = f"http://127.0.0.1:{port}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    ACTIVE_SERVER_URL = base_url
    return base_url


class Client:
    def __init__(self, base_url: str) -> None:
        self.base_url = ensure_powercontext_available(base_url).rstrip("/")

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            raise SystemExit(f"POST {path} -> {error.code}\n{detail}") from error
        return json.loads(raw) if raw else {}

    def health(self) -> dict[str, Any]:
        with urllib.request.urlopen(self.base_url + "/health/ready", timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))


def log(title: str, data: Any) -> None:
    print(f"\n### {title}")
    print(json.dumps(data, ensure_ascii=False, indent=2))


def render_context(prepared: dict[str, Any]) -> str:
    if prepared.get("status") == "empty":
        return "（账本还是空的，这位客人第一次来）"
    return prepared.get("content") or ""


def ensure_scope(client: Client, player_key: str) -> dict[str, Any]:
    return client.post(
        "/v1/scopes",
        {
            "title": f"碎锚酒馆 · {player_key}",
            "summary": f"碎锚酒馆老板老巴恩的恩怨账本，记录玩家 {player_key} 在此留下的每一笔恶作剧、欠账与人情。",
            "idempotency_key": f"grudge-inn-{player_key}",
        },
    )


def turn(client: Client, scope_id: str, memory: dict[str, Any]) -> dict[str, Any]:
    prepared = client.post(
        "/v1/context/prepare",
        {"scope_id": scope_id, "query": memory["reason"], "max_bytes": 3000},
    )
    listed = client.post("/v1/memory/entries/list", {"scope_id": scope_id})
    written = client.post(
        "/v1/memory/remember",
        {"scope_id": scope_id, "kind": memory["kind"], "text": memory["text"], "reason": memory["reason"]},
    )
    return {"prepare": prepared, "list_before": listed, "remember": written}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--player", default="shen", help="player id typed into the Chatflow")
    parser.add_argument("--reset", action="store_true", help="retire existing entries before the run")
    args = parser.parse_args(argv[1:])

    client = Client(args.base_url)
    health = client.health()
    log("health/ready", health)

    scope = ensure_scope(client, args.player)
    log("scopes (idempotent create)", scope)
    scope_id = scope["scope_id"]
    if args.reset:
        retired = 0
        while True:
            listed = client.post("/v1/memory/entries/list", {"scope_id": scope_id})
            active = listed.get("entries") or []
            if not active:
                break
            client.post(
                "/v1/memory/entries/retire",
                {"scope_id": scope_id, "citation": active[0]["citation"], "reason": "demo reset"},
            )
            retired += 1
        log("reset", {"retired": retired})

    first = turn(client, scope_id, TURN_ONE[0])
    log("turn 1 · prepare (first visit)", first["prepare"])
    log("turn 1 · list before", {"entries": len(first["list_before"].get("entries") or [])})
    log("turn 1 · remember", first["remember"])

    second = turn(client, scope_id, TURN_TWO[0])
    log("turn 2 · prepare (second visit)", second["prepare"])
    log("turn 2 · list before", {"entries": len(second["list_before"].get("entries") or [])})
    log("turn 2 · remember", second["remember"])

    content = render_context(second["prepare"]) + render_context(first["prepare"])
    ok = (
        first["prepare"].get("status") in {"empty", "ready"}
        and len(second["list_before"].get("entries") or []) >= 1
        and "麦酒桶" in content
    )
    print("\n### verdict")
    if ok:
        print("OK: the second turn retrieved the first turn's memory from PowerContext.")
        print(f"prepare.content_bytes = {second['prepare'].get('content_bytes')}")
        return 0
    print("FAILED: the second turn did not surface the first turn's memory.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
