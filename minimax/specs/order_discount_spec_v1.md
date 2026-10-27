# 规格文档 SPEC-ORD-2026 (v1.0)：企业采购订单折扣与风控计算规则

## [SPEC-ORD-01] 基础会员等级折扣规则
- `STANDARD` 客户：默认无等级折扣（折扣率 `0.0`）。
- `GOLD` 客户：享受 `10%` 折扣（折扣率 `0.10`）。
- `PLATINUM` 客户：享受 `15%` 折扣（折扣率 `0.15`）。
- 原有兼容约束：订单金额 `subtotal <= 0` 时必须抛出 `ValueError("subtotal must be positive")`；货币代码必须转换为大写并保留两位小数舍入。

## [SPEC-ORD-02] 大额绿色低碳采购叠加补贴与封顶约束（本次新增任务目标）
- 当订单同时满足以下两个条件时：
  1. `is_green_certified=True`（绿色低碳认证采购单）
  2. `subtotal >= 5000.0`
  额外叠加 `5%` 的绿色采购补贴（即总折扣率增加 `0.05`）。
- **总折扣率上限约束（Cap Rule）**：任何订单的最终合成折扣率 `total_discount_rate` **不得超过 `0.18`（18%）**。例如 `PLATINUM`（15%）叠加绿色采购补贴（5%）后，合成折扣率应被封顶为 `0.18`，应付净额 `final_amount = round(subtotal * (1 - total_discount_rate), 2)`。
