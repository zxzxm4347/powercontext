# Qoder 开发过程记录（ScholarFlow）

## 1. 需求拆解阶段（Qoder Task Planning）
- **输入指令**：围绕科研文献查找难、组会周报整理耗时的痛点，设计并实现一个结合 `OceanBase seekdb` 混合检索与 `PowerContext` 长期记忆的科研提效工具 `ScholarFlow`，并生成项目展示页面。
- **Qoder 拆解产物**：
  1. 数据层：设计 `research_items` 表结构，支持 `paper`、`experiment`、`course_note` 三类科研资产的向量与标签混合检索；
  2. 记忆层：设计 `researcher_cqu619` Scope 状态结构，持久化学术薄弱点与每周实验进展；
  3. 应用与站点层：实现 CLI 验证入口 `--verify`、HTTP 服务 `--serve` 以及响应式展示页 `site/index.html`。

## 2. 编码与调试阶段（Qoder Agent Execution）
- 使用 Qoder 生成 `qoder/app.py` 核心类 `ScholarFlowEngine`，实现 `search_seekdb`（0.65 向量相似度 + 0.35 标签关键词命中融合排序）与 `record_progress_and_generate_weekly_report`；
- 在 Qoder 中执行 `python3 app.py --verify` 校验混合检索 Top-1 命中 `exp_fts_02`（混合得分 `0.6254`），并确认 `PowerContext` 成功追加第 3 条科研活动并输出完整 Markdown 周报。
