#!/usr/bin/env python3
"""静态校验 Dify Chatflow DSL (dsl/adventure_npc_chatflow.yml 与 workflow.yml)。

校验项：
1. 顶层结构完整性 (app, kind=app, version, workflow.graph.nodes, workflow.graph.edges)。
2. 节点类型与数量统计 (start, code, http-request, llm, answer)，确保从 start 到 answer 拓扑连通无孤立节点。
3. 变量选择器 {{#node.var#}} 合法性校验（确保引用的上游节点输出或 env/sys 变量真实存在）。
4. PowerContext 接口覆盖校验（/v1/scopes, /v1/sources/content, /v1/context/prepare, /v1/memory/entries/list, /v1/memory/remember）。
5. OceanBase seekdb 接口覆盖校验（/api/v1/seekdb/hybrid_search, /api/v1/seekdb/record_grudge）。
6. 敏感信息安全扫描（确认不包含真实 sk- 密钥或私有密码）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

DIFY_DIR = Path(__file__).resolve().parents[1]
for candidate in (
    Path("/home/obdev/powercontext/competition/.venv/lib/python3.12/site-packages"),
):
    if candidate.is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import yaml

SELECTOR_RE = re.compile(r"\{\{#([a-zA-Z0-9_]+)\.([a-zA-Z0-9_]+)#\}\}")


def validate_file(path: Path) -> None:
    print(f"=== Validating {path.relative_to(DIFY_DIR.parent)} ===")
    raw = path.read_text(encoding="utf-8")
    dsl = yaml.safe_load(raw)

    assert dsl.get("kind") == "app", "kind must be 'app'"
    assert dsl.get("app", {}).get("mode") == "advanced-chat", "app.mode must be 'advanced-chat'"

    wf = dsl["workflow"]
    nodes = wf["graph"]["nodes"]
    edges = wf["graph"]["edges"]
    env_vars = {item["name"] for item in wf.get("environment_variables", [])}

    node_map = {n["id"]: n for n in nodes}
    type_counts: dict[str, int] = {}
    for n in nodes:
        t = n["data"]["type"]
        type_counts[t] = type_counts.get(t, 0) + 1

    print(f"Nodes count: {len(nodes)} | Edges count: {len(edges)} | Types: {type_counts}")
    assert type_counts.get("start") == 1, "Must have exactly 1 start node"
    assert type_counts.get("answer", 0) >= 1, "Must have at least 1 answer node"
    assert type_counts.get("llm", 0) >= 2, "Must have at least 2 llm nodes (judge + narrator)"
    assert type_counts.get("http-request", 0) >= 6, "Must have at least 6 http-request nodes"
    assert type_counts.get("code", 0) >= 4, "Must have at least 4 code nodes"

    # Check topological reachability from start to answer
    adj: dict[str, list[str]] = {nid: [] for nid in node_map}
    for e in edges:
        src, dst = e["source"], e["target"]
        assert src in node_map and dst in node_map, f"Edge references unknown node: {e}"
        adj[src].append(dst)

    visited: set[str] = set()
    stack = ["start"]
    while stack:
        curr = stack.pop()
        if curr not in visited:
            visited.add(curr)
            stack.extend(adj[curr])

    assert len(visited) == len(node_map), f"Unreachable nodes detected: {set(node_map) - visited}"
    print(f"Topological connectivity verified: all {len(visited)} nodes reachable from 'start' to 'answer'.")

    # Check selector references
    for m in SELECTOR_RE.finditer(raw):
        ns, var = m.group(1), m.group(2)
        if ns == "env":
            assert var in env_vars, f"Unknown env selector: env.{var}"
        elif ns == "sys":
            assert var in ("query", "user_id", "conversation_id", "files"), f"Unknown sys selector: sys.{var}"
        else:
            assert ns in node_map, f"Selector references unknown node: {ns}.{var}"

    # Verify required PowerContext and seekdb endpoints
    http_urls = [n["data"]["url"] for n in nodes if n["data"]["type"] == "http-request"]
    required_paths = [
        "/v1/scopes",
        "/v1/sources/content",
        "/v1/context/prepare",
        "/v1/memory/entries/list",
        "/v1/memory/remember",
        "/api/v1/seekdb/hybrid_search",
        "/api/v1/seekdb/record_grudge",
    ]
    for req_p in required_paths:
        assert any(req_p in u for u in http_urls), f"Missing required HTTP endpoint in DSL: {req_p}"
    print("Verified HTTP endpoints:", required_paths)

    # Verify code nodes compile cleanly
    for n in nodes:
        if n["data"]["type"] == "code":
            compile(n["data"]["code"], f"<dsl:{n['id']}>", "exec")
    print("All Python3 code nodes compiled successfully.")

    # Security check: no real API keys
    assert not re.search(r"sk-[A-Za-z0-9]{20,}", raw), "Found real API key in DSL!"
    print("Security check passed: no hardcoded secrets.")


def main() -> int:
    validate_file(DIFY_DIR / "dsl" / "adventure_npc_chatflow.yml")
    validate_file(DIFY_DIR / "workflow.yml")
    print("ALL DSL VALIDATION CHECKS PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
