# 验证证据目录（Evidence Index）

本目录收录了 **《碎锚酒馆 · 记仇老板老巴恩》** 在 **PowerContext + OceanBase seekdb (`5.7.25-OceanBase seekdb-v1.3.0.0`) + Dify 1.9.2** 下的全部实测运行日志与 API 原始响应，所有日志均可通过 `python dify/backend/app.py --verify` 一键复现。

## 1. 证据文件清单

| 文件名 | 产生脚本 / 来源 | 证明内容 |
| :--- | :--- | :--- |
| [`dsl-validation.log`](dsl-validation.log) | `python dify/scripts/validate_dsl.py dify/dsl/adventure_npc_chatflow.yml` | 证明 12 节点 Chatflow DSL 100% 符合 Dify 1.9.2 导入规范（`0 errors, 0 warning(s)`） |
| [`memory-demo.log`](memory-demo.log) | `python dify/scripts/pc_http_demo.py --player shen --reset` | 证明第一回合写入 PowerContext 的 `offense` 记忆在第二回合 `/v1/context/prepare` 中被完整召回（`content_bytes = 814`） |
| [`memory-second-visit.log`](memory-second-visit.log) | `python dify/scripts/pc_http_demo.py --player shen` | 证明跨进程重启后，首轮已持久化的 2 条记忆依然保存在 OceanBase seekdb 中（初始 `entries = 2`，追加后升至 `revision = 4`） |
| [`chatflow-replay.log`](chatflow-replay.log) | `replay_chatflow.py --player cui`（第一幕：倒粗盐挑衅） | 初次进店 `pc_prepare` 为 `empty`（第 1 次照面 · 怨气 `0/100`），回合末通过 `pc_remember` 写入 `offense`（`revision = 1`） |
| [`chatflow-replay-returning.log`](chatflow-replay-returning.log) | `replay_chatflow.py --player cui`（第二幕：跨会话重返） | 新会话进店时 `pc_prepare` 变为 `ready`，`panel` 计算出「第 2 次照面 · 怨气 `25/100`」，老巴恩当场翻出粗盐旧账 |
| [`chatflow-replay-favor.log`](chatflow-replay-favor.log) | `replay_chatflow.py --player cui`（第三幕：三坛朗姆酒赔罪） | 账本累积 2 条 `offense`（怨气 `50/100`），本回合识别为 `favor`（三坛陈年朗姆酒赔礼）并写入 `revision = 3` |
| [`chatflow-replay-after-favor.log`](chatflow-replay-after-favor.log) | `replay_chatflow.py --player cui`（第四幕：赔罪后打听传闻） | 账本包含「记仇2／记恩1」，怨气由 `50/100` 回落至 `30/100`，老巴恩消气并透露暴风海沉船密闻 |
| [`memory-audit.log`](memory-audit.log) | `python dify/scripts/memory_audit.py --player cui` | 完整导出玩家 `cui` 的 Scope、4 条 active 记忆条目及 `revision 1..4` 不可变修订流水 |
| [`seekdb-evidence.log`](seekdb-evidence.log) | `python dify/scripts/seekdb_evidence.py` | 验证 OceanBase seekdb (`5.7.25-OceanBase seekdb-v1.3.0.0`) 的向量+全文混合检索、`/v1/capabilities` 与 `/v1/memory/search` |
| [`environment.txt`](environment.txt) | 环境信息记录 | 记录操作系统内核、Python 版本、OceanBase seekdb 版本及 Dify DSL 版本 |
| [`dify-live/`](dify-live/README.md) | 真实 Dify 1.9.2 部署 API 导出 | 包含 `dify-app.json`、`dify-workflow-draft.json`、`dify-model-providers.json`、`dify-chat-turn1.json`、`dify-chat-turn2.json`、`dify-powercontext-audit.log` |

OB-P:Z4X16gLee3rCjUZxvoUw0g
