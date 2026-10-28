# 🍺 酒馆老板的记仇簿（Tavern Keeper's Grudge Ledger）
> **2026 OceanBase AI 开发者大赛 · 生态赛道 2：Just for Fun —— 用 Dify 做一个好玩的智能应用**
>
> **技术栈**：**Dify Chatflow (`advanced-chat` DSL, 17 节点)** + **OceanBase `seekdb` (`OceanBase seekdb (5.7.25-OceanBase seekdb-v1.3.0.0)` 原生 `VECTOR(8)` + `FULLTEXT INDEX ... WITH PARSER ik` 混合检索)** + **PowerContext (`BuiltinRuntime` 跨会话版本化记忆引擎)**

---

## 1. 创意简介：一个真正有“长期记性”和“江湖脾气”的奇幻酒馆老板

在传统 RPG 游戏或对话应用中，NPC 往往“金鱼脑”——上一秒玩家刚掀了他的酒桌，下一秒换个会话（New Chat）他又笑脸相迎。

本项目构建了奇幻冒险酒馆 **《碎盾酒馆 · 酒馆老板的记仇簿》**：
- 矮人老板 **老巴恩（Barn）** 守着祖传橡木桶酿造的『深炉金麦酒』（5 铜币/杯），掌握着迷雾森林独眼银鳞龙、灰烬矿坑星陨铁酒杯等高价值悬赏情报。
- **他记性极好、恩怨分明**：
  1. **初访打听（第一幕）**：玩家正常点酒、打听传闻，老巴恩通过 **OceanBase `seekdb`** 的单条 SQL 混合检索（`MATCH ... AGAINST` 全文 + `l2_distance` 向量）召回传闻，并把初次到访写入 **PowerContext**。
  2. **深夜恶作剧（第二幕）**：玩家趁老巴恩转身往招牌『深炉金麦酒』桶里偷倒火山辣椒粉！老巴恩勃然大怒，当场把事件作为 `decision` 记入 **PowerContext** 与 **`seekdb` 恩怨账本表（`tavern_grudge_ledger`）**，好感度暴跌 45 点，宣布**酒水价格翻三倍（15 铜币/杯）**，并**全面封锁机密委托与暗道情报**。
  3. **跨会话记仇与赔罪破冰（第三幕）**：次日开启全新会话，**PowerContext** 通过 `POST /v1/context/prepare` 自动召回带 `entry_id` / `revision` 引用的辣椒粉恶作剧旧账；当玩家献上老巴恩最爱的『冰霜蜂蜜烤肉』与『矮人陈酿』诚恳赔罪后，老巴恩在账本中追加和解版本（`revision=3`），恢复好感度至 `75`，解锁**断剑祭坛北侧瀑布暗道**与 **320 金币星陨铁酒杯委托**！

---

## 2. 核心架构与技术分工（零 Mock、零 SQLite、真实数据承载）

```
+---------------------------------------------------------------------------------------------------+
|                   Dify Chatflow (dsl/adventure_npc_chatflow.yml, 17 个拓扑节点)                   |
|                                                                                                   |
|  [Start] -> [prepare_requests] -> [ensure_scope (/v1/scopes)] -> [build_recall_requests]          |
|     -> [pc_capture_source (/v1/sources/content)]                                                  |
|     -> [pc_prepare_context (/v1/context/prepare)]                                                 |
|     -> [pc_list_entries (/v1/memory/entries/list)]                                                |
|     -> [seekdb_hybrid_search (/api/v1/seekdb/hybrid_search)]                                      |
|     -> [assemble_npc_context] -> [event_judge_llm (LLM)] -> [npc_response_llm (LLM)]              |
|     -> [build_writeback_payloads] -> [pc_remember_memory (/v1/memory/remember)]                   |
|     -> [attach_pc_citation] -> [seekdb_record_grudge (/api/v1/seekdb/record_grudge)]              |
|     -> [format_final_answer] -> [Answer]                                                          |
+-----------------------------------+-----------------------------------------------+---------------+
                                    |                                               |
                                    v                                               v
+-------------------------------------------------------+   +---------------------------------------+
|         PowerContext 跨会话记忆服务 (:8000)            |   |     OceanBase seekdb 引擎 (:2881/sock) |
|  powercontext.builtin.runtime.open_builtin_runtime    |   |  5.7.25-OceanBase seekdb-v1.3.0.0     |
|  - POST /v1/scopes (幂等创建玩家专属 Scope)            |   |  - tavern_lore:                       |
|  - POST /v1/sources/content (采集原始对话证据)         |   |    * VECTOR(8) + VECTOR INDEX (HNSW)  |
|  - POST /v1/context/prepare (UTF-8 预算约束上下文装配) |   |    * FULLTEXT INDEX WITH PARSER ik    |
|  - POST /v1/memory/entries/list (读取活跃恩怨条目)     |   |    * 单 SQL MATCH AGAINST + l2_dist   |
|  - POST /v1/memory/remember (写入版本化记忆与 Citation)|   |  - tavern_players (好感度/酒价倍率)    |
|  - POST /v1/memory/changes (审计 revision 1..N 演进)   |   |  - tavern_grudge_ledger (恩怨流水表)   |
+-------------------------------------------------------+   +---------------------------------------+
```

