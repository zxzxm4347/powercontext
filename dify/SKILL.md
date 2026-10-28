---
name: dify-tavern-grudge-ledger
description: 基于 Dify Chatflow + OceanBase seekdb（向量与全文混合检索）+ PowerContext（跨会话版本化记忆引擎）构建的《酒馆老板的记仇簿》互动智能应用。
---

# 酒馆老板的记仇簿 (Tavern Keeper's Grudge Ledger)

## 1. 赛题对应关系
- **赛道**：2026 OceanBase AI 开发者大赛 · 生态赛道 2（Just for Fun —— 用 Dify 做一个好玩的智能应用，`questionId=600076`）
- **核心技术栈**：
  1. **Dify Chatflow 工作流**：[`dsl/adventure_npc_chatflow.yml`](dsl/adventure_npc_chatflow.yml) 与 [`workflow.yml`](workflow.yml)（共 17 个节点，涵盖 `start`、`code`、`http-request`、`llm`、`answer`）。
  2. **OceanBase `seekdb` 混合检索**：[`sql/init.sql`](sql/init.sql) 与 [`backend/app.py`](backend/app.py)（基于 `OceanBase seekdb (5.7.25-OceanBase seekdb-v1.3.0.0)`，真实执行 `VECTOR(8)` 向量列、`FULLTEXT INDEX ... WITH PARSER ik` 全文索引、`VECTOR INDEX (distance=l2, type=hnsw, lib=vsag)` 向量索引，以及单条 SQL `MATCH ... AGAINST` + `l2_distance` 混合查询与 `EXPLAIN` 验证）。
  3. **PowerContext 跨会话记忆服务**：基于官方 `powercontext.builtin.runtime.open_builtin_runtime` + `SeekDBConfig`，通过 HTTP 调用 `/v1/scopes`、`/v1/sources/content`、`/v1/context/prepare`、`/v1/memory/entries/list`、`/v1/memory/remember`、`/v1/memory/changes`。

## 2. 一键验证命令

```bash
# 校验 Dify DSL 结构与拓扑图
python3 dify/scripts/validate_dsl.py

# 运行三回合跨会话端到端验证（自动拉起内嵌 seekdb 与 PowerContext 服务并执行 DSL 全部 17 个节点）
python3 dify/backend/app.py --verify

# 查看 PowerContext 记忆版本演进审计报告
python3 dify/scripts/memory_audit.py --from-evidence
```

## 3. 交付物清单
- [`workflow.yml`](workflow.yml) / [`dsl/adventure_npc_chatflow.yml`](dsl/adventure_npc_chatflow.yml)：可导入 Dify 1.9+ 的完整 Chatflow DSL。
- [`sql/init.sql`](sql/init.sql)：OceanBase `seekdb` 表结构与种子数据脚本。
- [`backend/app.py`](backend/app.py)：真实连接 `seekdb` 与 `PowerContext` 并按拓扑执行 Dify DSL 的后端程序（无任何 SQLite 或假 JSON 文件）。
- [`samples/seekdb-rows.txt`](samples/seekdb-rows.txt)：从真实 `seekdb` 实例导出的表结构与数据快照。
- [`logs/execution_evidence.json`](logs/execution_evidence.json)：三回合端到端验证完整证据日志。
