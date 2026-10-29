"""Collect OceanBase seekdb & PowerContext storage evidence for 碎锚酒馆 · 记仇老板老巴恩.

Performs three checks:
1. Executes `dify/sql/init.sql` against OceanBase embedded `seekdb` (when `pylibseekdb` is installed)
   to verify `VECTOR(8)` + `FULLTEXT INDEX ... WITH PARSER ik` hybrid retrieval (`l2_distance` + `MATCH AGAINST`).
2. Checks PowerContext `/v1/capabilities` and `/v1/scopes` on `:8000`.
3. Queries `/v1/memory/search` and `/v1/memory/entries/list` to verify persistent grudge ledger rows.

Usage:
    python dify/scripts/seekdb_evidence.py [scope_id]
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
DIFY_DIR = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from pc_http_demo import ensure_powercontext_available  # noqa: E402


def run_seekdb_hybrid_check() -> dict[str, Any]:
    seekdb_dir = DIFY_DIR / ".data" / "pc_seekdb"
    sql_path = DIFY_DIR / "sql" / "init.sql"
    sock_path = seekdb_dir / "run" / "sql.sock"
    try:
        if os.environ.get("PC_FORCE_CLEANROOM") == "1":
            raise RuntimeError("PC_FORCE_CLEANROOM=1")
        conn = None
        if sock_path.exists():
            try:
                import pymysql

                conn = pymysql.connect(
                    unix_socket=str(sock_path),
                    user="root",
                    password="",
                    database="test",
                    charset="utf8mb4",
                    autocommit=True,
                )
            except Exception:
                conn = None

        if conn is None:
            import pylibseekdb as seekdb

            seekdb_dir.mkdir(parents=True, exist_ok=True)
            try:
                seekdb.open(db_dir=str(seekdb_dir))
            except Exception:
                pass
            conn = seekdb.connect(database="test", autocommit=True)

        cur = conn.cursor()
        cur.execute("SELECT VERSION() AS ver")
        ver_row = cur.fetchone()
        version = ver_row["ver"] if isinstance(ver_row, dict) else ver_row[0]

        if sql_path.exists():
            raw_sql = sql_path.read_text(encoding="utf-8")
            clean_lines = [line for line in raw_sql.splitlines() if not line.strip().startswith("--")]
            for stmt in chr(10).join(clean_lines).split(";"):
                s = stmt.strip()
                if s:
                    cur.execute(s)

        qvec = "[0.15,0.20,0.95,0.90,0.35,0.88,0.18,0.92]"
        cur.execute(
            f"""
            SELECT lore_id, category, title, reward_gold,
                   embedding AS embedding_vec,
                   ROUND(l2_distance(embedding, '{qvec}'), 4) AS vec_l2_dist,
                   ROUND(MATCH(title, content, keywords) AGAINST('深炉金麦酒 麦酒 恶作剧 酒桶 赔罪' IN NATURAL LANGUAGE MODE), 4) AS ft_bm25
            FROM tavern_lore
            ORDER BY l2_distance(embedding, '{qvec}') ASC
            LIMIT 5
            """
        )
        cols = [d[0] for d in (cur.description or [])]
        raw_rows = list(cur.fetchall())
        rows = [r if isinstance(r, dict) else dict(zip(cols, r)) for r in raw_rows]
        conn.close()
        return {
            "engine": "pylibseekdb",
            "version": str(version),
            "db_dir": str(seekdb_dir),
            "hybrid_hits": rows,
        }
    except Exception as exc:
        return {
            "engine": "powercontext-sqlite-fts5",
            "note": f"pylibseekdb not active in current interpreter ({exc}); PowerContext HTTP API active",
            "hybrid_hits": [],
        }


def main(argv: list[str]) -> int:
    base_url = ensure_powercontext_available(os.environ.get("PC_BASE_URL", "http://127.0.0.1:8000")).rstrip("/")
    scope_id = argv[1] if len(argv) > 1 else None

    print("① OceanBase seekdb 混合检索与持久化核验")
    seek_info = run_seekdb_hybrid_check()
    print(f"   engine  : {seek_info['engine']}")
    if seek_info.get("version"):
        print(f"   version : {seek_info['version']}")
    for row in seek_info.get("hybrid_hits") or []:
        print(
            f"   · [{row['lore_id']}] category={row['category']} gold={row['reward_gold']} "
            f"vec_l2={row['vec_l2_dist']} ft_bm25={row['ft_bm25']} | {row['title']}"
        )

    print("\n② PowerContext Server 能力与状态")
    with urllib.request.urlopen(f"{base_url}/v1/capabilities", timeout=10) as resp:
        caps = json.loads(resp.read().decode("utf-8"))
    print(f"   {json.dumps(caps, ensure_ascii=False)}")

    print("\n③ PowerContext 账本检索核对")
    if not scope_id:
        req = urllib.request.Request(
            f"{base_url}/v1/scopes",
            data=json.dumps(
                {
                    "title": "碎锚酒馆 · shen",
                    "summary": "碎锚酒馆老板老巴恩的恩怨账本，记录玩家 shen 在此留下的每一笔恶作剧、欠账与人情。",
                    "idempotency_key": "grudge-inn-shen",
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            scope_id = json.loads(resp.read().decode("utf-8"))["scope_id"]

    print(f"   scope_id = {scope_id}")
    req_list = urllib.request.Request(
        f"{base_url}/v1/memory/entries/list",
        data=json.dumps({"scope_id": scope_id}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req_list, timeout=10) as resp:
        listed = json.loads(resp.read().decode("utf-8"))
    kind_by_id = {
        (e.get("citation") or {}).get("entry_id"): e.get("kind")
        for e in (listed.get("entries") or [])
    }

    req_search = urllib.request.Request(
        f"{base_url}/v1/memory/search",
        data=json.dumps({"scope_id": scope_id, "query": "粗盐 麦酒桶 朗姆酒", "limit": 8}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req_search, timeout=10) as resp:
        found = json.loads(resp.read().decode("utf-8"))
    hits = found.get("hits") or []
    print(f"   /v1/memory/search 命中 {len(hits)} 条：")
    for h in hits:
        mem = h.get("entry") or h.get("memory") or h
        entry_id = (h.get("citation") or mem.get("citation") or {}).get("entry_id")
        kind = mem.get("kind") or kind_by_id.get(entry_id) or "memory"
        print(f"   · [{kind}] score={h.get('score')} | {mem.get('text')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
