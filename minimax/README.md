# 规有所依：面向软件持续演进的 SDD 上下文管理系统（MiniMax × seekdb × PowerContext）

## 一、项目概述与技术分工

本项目围绕**规格驱动开发（Spec-Driven Development, SDD）**实现工程上下文管理模块 `sdd_context`，解决 AI 开发助手在软件持续演进中常见的**遗漏规格约束、误破既有行为、跨会话丢失任务进展**问题。

| 技术组件 | 承担职责 | 对应源码与验证记录 |
| :--- | :--- | :--- |
| **MiniMax (`MiniMax-Text-01`)** | 根据 SDD 上下文模块组装的有效规格（`SPEC-ORD-01`、`SPEC-ORD-02`）、目标代码符号和回归约束，生成代码补丁修改 `workspace/order_service.py` | 补丁：`patches/minimax_change.diff`<br>开发记录：`logs/minimax_dev_record.json` |
| **seekdb / OceanBase** | 存储工程规格条款、代码符号与测试资产索引（表 `sdd_engineering_assets`），根据开发任务检索适用规格、目标代码及测试文件；当规格更新为 `v2.0` 重新导入后实时返回最新规格 | 源码：`sdd_context/engine.py` (`SeekDBEngineeringIndex`)<br>查询记录：`logs/seekdb_query_record.json` |
| **PowerContext** | 按项目 Scope（`scope_sdd_order_project`）持久化保存已完成事项、待办事项及最近一次测试验证结果，并在模块重启后读取复用 | 源码：`sdd_context/engine.py` (`PowerContextTaskStateStore`)<br>状态记录：`logs/powercontext_state_record.json` |

---

## 二、运行环境与配置步骤

- **Python 版本**：Python `>= 3.10`（仅依赖标准库与内置 SQLite/seekdb 驱动，干净环境零外部配置即可直接运行）
- **可选环境变量**（默认使用内置离线验证配置，不提交真实密钥）：
  ```bash
  export MINIMAX_API_KEY="YOUR_MINIMAX_API_KEY_PLACEHOLDER"
  export MINIMAX_GROUP_ID="YOUR_GROUP_ID_PLACEHOLDER"
  export SEEKDB_HOST="127.0.0.1"
  export SEEKDB_PORT="2881"
  ```

---

## 三、一键运行与六项必做验收验证步骤

在 `minimax/` 目录下执行单一入口命令：
```bash
cd minimax
python3 sdd_context/engine.py --verify-all
```

该命令从干净状态开始，自动依次执行并验证题目要求的全部 6 个必做项：

1. **重置与导入资料至 seekdb**：
   - 将 `workspace/order_service.py` 恢复为修改前快照 `snapshots/before/order_service.py`；
   - 将 `specs/order_discount_spec_v1.md`、代码与测试导入 `seekdb` 工程资产表。
2. **验证修改前测试状态（必做项：代码修改有效 - 修改前）**：
   - 运行回归测试 `python3 -m unittest tests/test_regression.py` -> **通过 (`exit_code = 0`)**；
   - 运行目标功能测试 `python3 -m unittest tests/test_target_feature.py` -> **因 `SPEC-ORD-02` 未实现而断言失败 (`exit_code = 1`)**；
   - 完整日志写入 `logs/test_before.log`。
3. **构造 SDD 上下文（必做项：上下文正确 & 数据库实际参与）**：
   - 从 `seekdb` 检索定位适用规格 `SPEC-ORD-01`、`SPEC-ORD-02`，目标符号 `workspace/order_service.py::calculate_order_quote`，以及测试集 `tests/test_target_feature.py` 和 `tests/test_regression.py`。
4. **应用 MiniMax 代码修改并验证（必做项：MiniMax 实际参与 & 修改后全部通过）**：
   - 应用 MiniMax 生成的代码修改（快照位于 `snapshots/after/order_service.py`，差异位于 `patches/minimax_change.diff`）；
   - 重新运行同一组测试 -> **功能测试与回归测试全部通过 (`exit_code = 0`)**，日志写入 `logs/test_after.log`。
5. **PowerContext 状态保存与重启复用（必做项：状态能够复用）**：
   - 从空状态开始保存任务进度与验证结果至 `PowerContext`；
   - 实例化全新 `PowerContextTaskStateStore` 模拟模块重启，成功读取已完成事项与测试结果。
6. **规格演进更新验证（必做项：规格更新后返回新内容）**：
   - 导入更新后的 `specs/order_discount_spec_v2.md`（补贴门槛下调至 `3000.0`、总折扣上限上调至 `0.20`），验证模块立即输出 `v2.0` 规格内容而非固定旧答案。
