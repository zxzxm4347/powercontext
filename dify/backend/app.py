"""Companion verification runner and PowerContext + seekdb service launcher for 碎锚酒馆.

Usage:
    python dify/backend/app.py --verify
    python dify/backend/app.py --port 8000
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

DIFY_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = DIFY_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def run_verify() -> int:
    import memory_audit
    import pc_http_demo
    import replay_chatflow
    import reset_player
    import seekdb_evidence
    import validate_dsl

    print("=== [1/5] Validate Dify Chatflow DSL ===")
    rc = validate_dsl.main(["validate_dsl.py", str(DIFY_DIR / "dsl" / "adventure_npc_chatflow.yml")])
    if rc != 0:
        return rc

    print("\n=== [2/5] Reset & Run PowerContext HTTP Demo (player=shen) ===")
    reset_player.main(["reset_player.py", "--player", "shen"])
    rc = pc_http_demo.main(["pc_http_demo.py", "--player", "shen", "--reset"])
    if rc != 0:
        return rc

    print("\n=== [3/5] Replay 12-Node Dify Chatflow Graph Across 4 Sessions (player=shen) ===")
    reset_player.main(["reset_player.py", "--player", "shen"])
    turns = [
        ("我往你的橡木麦酒桶里倒了一把粗盐，一文钱也不赔，你能拿我怎样？", DIFY_DIR / "samples" / "judge" / "offense.json"),
        ("我又来了，上次那桶加了粗盐的麦酒味道如何？", DIFY_DIR / "samples" / "judge" / "offense.json"),
        ("上次往麦酒桶倒粗盐是我混账，今天拎来三坛陈年朗姆酒赔罪，酒钱双倍奉上。", DIFY_DIR / "samples" / "judge" / "favor.json"),
        ("老巴恩，你这碎锚酒馆开了多少年了？今晚码头有什么传闻？", DIFY_DIR / "samples" / "judge" / "neutral.json"),
    ]
    for q, jf in turns:
        rc = replay_chatflow.main(
            [
                "replay_chatflow.py",
                "--player",
                "shen",
                "--query",
                q,
                "--judge-file",
                str(jf),
            ]
        )
        if rc != 0:
            return rc

    print("\n=== [4/5] Audit PowerContext Ledger & Revision History (player=shen) ===")
    rc = memory_audit.main(["memory_audit.py", "--player", "shen"])
    if rc != 0:
        return rc

    print("\n=== [5/5] Verify OceanBase seekdb & PowerContext Storage Evidence ===")
    rc = seekdb_evidence.main(["seekdb_evidence.py"])
    import run_powercontext

    if run_powercontext.GLOBAL_ADAPTER is not None:
        run_powercontext.GLOBAL_ADAPTER.close()
    return rc


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="Run full end-to-end verification suite")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv[1:])

    if args.verify:
        return run_verify()

    import run_powercontext

    return run_powercontext.main(["run_powercontext.py", "--host", args.host, "--port", str(args.port)])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
