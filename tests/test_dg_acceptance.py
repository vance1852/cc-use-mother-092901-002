"""离线验收测试。"""

import unittest

from driver_guarantee.acceptance import run


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance_guards(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        # 投诉一：未休息继续派单必须被拦，强制派单可归责
        self.assertTrue(result["dispatch_rejected_when_unrested"])
        self.assertTrue(result["forced_dispatch_attributed_to_carrier"])
        # 投诉二：晚到异常记录不得追扣、申诉期冻结扣款、争议单独托管
        self.assertTrue(result["late_evidence_blocked"])
        self.assertTrue(result["deductions_blocked_during_appeal"])
        self.assertTrue(result["escrow_held"])
        self.assertTrue(result["uncontested_income_preserved"])
        self.assertTrue(result["appeal_released_to_driver"])
        # 重放不重复计费、冻结边界、规则复算
        self.assertTrue(result["rescue_replayed"])
        self.assertEqual(1, result["rescue_lines_count"])
        self.assertTrue(result["frozen_backfill_blocked"])
        self.assertGreater(result["recompute_delta"], 0)


if __name__ == "__main__":
    unittest.main()
