"""回归测试集：验证 SPEC-ORD-01 原有等级折扣、货币标准化与边界异常行为保持不变。"""
import unittest
from workspace.order_service import calculate_order_quote


class TestOrderRegression(unittest.TestCase):
    def test_standard_and_gold_tiers(self):
        q_std = calculate_order_quote("standard", 1000.0, "usd")
        self.assertEqual(q_std.customer_tier, "STANDARD")
        self.assertEqual(q_std.currency, "USD")
        self.assertEqual(q_std.discount_rate, 0.0)
        self.assertEqual(q_std.final_amount, 1000.0)
        self.assertEqual(q_std.applied_rules, ("SPEC-ORD-01",))

        q_gold = calculate_order_quote("GOLD", 2000.0, "cny")
        self.assertEqual(q_gold.discount_rate, 0.10)
        self.assertEqual(q_gold.final_amount, 1800.0)

    def test_invalid_subtotal_and_tier_raise_value_error(self):
        with self.assertRaises(ValueError):
            calculate_order_quote("GOLD", 0.0)
        with self.assertRaises(ValueError):
            calculate_order_quote("GOLD", -100.0)
        with self.assertRaises(ValueError):
            calculate_order_quote("UNKNOWN_TIER", 500.0)


if __name__ == "__main__":
    unittest.main()
