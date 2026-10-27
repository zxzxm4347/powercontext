# ScholarFlow —— 基于 Qoder、seekdb 与 PowerContext 的科研文献检索与组会周报助手

## 一、项目简介与真实痛点

在日常科研与工程研发中，整理文献要翻几十篇论文、复习时找不到此前的实验参数、写组会周报要从零散笔记里一条条拼凑。
**ScholarFlow** 使用 **Qoder** 完成从需求拆解、编码实现、回归测试到静态站点发布的完整交付，结合 **OceanBase seekdb** 与 **PowerContext** 实现三大核心功能：
1. **文献与实验记录混合检索（seekdb）**：将论文笔记、课程重点与实验数据导入 `seekdb`，支持按语义向量与关键词标签混合检索，秒级定位实验结论与原始出处。
2. **个人薄弱点与科研进度跨会话记忆（PowerContext）**：使用 `PowerContext` 记住研究者的薄弱知识点（`weak_topics`）、已读文献与每日实验动态。
3. **组会周报一键生成**：自动结合 `PowerContext` 中的本周科研流水与 `seekdb` 检索出的支撑实验数据，生成结构化 Markdown 组会汇报。

---

## 二、技术结合与分工

| 组成部分 | 作用说明 | 验证记录与代码路径 |
| :--- | :--- | :--- |
| **Qoder (必选)** | 完成项目需求拆解、`ScholarFlowEngine` 编码实现、自动化验证脚本调试及项目展示网站构建 | 开发记录：[`logs/qoder_dev_record.md`](./logs/qoder_dev_record.md)<br>项目网页：[`site/index.html`](./site/index.html) |
| **seekdb** | 存储论文、实验记录与课程笔记（`research_items` 表），通过向量距离 + 关键词混合打分检索相关段落与实验指标 | 核心实现：[`app.py`](./app.py) (`search_seekdb`)<br>检索日志：[`logs/verification_run.json`](./logs/verification_run.json) |
| **PowerContext** | 保存并跨会话复用研究者的已完成阅读、薄弱知识点与每周科研活动记录 | 核心实现：[`app.py`](./app.py) (`record_progress_and_generate_weekly_report`)<br>状态文件：`data/scholarflow_powercontext.json` |

---

## 三、运行环境与从安装到启动的完整步骤

- **运行环境**：Python `>= 3.10`（跨平台支持 Linux / macOS / Windows）
- **步骤 1：运行自动化验证样例**
  ```bash
  cd qoder
  python3 app.py --verify
  ```
  该命令会自动初始化 `seekdb` 文献与实验库、重置并写入 `PowerContext` 科研进展记忆、执行混合检索并生成完整组会周报，输出验证结果至 `logs/verification_run.json`。
- **步骤 2：启动本地项目网站预览**
  ```bash
  cd qoder
  python3 app.py --serve --port 8092
  ```
  浏览器访问 `http://127.0.0.1:8092/site/index.html` 即可查看项目展示站点。
