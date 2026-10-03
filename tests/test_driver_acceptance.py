import unittest

from driver_rights.acceptance import run


class DriverAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["event_replayed"])
        self.assertEqual(261500, result["drv1_gross_cents"])
        self.assertEqual(20000, result["escrow_during_appeal_cents"])
        self.assertEqual(241500, result["payable_undisputed_cents"])
        self.assertEqual(20000, result["returned_after_uphold_cents"])
        self.assertEqual(20000, result["outstanding_after_uphold_cents"])
        self.assertTrue(result["late_deduction_blocked"])
        self.assertTrue(result["frozen_backfill_blocked"])
        self.assertEqual("closed", result["recompute_period_status"])
        self.assertEqual(38500, result["recompute_delta_cents"])
        self.assertTrue(result["statement_unchanged_after_recompute"])


if __name__ == "__main__":
    unittest.main()
