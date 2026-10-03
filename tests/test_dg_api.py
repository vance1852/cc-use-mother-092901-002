"""司机履约与权益保障 HTTP 路由测试。"""

import unittest
from datetime import datetime, timezone

from driver_guarantee.api import route
from driver_guarantee.clock import ManualClock
from driver_guarantee.service import GuaranteeService
from driver_guarantee.storage import Database


def call(service, method, path, body=None, actor=""):
    return route(service, method, path, body, {"X-Actor-Id": actor})


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.clock = ManualClock(datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.s = GuaranteeService(self.db, self.clock)
        call(self.s, "POST", "/bootstrap", {"request_id": "b0", "principal_id": "admin",
                                            "display_name": "管理员"})
        call(self.s, "POST", "/carriers", {"request_id": "b1",
                                           "carrier_id": "c1", "name": "承运一"}, "admin")
        call(self.s, "POST", "/drivers", {"request_id": "b2",
                                          "driver_id": "d1", "display_name": "张",
                                          "license_no": "L1", "organization_id": "c1"}, "admin")
        call(self.s, "POST", "/principals", {"request_id": "b3",
                                             "principal_id": "pd1", "role": "driver",
                                             "display_name": "张", "driver_id": "d1"}, "admin")
        call(self.s, "POST", "/principals", {"request_id": "b4",
                                             "principal_id": "pc1", "role": "carrier",
                                             "display_name": "调度", "carrier_id": "c1"}, "admin")
        call(self.s, "POST", "/principals", {"request_id": "b5",
                                             "principal_id": "pr1", "role": "regulator",
                                             "display_name": "监管"}, "admin")
        call(self.s, "POST", "/contracts", {"request_id": "b6",
                                            "contract_id": "k1", "carrier_id": "c1",
                                            "title": "合同", "body": {"v": 1}}, "pc1")
        call(self.s, "POST", "/rulesets", {"request_id": "b7",
                                           "ruleset_id": "rs", "title": "v1"}, "admin")

    def tearDown(self):
        self.db.close()

    def test_health_without_actor(self):
        status, payload = call(self.s, "GET", "/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_unknown_route_404(self):
        status, payload = call(self.s, "GET", "/nope")
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_missing_actor_is_rejected(self):
        status, payload = call(self.s, "POST", "/trips", {
            "request_id": "x", "trip_id": "z", "carrier_id": "c1", "origin": "A",
            "destination": "B", "planned_pickup_at": "2026-10-01T00:00:00Z",
            "contract_id": "k1"})
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])

    def test_driver_available_trips_view(self):
        status, _ = call(self.s, "POST", "/trips", {
            "request_id": "t1", "trip_id": "t1", "carrier_id": "c1",
            "origin": "A", "destination": "B", "planned_pickup_at": "2026-10-01T00:00:00Z",
            "contract_id": "k1", "estimated_driving_min": 60}, "pc1")
        self.assertEqual(201, status)
        status, payload = call(self.s, "GET", "/trips/available?at=2026-10-01T00:00:00Z",
                               actor="pd1")
        self.assertEqual(200, status)
        self.assertEqual("t1", payload["items"][0]["trip_id"])
        self.assertTrue(payload["items"][0]["availability"]["eligible"])

    def test_dispatch_rejected_returns_conflict(self):
        call(self.s, "POST", "/work/intervals", {
            "request_id": "w1", "driver_id": "d1", "kind": "driving",
            "start_at": "2026-09-30T20:00:00Z", "end_at": "2026-10-01T00:00:00Z"}, "pd1")
        call(self.s, "POST", "/trips", {
            "request_id": "t2", "trip_id": "t2", "carrier_id": "c1",
            "origin": "A", "destination": "B", "planned_pickup_at": "2026-10-01T00:00:00Z",
            "contract_id": "k1", "estimated_driving_min": 30}, "pc1")
        status, payload = call(self.s, "POST", "/dispatches", {
            "request_id": "d1", "trip_id": "t2", "driver_id": "d1",
            "at": "2026-10-01T00:00:00Z"}, "pc1")
        self.assertEqual(409, status)
        self.assertEqual("conflict", payload["error"])

    def test_regulator_overtime_view(self):
        status, payload = call(self.s, "GET",
                               "/overtime-responsibility?at=2026-10-01T00:00:00Z", actor="pr1")
        self.assertEqual(200, status)
        self.assertEqual([], payload["items"])

    def test_appeals_progress_view_scoped_by_role(self):
        status, payload = call(self.s, "GET", "/appeals", actor="pd1")
        self.assertEqual(200, status)
        self.assertEqual([], payload["items"])
        status, payload = call(self.s, "GET", "/appeals", actor="pr1")
        self.assertEqual(200, status)

    def test_invalid_body_shape_returns_400(self):
        status, payload = call(self.s, "POST", "/carriers",
                               {"request_id": "x"}, "admin")
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def test_idempotent_replay_returns_200(self):
        body = {"request_id": "dup", "carrier_id": "c9", "name": "新承运"}
        first = call(self.s, "POST", "/carriers", body, "admin")
        second = call(self.s, "POST", "/carriers", body, "admin")
        self.assertEqual(201, first[0])
        self.assertEqual(200, second[0])
        self.assertTrue(second[1]["replayed"])


if __name__ == "__main__":
    unittest.main()