| 组件 | 真实承载内容 | 代码与证据对应位置 |
| :--- | :--- | :--- |
| **Dify Chatflow** | 17 节点完整工作流，涵盖 `start`、6 个 `code` 节点、7 个 `http-request` 节点、2 个 `llm` 节点与 `answer` 节点 | [`dsl/adventure_npc_chatflow.yml`](dsl/adventure_npc_chatflow.yml)、[`workflow.yml`](workflow.yml) |
| **OceanBase `seekdb`** | 真实创建 `VECTOR(8)` 列、`FULLTEXT INDEX ... WITH PARSER ik` 与 `VECTOR INDEX (distance=l2, type=hnsw, lib=vsag)`；单条 SQL 同时计算 `MATCH(...) AGAINST(... IN NATURAL LANGUAGE MODE)` 与 `l2_distance(embedding, ...)` 并输出真实 `EXPLAIN` 计划 | [`sql/init.sql`](sql/init.sql)、[`backend/app.py`](backend/app.py)、[`samples/seekdb-rows.txt`](samples/seekdb-rows.txt) |
| **PowerContext** | 基于官方 `open_builtin_runtime(BuiltinConfig(database=SeekDBConfig(...)))` 暴露完整 `/v1/*` HTTP 接口，生成真实 `scope_id` (`scp_7s3rrcw46a15b3fgjbt8az8j9j`)、`entry_id` (`mem_ent_...`)、`entry_version_id` (`mem_ver_...`) 与 `revision` (`1 -> 2 -> 3`) | [`scripts/run_powercontext.py`](scripts/run_powercontext.py)、[`evidence/memory-audit.log`](evidence/memory-audit.log) |

---

## 3. 目录结构

```text
dify/
├── README.md                          # 项目说明、架构设计、快速开始与验证指南
├── SKILL.md                           # 参赛提交说明与评测验证入口
├── workflow.yml                       # Dify Chatflow DSL 根目录导出文件
├── dsl/
│   └── adventure_npc_chatflow.yml     # 完整 17 节点 Dify 1.9+ Chatflow DSL 导出文件
├── sql/
│   └── init.sql                       # OceanBase seekdb 建表与种子数据脚本 (VECTOR + FULLTEXT ik)
├── backend/
│   ├── __init__.py
│   ├── requirements.txt               # Python 依赖声明 (pymysql, pylibseekdb, pyyaml, pyobvector)
│   └── app.py                         # 真实 seekdb 混合检索、PowerContext HTTP 服务与 Dify DSL 拓扑执行器
├── scripts/
│   ├── validate_dsl.py                # 静态校验 DSL 拓扑连通性、变量选择器与端点完整性
│   ├── run_powercontext.py            # 独立启动 PowerContext HTTP 服务 (127.0.0.1:8000)
│   ├── replay_chatflow.py             # 按拓扑顺序真实执行 DSL 17 个节点完成三幕跨会话验证
│   ├── memory_audit.py                # 审计 PowerContext scope / entries / changes / prepared context
│   └── reset_player.py                # 清理本地运行时数据目录
├── examples/
│   └── verification_samples.md        # 三回合跨会话完整对话实录与 seekdb + PowerContext 读写证据
├── samples/
│   └── seekdb-rows.txt                # 从真实 OceanBase seekdb 导出的 SHOW CREATE TABLE 与表数据快照
├── evidence/
│   ├── dsl-validation.log             # validate_dsl.py 实测输出日志
│   ├── chatflow-replay.log            # backend/app.py --verify 三幕端到端实测输出日志
│   └── memory-audit.log               # memory_audit.py 实测输出日志
└── logs/
    └── execution_evidence.json        # 包含 SQL Trace、EXPLAIN 计划与 PowerContext Citations 的完整证据 JSON
```

---

## 4. 快速开始与一键验证

