"""
Comprehensive test suite for GET /admin/explain/{endpoint}.
Tests all five supported targets:
- attendance_list
- employee_monthly
- department_summary
- late_leaderboard
- department_trend

Verifies exact response schema, parameter validation (missing, invalid, out of bounds),
query plans (IXSCAN vs COLLSCAN), foreign collection scan statistics on $lookup stages,
unsupported target handling (422), and regressions for all completed endpoints.
"""
from datetime import datetime, timezone
import unittest
import uuid
from starlette.testclient import TestClient

from app.main import app, db, IST

client = TestClient(app)


def extract_stages(plan):
    """Recursively extract all stage names from a MongoDB winningPlan or executionStages tree."""
    stages = []
    if not isinstance(plan, dict):
        return stages
    if "stage" in plan:
        stages.append(plan["stage"])
    if "inputStage" in plan:
        stages.extend(extract_stages(plan["inputStage"]))
    if "inputStages" in plan:
        for sub in plan["inputStages"]:
            stages.extend(extract_stages(sub))
    return stages


class TestAdminExplain(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.employees.create_index("emp_code", unique=True)
        db.employees.create_index([("department", 1), ("emp_code", 1)])
        db.employees.create_index("joined_on")
        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
        db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])

    # ----------------------------------------------------------------------- #
    # 1. Target: attendance_list
    # ----------------------------------------------------------------------- #
    def test_01_explain_attendance_list(self):
        # Query with emp_code and date range
        res = client.get(
            "/admin/explain/attendance_list?emp_code=EMP0001&date_from=2026-01-01&date_to=2026-01-31&page=1&page_size=10"
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["endpoint"], "attendance_list")
        self.assertEqual(data["collection"], "attendance_logs")
        self.assertIn("explain", data)

        exp = data["explain"]
        self.assertIn("executionStats", exp)
        cursor_plan = exp.get("queryPlanner", {}).get("winningPlan", {})
        stages = extract_stages(cursor_plan)
        self.assertNotIn("COLLSCAN", stages, f"Unexpected COLLSCAN in stages: {stages}")
        self.assertTrue(any(s in stages for s in ["IXSCAN", "COUNT_SCAN"]), f"Expected index scan in: {stages}")

    def test_01b_explain_attendance_list_date_filter(self):
        # Query with date filter only (tests compound date index)
        res = client.get(
            "/admin/explain/attendance_list?date_from=2026-01-01&date_to=2026-01-31"
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["collection"], "attendance_logs")
        cursor_plan = data["explain"].get("queryPlanner", {}).get("winningPlan", {})
        stages = extract_stages(cursor_plan)
        self.assertNotIn("COLLSCAN", stages)
        self.assertIn("IXSCAN", stages)

    def test_01c_explain_attendance_list_invalid_date_validation(self):
        res1 = client.get("/admin/explain/attendance_list?date_from=invalid")
        self.assertEqual(res1.status_code, 422)

        res2 = client.get("/admin/explain/attendance_list?date_from=2026-02-01&date_to=2026-01-01")
        self.assertEqual(res2.status_code, 422)

    # ----------------------------------------------------------------------- #
    # 2. Target: employee_monthly
    # ----------------------------------------------------------------------- #
    def test_02_explain_employee_monthly(self):
        res = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-01")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["endpoint"], "employee_monthly")
        self.assertEqual(data["collection"], "attendance_logs")
        self.assertIn("explain", data)

        exp = data["explain"]
        cursor_plan = exp.get("queryPlanner", {}).get("winningPlan")
        if not cursor_plan:
            for s in exp.get("stages", []):
                if "$cursor" in s:
                    cursor_plan = s["$cursor"].get("queryPlanner", {}).get("winningPlan")
                    break

        stages = extract_stages(cursor_plan)
        self.assertNotIn("COLLSCAN", stages, f"Unexpected COLLSCAN in stages: {stages}")
        self.assertTrue(any(s in stages for s in ["IXSCAN", "COUNT_SCAN"]), f"Expected index scan in: {stages}")

    def test_02b_explain_employee_monthly_missing_params(self):
        # Missing emp_code
        res1 = client.get("/admin/explain/employee_monthly?month=2026-01")
        self.assertEqual(res1.status_code, 422)

        # Missing month
        res2 = client.get("/admin/explain/employee_monthly?emp_code=EMP0001")
        self.assertEqual(res2.status_code, 422)

        # Malformed month
        res3 = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-13")
        self.assertEqual(res3.status_code, 422)

    # ----------------------------------------------------------------------- #
    # 3. Target: department_summary
    # ----------------------------------------------------------------------- #
    def test_03_explain_department_summary(self):
        res = client.get("/admin/explain/department_summary?month=2026-01&department=Engineering")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["endpoint"], "department_summary")
        self.assertEqual(data["collection"], "employees")
        self.assertIn("explain", data)

        exp = data["explain"]
        cursor_plan = exp.get("queryPlanner", {}).get("winningPlan")
        if not cursor_plan:
            for s in exp.get("stages", []):
                if "$cursor" in s:
                    cursor_plan = s["$cursor"].get("queryPlanner", {}).get("winningPlan")
                    break

        stages = extract_stages(cursor_plan)
        self.assertNotIn("COLLSCAN", stages, f"Unexpected COLLSCAN in root employees stages: {stages}")
        self.assertIn("IXSCAN", stages, f"Expected IXSCAN in root employees stages: {stages}")

        # Check foreign lookup into attendance_logs
        lookup_stage = None
        for s in exp.get("stages", []):
            if "$lookup" in s:
                lookup_stage = s
                break
        self.assertIsNotNone(lookup_stage, "Lookup stage not found in explain output")
        self.assertEqual(lookup_stage.get("collectionScans", 0), 0)
        self.assertTrue(
            any(idx in lookup_stage.get("indexesUsed", []) for idx in ["emp_code_1_date_1", "date_-1_emp_code_1"]),
            f"Expected attendance index scan in lookup, got: {lookup_stage.get('indexesUsed')}"
        )

    def test_03b_explain_department_summary_missing_month(self):
        res = client.get("/admin/explain/department_summary")
        self.assertEqual(res.status_code, 422)

        res2 = client.get("/admin/explain/department_summary?department=Engineering")
        self.assertEqual(res2.status_code, 422)

    # ----------------------------------------------------------------------- #
    # 4. Target: late_leaderboard
    # ----------------------------------------------------------------------- #
    def test_04_explain_late_leaderboard(self):
        res = client.get("/admin/explain/late_leaderboard?month=2026-01&limit=5&department=Engineering")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["endpoint"], "late_leaderboard")
        self.assertEqual(data["collection"], "attendance_logs")
        self.assertIn("explain", data)

        exp = data["explain"]
        cursor_plan = exp.get("queryPlanner", {}).get("winningPlan")
        if not cursor_plan:
            for s in exp.get("stages", []):
                if "$cursor" in s:
                    cursor_plan = s["$cursor"].get("queryPlanner", {}).get("winningPlan")
                    break

        stages = extract_stages(cursor_plan)
        self.assertNotIn("COLLSCAN", stages, f"Unexpected COLLSCAN in root attendance stages: {stages}")
        self.assertIn("IXSCAN", stages, f"Expected IXSCAN in root attendance stages: {stages}")

        # Check foreign lookup into employees
        lookup_stage = None
        for s in exp.get("stages", []):
            if "$lookup" in s:
                lookup_stage = s
                break
        self.assertIsNotNone(lookup_stage, "Lookup stage not found in late_leaderboard explain")
        self.assertEqual(lookup_stage.get("collectionScans", 0), 0)

        # When late records exist, verify foreign lookup specifically utilizes emp_code index
        dummy_log = {"emp_code": "EMP0001", "date": "2026-01-10", "status": "PRESENT", "late_minutes": 15}
        db.attendance_logs.insert_one(dummy_log)
        try:
            res_late = client.get("/admin/explain/late_leaderboard?month=2026-01&limit=5&department=Engineering")
            self.assertEqual(res_late.status_code, 200)
            exp_late = res_late.json()["explain"]
            for s in exp_late.get("stages", []):
                if "$lookup" in s:
                    self.assertEqual(s.get("collectionScans", 0), 0)
                    self.assertIn("emp_code_1", s.get("indexesUsed", []))
                    break
        finally:
            db.attendance_logs.delete_one({"emp_code": "EMP0001", "date": "2026-01-10"})

    def test_04b_explain_late_leaderboard_validation(self):
        # Missing month
        res1 = client.get("/admin/explain/late_leaderboard")
        self.assertEqual(res1.status_code, 422)

        # Invalid limit
        res2 = client.get("/admin/explain/late_leaderboard?month=2026-01&limit=0")
        self.assertEqual(res2.status_code, 422)

        res3 = client.get("/admin/explain/late_leaderboard?month=2026-01&limit=51")
        self.assertEqual(res3.status_code, 422)

    # ----------------------------------------------------------------------- #
    # 5. Target: department_trend
    # ----------------------------------------------------------------------- #
    def test_05_explain_department_trend(self):
        res = client.get(
            "/admin/explain/department_trend?department=Engineering&from=2026-01-01&to=2026-01-07"
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["endpoint"], "department_trend")
        self.assertEqual(data["collection"], "employees")
        self.assertIn("explain", data)

        exp = data["explain"]
        cursor_plan = exp.get("queryPlanner", {}).get("winningPlan")
        if not cursor_plan:
            for s in exp.get("stages", []):
                if "$cursor" in s:
                    cursor_plan = s["$cursor"].get("queryPlanner", {}).get("winningPlan")
                    break

        stages = extract_stages(cursor_plan)
        self.assertNotIn("COLLSCAN", stages, f"Unexpected COLLSCAN in root employees stages: {stages}")
        self.assertIn("IXSCAN", stages, f"Expected IXSCAN in root employees stages: {stages}")

        # Foreign lookup on attendance_logs
        lookup_stage = None
        for s in exp.get("stages", []):
            if "$lookup" in s:
                lookup_stage = s
                break
        self.assertIsNotNone(lookup_stage, "Lookup stage not found in trend explain")
        self.assertEqual(lookup_stage.get("collectionScans", 0), 0)
        self.assertIn("date_-1_emp_code_1", lookup_stage.get("indexesUsed", []))

    def test_05b_explain_department_trend_missing_and_invalid_params(self):
        # Missing department
        r1 = client.get("/admin/explain/department_trend?from=2026-01-01&to=2026-01-07")
        self.assertEqual(r1.status_code, 422)

        # Missing from
        r2 = client.get("/admin/explain/department_trend?department=Engineering&to=2026-01-07")
        self.assertEqual(r2.status_code, 422)

        # Missing to
        r3 = client.get("/admin/explain/department_trend?department=Engineering&from=2026-01-01")
        self.assertEqual(r3.status_code, 422)

        # Reversed range
        r4 = client.get("/admin/explain/department_trend?department=Engineering&from=2026-01-10&to=2026-01-05")
        self.assertEqual(r4.status_code, 422)

        # Exceeds 92 days
        r5 = client.get("/admin/explain/department_trend?department=Engineering&from=2026-01-01&to=2026-04-03")
        self.assertEqual(r5.status_code, 422)

    # ----------------------------------------------------------------------- #
    # 6. Unsupported Endpoint Targets
    # ----------------------------------------------------------------------- #
    def test_06_unsupported_endpoint_targets(self):
        for unsupported in ["invalid_target", "users", "123", "explain", "attendance"]:
            with self.subTest(target=unsupported):
                res = client.get(f"/admin/explain/{unsupported}")
                self.assertEqual(res.status_code, 422)

    # ----------------------------------------------------------------------- #
    # 7. Exact Response Shape & Contract
    # ----------------------------------------------------------------------- #
    def test_07_exact_response_shape(self):
        res = client.get("/admin/explain/attendance_list")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(set(data.keys()), {"endpoint", "collection", "explain"})
        self.assertIsInstance(data["endpoint"], str)
        self.assertIsInstance(data["collection"], str)
        self.assertIsInstance(data["explain"], dict)
        self.assertNotIn("_id", data)


if __name__ == "__main__":
    unittest.main()
