"""目标功能测试集：验证 SPEC-ORD-02 大额绿色低碳采购叠加补贴与 18% 封顶规则。"""
import unittest
from workspace.order_service import calculate_order_quote


class TestGreenSubsidyFeature(unittest.TestCase):
    def test_green_certified_below_threshold_does_not_trigger(self):
        quote = calculate_order_quote("GOLD", 4999.99, is_green_certified=True)
        self.assertEqual(quote.discount_rate, 0.10)
        self.assertEqual(quote.applied_rules, ("SPEC-ORD-01",))

    def test_gold_with_green_subsidy_stacks_to_15_percent(self):
        quote = calculate_order_quote("GOLD", 6000.0, is_green_certified=True)
        self.assertEqual(quote.discount_rate, 0.15)
        self.assertEqual(quote.final_amount, 5100.00)
        self.assertEqual(quote.applied_rules, ("SPEC-ORD-01", "SPEC-ORD-02"))

    def test_platinum_with_green_subsidy_is_capped_at_18_percent(self):
        quote = calculate_order_quote("PLATINUM", 10000.0, is_green_certified=True)
        self.assertEqual(quote.discount_rate, 0.18)
        self.assertEqual(quote.final_amount, 8200.00)
        self.assertEqual(quote.applied_rules, ("SPEC-ORD-01", "SPEC-ORD-02"))


if __name__ == "__main__":
    unittest.main()
