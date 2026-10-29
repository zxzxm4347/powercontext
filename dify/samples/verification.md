# 碎锚酒馆 · 记仇老板老巴恩 —— 验证样本与复现说明

本文档提供四幕跨会话剧情的输入样本、预期节点行为与实测输出对照。所有样本均可直接通过 `python dify/backend/app.py --verify` 或单步执行 `dify/scripts/replay_chatflow.py` 重现。

---

## 1. 四幕跨会话验证样本（玩家：`cui`）

### 第一幕：初次进店，往橡木麦酒桶倒粗盐（恶意得罪 `offense`）

- **执行命令**：
  ```bash
  python dify/scripts/replay_chatflow.py --player cui \
    --query "我往你的橡木麦酒桶里倒了一把粗盐，一文钱也不赔，你能拿我怎样？" \
    --judge-file dify/samples/judge/offense.json
  ```
- **PowerContext 交互状态**：
  - `pc_scope`：幂等创建/获取 `idempotency_key = "grudge-inn-cui"` 对应的 `scope_id`。
  - `pc_prepare` & `pc_list`：首次来访返回 `status: "empty"`，`entries: []`。
  - `panel`：计算得出 `visit_count = "1"`，`grudge = "0"`（初见账本为空）。
  - `pc_remember`：将本轮恶行以 `kind = "offense"` 写入账本，生成 `revision = 1`。
- **完整输出日志**：见 [`../evidence/chatflow-replay.log`](../evidence/chatflow-replay.log)。

---

### 第二幕：换个新会话再次进店（跨会话翻旧账验证）

- **执行命令**：
  ```bash
  python dify/scripts/replay_chatflow.py --player cui \
    --query "我又来了，上次那桶加了粗盐的麦酒味道如何？" \
    --judge-file dify/samples/judge/offense.json
  ```
- **PowerContext 交互状态**：
  - `pc_prepare`：返回 `status: "ready"`，召回第一幕写入的 `revision = 1` 粗盐黑账。
  - `panel`：计算得出 `visit_count = "2"`，`grudge = "25"`，`ledger_text` 列出 `[记仇] 冒险者在碎锚酒馆吧台前故意打翻橡木麦酒桶...`。
  - `narrate`：老巴恩根据召回的旧账当场翻脸追讨五十枚金海妖酒钱。
  - `pc_remember`：将本轮当面揭短挑衅继续记入 `offense`（`revision = 2`）。
- **完整输出日志**：见 [`../evidence/chatflow-replay-returning.log`](../evidence/chatflow-replay-returning.log)。

---

### 第三幕：拎来三坛陈年朗姆酒登门赔罪（人情消气 `favor`）

- **执行命令**：
  ```bash
  python dify/scripts/replay_chatflow.py --player cui \
    --query "上次往麦酒桶倒粗盐是我混账，今天拎来三坛陈年朗姆酒赔罪，酒钱双倍奉上。" \
    --judge-file dify/samples/judge/favor.json
  ```
- **PowerContext 交互状态**：
  - `panel`：进店时已累计 2 笔 `offense`（第 3 次照面 · 怨气 `50/100`）。
  - `judge` & `parse`：识别出玩家诚心赔礼，分类为 `kind = "favor"`。
  - `pc_remember`：写入 `favor` 记忆（`revision = 3`）。
- **完整输出日志**：见 [`../evidence/chatflow-replay-favor.log`](../evidence/chatflow-replay-favor.log)。

---

### 第四幕：赔罪后闲聊码头传闻（怨气回落 & 解锁密闻 `neutral`）

- **执行命令**：
  ```bash
  python dify/scripts/replay_chatflow.py --player cui \
    --query "老巴恩，你这碎锚酒馆开了多少年了？今晚码头有什么传闻？" \
    --judge-file dify/samples/judge/neutral.json
  ```
- **PowerContext 交互状态**：
  - `panel`：进店时读取到 `记仇2／记恩1`，怨气值从 `50/100` 回落至 **`30/100`**（第 4 次照面）。
  - `narrate`：老巴恩看到账本里「三坛陈年朗姆酒赔罪」的人情记录，收起铁钩推过一碟海盐烤鱼干，主动聊起暴风海的秘密。
  - `pc_remember`：记录本次日常闲聊 `kind = "neutral"`（`revision = 4`）。
- **完整输出日志**：见 [`../evidence/chatflow-replay-after-favor.log`](../evidence/chatflow-replay-after-favor.log)。

OB-P:Z4X16gLee3rCjUZxvoUw0g