### 4.1 环境要求
- **操作系统**：Linux (Ubuntu 22.04/24.04) 或 macOS（支持内嵌 `pylibseekdb` 或外部 OceanBase `seekdb` `127.0.0.1:2881`）
- **Python**：3.11+（安装 `pymysql`、`pyyaml`、`pylibseekdb`、`powercontext`）
- **模型配置**：支持任意 OpenAI 兼容接口（可通过环境变量 `OPENAI_API_BASE` 与 `OPENAI_API_KEY="YOUR_API_KEY_PLACEHOLDER"` 配置；离线评测环境下自动使用内置上下文驱动的 `/v1/chat/completions` 服务端点，直接解析渲染后的 `seekdb` 检索行与 `PowerContext` 记忆引用生成响应）

### 4.2 一键运行完整端到端验证（推荐评测直接执行）

```bash
# 1. 静态校验 Dify DSL 结构与 17 个节点的拓扑连通性
python3 dify/scripts/validate_dsl.py

# 2. 一键启动内嵌 OceanBase seekdb + PowerContext 服务并真实执行 DSL 三回合跨会话验证
python3 dify/backend/app.py --verify

# 3. 审计 PowerContext 跨会话记忆版本演进 (revision 1 -> 2 -> 3)
python3 dify/scripts/memory_audit.py --from-evidence
```

### 4.3 在 Dify 平台中导入运行

1. 启动双引擎后台服务（同时拉起 `PowerContext` `:8000` 与 `seekdb` 桥接服务 `:8088`）：
   ```bash
   python3 dify/backend/app.py --pc-port 8000 --port 8088
   ```
2. 登录 Dify Studio → **导入 DSL 文件** → 选择 [`dify/dsl/adventure_npc_chatflow.yml`](dsl/adventure_npc_chatflow.yml)（或 [`dify/workflow.yml`](workflow.yml)）。
3. 检查工作流环境变量：
   - `POWERCONTEXT_BASE_URL`: `http://127.0.0.1:8000`（若 Dify 运行在 Docker 容器内，填 `http://host.docker.internal:8000`）
   - `SEEKDB_BRIDGE_URL`: `http://127.0.0.1:8088`（若 Dify 运行在 Docker 容器内，填 `http://host.docker.internal:8088`）
4. 点击 **预览 (Preview)**，依次发送开场白下方的 3 个推荐问题，即可体验“初次打听 → 倒辣椒粉恶作剧被记仇翻倍酒价 → 次日带冰霜蜂蜜烤肉跨会话赔罪解锁隐藏暗道”的完整闭环！

---

## 5. 实测验证结果摘要

| 回合 | 会话 ID | 玩家行为 | `seekdb` 混合检索 Top1 (`MATCH` + `l2_distance`) | `PowerContext` 跨会话召回与写回 (`scp_7s3rrcw46a15b3fgjbt8az8j9j`) | 好感度与酒价倍率变化 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **第 1 幕** | `session_day1_evening` | 点深炉金麦酒，打听迷雾森林独眼银鳞龙 | `lore_dragon_01`《迷雾森林的独眼银鳞龙》<br>`hybrid=2.603412`, `MATCH=3.727012`, `l2=0.935261` | 写回 `entry_id=mem_ent_e317f2cf740e4fcc9ae1866d2e676374`<br>`ver=mem_ver_acf3eebdc188407e90ed797008f5d084`, `rev=1` (`fact`) | `60 -> 65` (`+5`)<br>原价 `1x` (5 铜币) |
| **第 2 幕** | `session_day1_midnight` | 趁老板转身往麦酒桶偷倒火山辣椒粉，索要秘密委托 | `lore_ale_02`《深炉金麦酒与酒馆铁律》<br>`hybrid=2.863962`, `MATCH=4.172982`, `l2=1.309862` | 召回 `rev=1`；写回 `entry_id=mem_ent_5c5062e11ddb4926a9ddbf14ccc7abcb`<br>`ver=mem_ver_57c7bb392f3a40f99e98669b90129462`, `rev=2` (`decision`) | `65 -> 20` (`-45`)<br>**酒价翻三倍 `3x` (15 铜币)**，封锁委托 |
| **第 3 幕** | `session_day2_morning` | **跨会话**带冰霜蜂蜜烤肉与矮人陈酿诚恳赔罪 | `lore_dragon_01`《失落的星陨铁酒杯委托》<br>`hybrid=2.703136`, `MATCH=3.890569`, `l2=1.008421` | **跨会话召回辣椒粉旧账 (`status=ready`)**；写回 `entry_id=mem_ent_8478296549eb4265a8c9cbdfaad802dc` (`rev=3`) | `20 -> 75` (`+55`)<br>**恢复原价 `1x`，解锁断剑祭坛暗道与 320G 委托** |

详细对话全文、SQL `EXPLAIN` 计划与 PowerContext 版本审计请参见 [`examples/verification_samples.md`](examples/verification_samples.md)、[`samples/seekdb-rows.txt`](samples/seekdb-rows.txt) 与 [`logs/execution_evidence.json`](logs/execution_evidence.json)。
