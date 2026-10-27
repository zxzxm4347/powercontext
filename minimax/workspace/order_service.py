"""业务工程模块：企业采购订单金额与折扣计算服务 (经 MiniMax 依据 SDD 上下文修改后版本)"""
from dataclasses import dataclass


@dataclass(frozen=True)
class OrderQuote:
    customer_tier: str
    subtotal: float
    currency: str
    discount_rate: float
    final_amount: float
    applied_rules: tuple[str, ...]


TIER_DISCOUNTS = {
    "STANDARD": 0.0,
    "GOLD": 0.10,
    "PLATINUM": 0.15,
}

GREEN_SUBSIDY_THRESHOLD = 5000.0
GREEN_SUBSIDY_RATE = 0.05
MAX_TOTAL_DISCOUNT_RATE = 0.18


def calculate_order_quote(
    customer_tier: str,
    subtotal: float,
    currency: str = "CNY",
    is_green_certified: bool = False,
) -> OrderQuote:
    """根据 SPEC-ORD-01 与 SPEC-ORD-02 计算订单折扣、绿色补贴与 18% 封顶净额。"""
    if subtotal <= 0:
        raise ValueError("subtotal must be positive")
    tier_key = (customer_tier or "STANDARD").strip().upper()
    if tier_key not in TIER_DISCOUNTS:
        raise ValueError(f"Unsupported customer tier: {customer_tier}")

    rate = TIER_DISCOUNTS[tier_key]
    rules = ["SPEC-ORD-01"]

    if is_green_certified and subtotal >= GREEN_SUBSIDY_THRESHOLD:
        rate = min(MAX_TOTAL_DISCOUNT_RATE, round(rate + GREEN_SUBSIDY_RATE, 4))
        rules.append("SPEC-ORD-02")

    final_amt = round(subtotal * (1.0 - rate), 2)
    return OrderQuote(
        customer_tier=tier_key,
        subtotal=round(float(subtotal), 2),
        currency=currency.strip().upper(),
        discount_rate=round(rate, 4),
        final_amount=final_amt,
        applied_rules=tuple(rules),
    )
