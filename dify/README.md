# Just for Fun：『会记仇的酒馆老板』—— 基于 Dify + PowerContext + seekdb 的互动剧情应用

## 一、作品创意、玩法与技术分工

### 1.1 创意与核心玩法
在传统 RPG 游戏里，玩家往 NPC 酒桶里乱丢东西、逃单或调戏老板，开启新会话后 NPC 往往像失忆一样继续笑脸相迎。
本项目使用 **Dify Chatflow** 结合 **PowerContext** 与 **OceanBase seekdb**，打造了**『碎盾酒馆：会记仇的矮人老板巴恩』**：
1. **自由打听与探索**：玩家可以向老板巴恩打听迷雾森林的宝藏传闻、招牌酒单或今日悬赏。
2. **恶作剧与蝴蝶效应**：玩家可以随时对酒馆搞恶作剧（例如*“趁老板转身往招牌麦酒桶里倒一勺辣椒粉”*）。
3. **跨会话记仇与伏笔回收**：开启全新会话再次进入酒馆时，老板不仅会通过 **PowerContext** 翻出旧账、把酒价翻三倍，还会发现被倒了辣椒粉的麦酒意外变成了能灌醉迷雾森林银鳞龙的『火山椒烈性黑麦酒』，从而触发专属隐藏赎罪任务！

### 1.2 Dify 与 seekdb、PowerContext 的技术分工
| 组件 | 承担职责 | 实际作用核对方式 |
| :--- | :--- | :--- |
| **Dify (必选)** | 核心 Chatflow 编排（输入变量解析 -> 并发调用 seekdb 检索与 PowerContext 记忆召回 -> LLM 角色扮演与状态判定 -> 调用 PowerContext 写回事件 -> 输出回复） | 导入 `dsl/adventure_npc_chatflow.yml`（或根目录 `workflow.yml`）即可查看完整节点拓扑 |
| **seekdb** | 存储『碎盾酒馆』奇幻世界设定、招牌酒单规矩与悬赏任务表（`tavern_lore`），通过全文（`MATCH ... AGAINST`）与向量距离（`l2_distance`）混合检索匹配玩家当前话题 | 查看 `logs/execution_evidence.json` 中的 `node_seekdb_search` SQL 与混合检索得分 |
| **PowerContext** | 按玩家 Scope（`scope_tavern_player_01`）持久化保存每次互动、好感度与『记仇账本（Active Grudges）』，在新会话中通过 `prepare` 接口组装带引用的记忆上下文 | 查看 `logs/execution_evidence.json` 中的 `node_powercontext_recall` 与 `data/powercontext_state.json` |

---

## 二、运行环境与版本要求

- **Dify 版本**：Dify Community Edition `>= 0.15.0` / `1.x`（支持 `advanced-chat` DSL 导入）
- **Python 环境**：Python `>= 3.10`
- **数据库与上下文服务**：
  - OceanBase **seekdb**（默认连接 `127.0.0.1:2881`，配套桥接服务内置 SQLite 同构混合检索自动回退，开箱即用）
  - **PowerContext**（通过 HTTP API / 本地状态持久化引擎接入）
- **模型配置**：Dify 中配置任意兼容 OpenAI 接口的对话模型（如 `qwen-plus`、`deepseek-chat` 或 `gpt-4o-mini`，配置示例均使用占位符 `YOUR_API_KEY_HERE`）

---

## 三、从安装、初始化到启动的完整步骤

### 步骤 1：一键初始化数据并运行完整三回合验证样例
无需额外部署复杂外部依赖，在 `dify/` 目录下直接运行：
```bash
cd dify
python3 backend/app.py --verify
```
该命令会自动完成：
1. 初始化 `data/tavern_seekdb.sqlite`（写入奇幻传闻、酒馆规矩、悬赏委托三类语料及向量索引）并重置 `scope_tavern_player_01` 的 PowerContext 初始状态；
2. 依次执行三回合跨会话验证样例，并将完整节点输入输出写入 `logs/execution_evidence.json`。

### 步骤 2：启动 Dify HTTP Request 桥接后端
```bash
python3 backend/app.py --serve --port 8088
```
服务默认监听 `http://127.0.0.1:8088`，向 Dify 工作流的三个 `HTTP Request` 节点提供：
- `POST /api/v1/seekdb/hybrid_search`
- `POST /api/v1/powercontext/prepare`
- `POST /api/v1/powercontext/capture`

### 步骤 3：在 Dify 中导入 DSL 并体验
1. 登录 Dify 控制台 -> 点击 **「导入 DSL 文件」** -> 选择 `dify/dsl/adventure_npc_chatflow.yml`（或 `dify/workflow.yml`）。
2. 在工作流右上角 **「环境变量」** 中确认 `SEEKDB_BRIDGE_URL` 为 `http://host.docker.internal:8088`（宿主机直接运行则为 `http://127.0.0.1:8088`）。
3. 点击 **「预览 / 发布」**，即可按下方验证样例体验会记仇的酒馆老板！

---

## 四、验证样例与运行结果摘录

详细步骤与恢复初始状态方法参见 [`examples/verification_samples.md`](./examples/verification_samples.md)，完整运行证据参见 [`logs/execution_evidence.json`](./logs/execution_evidence.json)：

1. **回合 1（会话 1 - 初次打听）**：
   - 输入：`老板，来一杯最烈的麦酒，顺便跟我讲讲迷雾森林的宝藏传闻！`
   - 表现：`seekdb` 命中 `lore_01`（迷雾森林银鳞龙与秘银宝箱），老板热情倒酒（5银币/杯），好感度 `60 -> 65`。
2. **回合 2（会话 1 - 玩家恶作剧）**：
   - 输入：`（恶作剧）趁老板转身，偷偷往他的招牌麦酒桶里倒了一勺辣椒粉。`
   - 表现：`seekdb` 命中 `lore_02`（巴恩的规矩与火山椒黑麦酒线索），老板勃然大怒记入小本本，`PowerContext` 记录 `prank_grudge`，好感度暴跌至 `30`。
3. **回合 3（会话 2 - 跨会话重进酒馆触发记仇与隐藏任务）**：
   - 输入：`嘿巴恩，我隔了两天又来喝酒啦！今天酒馆有什么新委托？`
   - 表现：尽管是全新会话，`PowerContext` 召回 `src_002` 辣椒粉恶作剧记录，老板当场翻旧账将麦酒涨价至三倍（15银币），并要求玩家带着那桶意外酿成的『火山椒烈性黑麦酒』去迷雾森林灌醉银鳞龙、取回铁砧徽章将功折罪！
