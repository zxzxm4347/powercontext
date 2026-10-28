#!/usr/bin/env python3
"""从运行中的 PowerContext HTTP 服务审计指定 Scope 的记忆账目、版本变更历史与 Prepared Context。

用法：
    python dify/scripts/memory_audit.py --base-url http://127.0.0.1:8000 [--scope-id scp_...]
    python dify/scripts/memory_audit.py --from-evidence
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DIFY_DIR = Path(__file__).resolve().parents[1]
if str(DIFY_DIR) not in sys.path:
    sys.path.insert(0, str(DIFY_DIR))

from backend.app import EVIDENCE_LOG_PATH, http_get_json, http_post_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--scope-id", default="")
    parser.add_argument("--query", default="辣椒粉 麦酒 赔罪 银鳞龙 委托")
    parser.add_argument("--from-evidence", action="store_true", help="直接从最近一次实测证据文件输出完整 PowerContext 审计报告")
    args = parser.parse_args(argv)

    if args.from_evidence or not args.scope_id:
        if EVIDENCE_LOG_PATH.is_file():
            data = json.loads(EVIDENCE_LOG_PATH.read_text(encoding="utf-8"))
            print("=== PowerContext Memory Audit Report (Live Execution Evidence) ===")
            print(f"Scope ID       : {data.get('powercontext_scope_id')}")
            print(f"SeekDB Engine  : {data.get('architecture', {}).get('hybrid_search_db')}")
            audit = data.get("powercontext_final_audit", {})
            entries = audit.get("entries", {}).get("entries", [])
            revs = audit.get("changes", {}).get("revisions", [])
            print(f"Active Entries : {len(entries)}")
            for idx, e in enumerate(entries, 1):
                cit = e.get("citation", {})
                print(
                    f"  [{idx}] entry_id={cit.get('entry_id')} | ver={cit.get('entry_version_id')} "
                    f"| rev={cit.get('memory_ref', {}).get('revision')} | kind={e.get('kind')} | text={e.get('text')}"
                )
            print(f"Revision History ({len(revs)} revisions):")
            print(json.dumps(audit.get("changes", {}), ensure_ascii=False, indent=2))
            return 0

    base = args.base_url.rstrip("/")
    scope_id = args.scope_id
    if not scope_id:
        scopes = http_get_json(f"{base}/v1/scopes").get("items", [])
        if not scopes:
            raise SystemExit("No scopes found in PowerContext server.")
        scope_id = scopes[0]["scope_id"]

    entries = http_post_json(f"{base}/v1/memory/entries/list", {"scope_id": scope_id})
    changes = http_post_json(f"{base}/v1/memory/changes", {"scope_id": scope_id})
    prepared = http_post_json(f"{base}/v1/context/prepare", {"scope_id": scope_id, "query": args.query, "max_bytes": 4000})

    print(json.dumps({"scope_id": scope_id, "entries": entries, "changes": changes, "prepared_context": prepared}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
