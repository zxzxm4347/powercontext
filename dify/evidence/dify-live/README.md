# Dify 1.9.2 实机运行与跨会话 API 证据

本目录保存了将 [`../../dsl/adventure_npc_chatflow.yml`](../../dsl/adventure_npc_chatflow.yml) 导入真实 **Dify 1.9.2** 环境、发布工作流并连续发起两次**不同会话 ID（`conversation_id`）** 的 `POST /v1/chat-messages` 请求所留存的完整证据。

## 1. 核心验证结论

| 阶段 | 结果摘要 | 对应证据文件 |
| :--- | :--- | :--- |
| **1. 导入与发布 DSL** | `mode: advanced-chat`，`version: 0.4.0`，12 节点完整解析无报错 | [`dify-app.json`](dify-app.json)、[`dify-workflow-draft.json`](dify-workflow-draft.json) |
| **2. 模型供应商绑定** | 工作区绑定 `langgenius/openai/openai` 插件供应商 | [`dify-model-providers.json`](dify-model-providers.json) |
| **3. 会话 A（首次进店挑衅）** | `conversation_id = 7c180eff-...`，`player_id = docker-final`，账本显示「第 1 次照面 · 怨气 0/100」，回合末写入第 1 笔 `offense` 记忆 | [`dify-chat-turn1.json`](dify-chat-turn1.json) |
| **4. 会话 B（新开会话翻旧账）** | `conversation_id = 241e126f-...`（**全新会话**），同一 `player_id = docker-final`，账本自动更新为「第 2 次照面 · 怨气 25/100」，并带出上一轮往麦酒桶倒粗盐的原话 | [`dify-chat-turn2.json`](dify-chat-turn2.json) |
| **5. PowerContext 账本审计** | `memory_audit.py --player docker-final` 查出 `revision 1..2` 两条完整记录与原话审计轨迹 | [`dify-powercontext-audit.log`](dify-powercontext-audit.log) |

OB-P:Z4X16gLee3rCjUZxvoUw0g
