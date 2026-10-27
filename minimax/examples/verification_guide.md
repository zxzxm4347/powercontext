# 验证样例说明（SDD 上下文管理系统）

## 1. 任务定义
- **要改什么（目标功能 `SPEC-ORD-02`）**：在 `workspace/order_service.py::calculate_order_quote` 中，当 `is_green_certified=True` 且 `subtotal >= 5000.0` 时，叠加 `5%` 绿色低碳采购补贴，且合成总折扣率上限不得超过 `18%`（`0.18`），同时在 `applied_rules` 中追加 `"SPEC-ORD-02"`。
- **哪些原有行为必须保持（回归约束 `SPEC-ORD-01`）**：
  1. `STANDARD` (0%)、`GOLD` (10%)、`PLATINUM` (15%) 默认等级折扣不变；
  2. `subtotal <= 0` 或未知客户等级时抛出 `ValueError`；
  3. 货币代码自动转大写、金额保留两位小数。

## 2. 上下文必须包含的来源清单
- **适用规格**：`specs/order_discount_spec_v1.md`（条款 `SPEC-ORD-01`, `SPEC-ORD-02`）
- **目标代码**：`workspace/order_service.py`（符号 `calculate_order_quote`）
- **验证测试**：
  - 新增功能测试：`tests/test_target_feature.py`（`TestGreenSubsidyFeature`）
  - 回归兼容测试：`tests/test_regression.py`（`TestOrderRegression`）

## 3. 执行与重置方法
```bash
cd minimax
python3 sdd_context/engine.py --verify-all
```
每次执行自动重置 `PowerContext` 状态并将代码还原至修改前快照，完整复现「修改前功能测试失败、回归通过 -> 上下文组装 -> MiniMax 补丁应用 -> 修改后全部通过 -> 状态重启复用 -> 规格 v2.0 更新生效」全流程。
