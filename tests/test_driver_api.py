import unittest
from datetime import datetime, timezone

from driver_rights.api import route
from driver_rights.service import DriverRightsService
from driver_rights.storage import DriverRightsDatabase
from transport_coordination.clock import ManualClock

TERMS = {"minimums": {"per_trip_cents": 30000}, "appeal_window_hours": 168}


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.database = DriverRightsDatabase()
        self.clock = ManualClock(datetime(2026, 9, 30, 23, 0, tzinfo=timezone.utc))
        self.service = DriverRightsService(self.database, self.clock)
        s = self.service
        s.register_organization(request_id="org-c", actor_id="bootstrap",
                                organization_id="org-c", name="承运企业")
        s.register_actor(request_id="a-admin", actor_id="bootstrap", new_actor_id="admin1",
                         display_name="管理员", role="admin", organization_id="org-c")
        s.register_actor(request_id="a-carrier", actor_id="admin1", new_actor_id="carrier1",
                         display_name="调度", role="carrier", organization_id="org-c")
        s.register_actor(request_id="a-d1", actor_id="admin1", new_actor_id="drv1",
                         display_name="司机", role="driver", organization_id="org-c")
        s.register_actor(request_id="a-reg", actor_id="admin1", new_actor_id="reg1",
                         display_name="监管", role="regulator", organization_id="org-c")
        s.register_driver(request_id="d-1", actor_id="carrier1", driver_id="drv1",
                          license_no="L0001", name="司机")
        s.publish_regulation(request_id="reg-1", actor_id="reg1",
                             regulation_id="hos-default", rules={})
        s.publish_contract(request_id="c-1", actor_id="carrier1", driver_id="drv1",
                           effective_from="2026-09-01T00:00:00Z", terms=TERMS)

    def tearDown(self):
        self.database.close()

    def post(self, path, body, actor="carrier1"):
        return route(self.service, "POST", path, body, {"X-Actor-Id": actor})

    def get(self, path, actor="carrier1"):
        return route(self.service, "GET", path, None, {"X-Actor-Id": actor})

    def test_health_delegates_to_base(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])
        self.assertTrue(payload["audit_valid"])

    def test_base_registration_still_available(self):
        status, payload = self.post("/organizations", {
            "request_id": "org-x", "organization_id": "org-x", "name": "新企业"}, actor="admin1")
        self.assertEqual(201, status)
        self.assertEqual("org-x", payload["resource_id"])

    def test_task_lifecycle_over_http(self):
        status, payload = self.post("/driver-rights/tasks", {
            "request_id": "t-1", "task_id": "task-1", "origin": "甲地", "destination": "乙地",
            "planned_start": "2026-10-01T00:00:00Z", "planned_end": "2026-10-01T04:00:00Z",
            "base_freight_cents": 100000})
        self.assertEqual(201, status)
        replay, _ = self.post("/driver-rights/tasks", {
            "request_id": "t-1", "task_id": "task-1", "origin": "甲地", "destination": "乙地",
            "planned_start": "2026-10-01T00:00:00Z", "planned_end": "2026-10-01T04:00:00Z",
            "base_freight_cents": 100000})
        self.assertEqual(200, replay)
        status, payload = self.post("/driver-rights/tasks/assign", {
            "request_id": "as-1", "task_id": "task-1", "driver_id": "drv1"})
        self.assertEqual(201, status)
        status, _ = self.post("/driver-rights/tasks/accept", {
            "request_id": "ac-1", "task_id": "task-1"}, actor="drv1")
        self.assertEqual(201, status)
        status, payload = self.get("/driver-rights/tasks/available", actor="drv1")
        self.assertEqual(200, status)
        self.assertEqual("active", payload["items"][0]["assignment_status"])
        status, payload = self.get("/driver-rights/tasks/detail?task_id=task-1", actor="reg1")
        self.assertEqual(200, status)
        self.assertEqual("assigned", payload["status"])

    def test_permission_error_maps_to_403(self):
        status, payload = self.post("/driver-rights/tasks", {
            "request_id": "t-9", "task_id": "task-9", "origin": "甲", "destination": "乙",
            "planned_start": "2026-10-01T00:00:00Z", "planned_end": "2026-10-01T01:00:00Z",
            "base_freight_cents": 1}, actor="drv1")
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_validation_error_maps_to_400(self):
        status, payload = self.post("/driver-rights/tasks", {
            "request_id": "t-9", "task_id": "task-9", "origin": "甲", "destination": "乙",
            "planned_start": "2026-10-01T01:00:00Z", "planned_end": "2026-10-01T00:00:00Z",
            "base_freight_cents": 1})
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_unknown_driver_rights_route_returns_404(self):
        status, payload = self.get("/driver-rights/missing")
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_missing_actor_is_404(self):
        status, payload = self.get("/driver-rights/tasks/available", actor="")
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])

    def test_invalid_body_shape_returns_400(self):
        status, payload = self.post("/driver-rights/tasks", {"request_id": "only"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])


if __name__ == "__main__":
    unittest.main()
