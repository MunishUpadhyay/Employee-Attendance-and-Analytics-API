"""
Comprehensive test suite for GET /analytics/leaderboard/late.
Covers contract validation, standard competition ranking, ties, limit cutoffs,
department filtering, empty states, boundary cases, explain plans, and regressions.
"""
import calendar
from datetime import datetime, timezone
import unittest
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


class TestLateLeaderboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.employees.create_index("emp_code", unique=True)
        db.employees.create_index([("department", 1), ("emp_code", 1)])
        db.employees.create_index("joined_on")
        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
        db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])

    # ----------------------------------------------------------------------- #
    # 1. Validation and Contract
    # ----------------------------------------------------------------------- #
    def test_01_valid_request_baseline(self):
        res = client.get("/analytics/leaderboard/late?month=2026-01")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["month"], "2026-01")
        self.assertIn("items", data)
        self.assertIsInstance(data["items"], list)

    def test_02_missing_month_parameter(self):
        res = client.get("/analytics/leaderboard/late")
        self.assertEqual(res.status_code, 422)

    def test_03_malformed_month_parameter(self):
        invalid_months = ["2026-13", "2026-00", "2026/01", "2026-1", "invalid", "2026-01-01"]
        for inv in invalid_months:
            with self.subTest(invalid_month=inv):
                res = client.get(f"/analytics/leaderboard/late?month={inv}")
                self.assertEqual(res.status_code, 422)

    def test_04_limit_parameter_bounds(self):
        # limit < 1 -> 422
        res = client.get("/analytics/leaderboard/late?month=2026-01&limit=0")
        self.assertEqual(res.status_code, 422)

        # limit > 50 -> 422
        res = client.get("/analytics/leaderboard/late?month=2026-01&limit=51")
        self.assertEqual(res.status_code, 422)

        # limit negative -> 422
        res = client.get("/analytics/leaderboard/late?month=2026-01&limit=-1")
        self.assertEqual(res.status_code, 422)

        # limit non-integer -> 422
        res = client.get("/analytics/leaderboard/late?month=2026-01&limit=abc")
        self.assertEqual(res.status_code, 422)

        # limit 1 and 50 are valid
        res = client.get("/analytics/leaderboard/late?month=2026-01&limit=1")
        self.assertEqual(res.status_code, 200)
        res = client.get("/analytics/leaderboard/late?month=2026-01&limit=50")
        self.assertEqual(res.status_code, 200)

    def test_05_schema_and_no_id_leak(self):
        res = client.get("/analytics/leaderboard/late?month=2026-01")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(set(data.keys()), {"month", "items"})
        required_item_keys = {"rank", "emp_code", "name", "department", "total_late_minutes", "late_count"}
        for item in data["items"]:
            self.assertEqual(set(item.keys()), required_item_keys)
            self.assertNotIn("_id", item)
            self.assertIsInstance(item["rank"], int)
            self.assertGreaterEqual(item["rank"], 1)
            self.assertIsInstance(item["emp_code"], str)
            self.assertIsInstance(item["name"], str)
            self.assertIsInstance(item["department"], str)
            self.assertIsInstance(item["total_late_minutes"], int)
            self.assertGreater(item["total_late_minutes"], 0)
            self.assertIsInstance(item["late_count"], int)
            self.assertGreater(item["late_count"], 0)

    # ----------------------------------------------------------------------- #
    # 2. Ranking, Ties, Cutoffs, Department Filter, and Edge Cases
    # ----------------------------------------------------------------------- #
    def test_leaderboard_competition_ranking_and_ties(self):
        month = "2026-10"
        test_dept_a = "LeaderboardDeptA"
        test_dept_b = "LeaderboardDeptB"

        test_emps = [
            {"emp_code": "EMPLB01", "name": "Emp One", "email": "lb1@test.com", "department": test_dept_a, "shift_start": "09:00", "shift_end": "18:00", "joined_on": "2026-01-01"},
            {"emp_code": "EMPLB02", "name": "Emp Two", "email": "lb2@test.com", "department": test_dept_a, "shift_start": "09:00", "shift_end": "18:00", "joined_on": "2026-01-01"},
            {"emp_code": "EMPLB03", "name": "Emp Three", "email": "lb3@test.com", "department": test_dept_a, "shift_start": "09:00", "shift_end": "18:00", "joined_on": "2026-01-01"},
            {"emp_code": "EMPLB04", "name": "Emp Four", "email": "lb4@test.com", "department": test_dept_a, "shift_start": "09:00", "shift_end": "18:00", "joined_on": "2026-01-01"},
            {"emp_code": "EMPLB05", "name": "Emp Five", "email": "lb5@test.com", "department": test_dept_a, "shift_start": "09:00", "shift_end": "18:00", "joined_on": "2026-01-01"},
            {"emp_code": "EMPLB06", "name": "Emp Zero Late", "email": "lb6@test.com", "department": test_dept_a, "shift_start": "09:00", "shift_end": "18:00", "joined_on": "2026-01-01"},
            {"emp_code": "EMPLB07", "name": "Emp Dept B", "email": "lb7@test.com", "department": test_dept_b, "shift_start": "09:00", "shift_end": "18:00", "joined_on": "2026-01-01"},
        ]
        all_emp_codes = [e["emp_code"] for e in test_emps] + ["EMPLBGhost"]

        db.employees.delete_many({"emp_code": {"$in": all_emp_codes}})
        db.attendance_logs.delete_many({"emp_code": {"$in": all_emp_codes}})

        try:
            db.employees.insert_many(test_emps)

            # Insert attendance logs:
            # EMPLB01: 150 total late minutes (100 on 2026-10-01, 50 on 2026-10-02) -> 2 logs
            # EMPLB02: 100 total late minutes (100 on 2026-10-05) -> 1 log
            # EMPLB03: 100 total late minutes (60 on 2026-10-06, 40 on 2026-10-07) -> 2 logs (Tied with EMPLB02!)
            # EMPLB04: 50 total late minutes (50 on 2026-10-08) -> 1 log
            # EMPLB05: 20 total late minutes (20 on 2026-10-09) -> 1 log
            # EMPLB06: 0 late minutes -> MUST NOT be in leaderboard!
            # EMPLB07: 120 total late minutes in Dept B
            # EMPLBGhost: 300 late minutes, but employee does NOT exist in employees collection -> MUST be ignored!
            # Outside boundaries:
            # EMPLB01: 100 late minutes on 2026-09-30 (September) -> excluded
            # EMPLB01: 100 late minutes on 2026-11-01 (November) -> excluded
            test_logs = [
                {"emp_code": "EMPLB01", "date": "2026-10-01", "late_minutes": 100},
                {"emp_code": "EMPLB01", "date": "2026-10-02", "late_minutes": 50},
                {"emp_code": "EMPLB02", "date": "2026-10-05", "late_minutes": 100},
                {"emp_code": "EMPLB03", "date": "2026-10-06", "late_minutes": 60},
                {"emp_code": "EMPLB03", "date": "2026-10-07", "late_minutes": 40},
                {"emp_code": "EMPLB04", "date": "2026-10-08", "late_minutes": 50},
                {"emp_code": "EMPLB05", "date": "2026-10-09", "late_minutes": 20},
                {"emp_code": "EMPLB06", "date": "2026-10-10", "late_minutes": 0},
                {"emp_code": "EMPLB07", "date": "2026-10-11", "late_minutes": 120},
                {"emp_code": "EMPLBGhost", "date": "2026-10-12", "late_minutes": 300},
                {"emp_code": "EMPLB01", "date": "2026-09-30", "late_minutes": 100},
                {"emp_code": "EMPLB01", "date": "2026-11-01", "late_minutes": 100},
            ]
            db.attendance_logs.insert_many(test_logs)

            # Case A: Filter by Dept A with limit=10
            # Expected in Dept A:
            # Rank 1: EMPLB01 (150 mins, late_count=2)
            # Rank 2: EMPLB02 (100 mins, late_count=1) -> tied with EMPLB03, sorted by emp_code ascending
            # Rank 2: EMPLB03 (100 mins, late_count=2)
            # Rank 4: EMPLB04 (50 mins, late_count=1) -> Rank 3 skipped due to competition ranking!
            # Rank 5: EMPLB05 (20 mins, late_count=1)
            # Total 5 rows. EMPLB06 (0 min) and Ghost excluded.
            res = client.get(f"/analytics/leaderboard/late?month={month}&department={test_dept_a}&limit=10")
            self.assertEqual(res.status_code, 200)
            items = res.json()["items"]
            self.assertEqual(len(items), 5)

            self.assertEqual(items[0]["emp_code"], "EMPLB01")
            self.assertEqual(items[0]["rank"], 1)
            self.assertEqual(items[0]["total_late_minutes"], 150)
            self.assertEqual(items[0]["late_count"], 2)

            # Tied rank 2
            self.assertEqual(items[1]["emp_code"], "EMPLB02")
            self.assertEqual(items[1]["rank"], 2)
            self.assertEqual(items[1]["total_late_minutes"], 100)

            self.assertEqual(items[2]["emp_code"], "EMPLB03")
            self.assertEqual(items[2]["rank"], 2)
            self.assertEqual(items[2]["total_late_minutes"], 100)

            # Competition ranking skips rank 3!
            self.assertEqual(items[3]["emp_code"], "EMPLB04")
            self.assertEqual(items[3]["rank"], 4)
            self.assertEqual(items[3]["total_late_minutes"], 50)

            self.assertEqual(items[4]["emp_code"], "EMPLB05")
            self.assertEqual(items[4]["rank"], 5)
            self.assertEqual(items[4]["total_late_minutes"], 20)

            # Case B: Limit cutoff behavior with ties
            # limit=2: Employees with rank <= 2 should be returned.
            # EMPLB01 (rank 1), EMPLB02 (rank 2), EMPLB03 (rank 2).
            # Result MUST contain 3 items, exceeding limit=2!
            res_lim2 = client.get(f"/analytics/leaderboard/late?month={month}&department={test_dept_a}&limit=2")
            self.assertEqual(res_lim2.status_code, 200)
            items_lim2 = res_lim2.json()["items"]
            self.assertEqual(len(items_lim2), 3)
            self.assertEqual([i["emp_code"] for i in items_lim2], ["EMPLB01", "EMPLB02", "EMPLB03"])
            self.assertEqual([i["rank"] for i in items_lim2], [1, 2, 2])

            # limit=3: Employees with rank <= 3.
            # Since rank 3 is skipped, ranks 1, 2, 2 are still the only ones <= 3!
            res_lim3 = client.get(f"/analytics/leaderboard/late?month={month}&department={test_dept_a}&limit=3")
            self.assertEqual(res_lim3.status_code, 200)
            items_lim3 = res_lim3.json()["items"]
            self.assertEqual(len(items_lim3), 3)

            # limit=4: Returns ranks 1, 2, 2, 4 (4 rows)
            res_lim4 = client.get(f"/analytics/leaderboard/late?month={month}&department={test_dept_a}&limit=4")
            self.assertEqual(res_lim4.status_code, 200)
            items_lim4 = res_lim4.json()["items"]
            self.assertEqual(len(items_lim4), 4)

            # Case C: Department B ranking happens inside Department B only
            # In Dept B, EMPLB07 has 120 mins -> MUST be rank 1 (not rank 2 from global list)
            res_dept_b = client.get(f"/analytics/leaderboard/late?month={month}&department={test_dept_b}")
            self.assertEqual(res_dept_b.status_code, 200)
            items_b = res_dept_b.json()["items"]
            self.assertEqual(len(items_b), 1)
            self.assertEqual(items_b[0]["emp_code"], "EMPLB07")
            self.assertEqual(items_b[0]["rank"], 1)
            self.assertEqual(items_b[0]["department"], test_dept_b)

            # Case D: Non-existent department returns empty items []
            res_nonexistent = client.get(f"/analytics/leaderboard/late?month={month}&department=NonExistentDept99")
            self.assertEqual(res_nonexistent.status_code, 200)
            self.assertEqual(res_nonexistent.json()["items"], [])

            # Case E: Month with zero late records
            res_empty_month = client.get("/analytics/leaderboard/late?month=2025-01")
            self.assertEqual(res_empty_month.status_code, 200)
            self.assertEqual(res_empty_month.json()["items"], [])

        finally:
            db.employees.delete_many({"emp_code": {"$in": all_emp_codes}})
            db.attendance_logs.delete_many({"emp_code": {"$in": all_emp_codes}})

    # ----------------------------------------------------------------------- #
    # 3. Query Plan & Index Verification
    # ----------------------------------------------------------------------- #
    def test_explain_plan_late_leaderboard(self):
        from app.main import build_late_leaderboard_pipeline

        month = "2026-01"
        pipeline = build_late_leaderboard_pipeline(month, limit=10, department=None)

        exp = db.command('explain', {
            'aggregate': 'attendance_logs',
            'pipeline': pipeline,
            'cursor': {}
        }, verbosity='executionStats')

        # Check cursor stages on attendance_logs
        cursor_plan = exp.get('queryPlanner', {}).get('winningPlan')
        if not cursor_plan:
            for s in exp.get('stages', []):
                if '$cursor' in s:
                    cursor_plan = s['$cursor'].get('queryPlanner', {}).get('winningPlan')
                    break

        stages = extract_stages(cursor_plan)
        self.assertNotIn('COLLSCAN', stages, f"Unexpected COLLSCAN in attendance_logs stages: {stages}")
        self.assertIn('IXSCAN', stages, f"Expected IXSCAN in attendance_logs stages: {stages}")

        # Check $lookup into employees
        lookup_stage = None
        for s in exp.get('stages', []):
            if '$lookup' in s:
                lookup_stage = s
                break
        self.assertIsNotNone(lookup_stage, "Lookup stage not found in explain output")
        self.assertEqual(lookup_stage.get('collectionScans', 0), 0)

    # ----------------------------------------------------------------------- #
    # 4. Regression Testing Across All Completed Endpoints
    # ----------------------------------------------------------------------- #
    def test_regression_all_endpoints(self):
        # 1. GET /health
        r = client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok"})

        # 2. GET /employees
        r = client.get("/employees")
        self.assertEqual(r.status_code, 200)
        self.assertIn("items", r.json())
        self.assertIn("total", r.json())

        # 3. POST /employees
        unique_code = f"EMP{int(datetime.now().timestamp()) % 1000000:06d}"
        r = client.post("/employees", json={
            "emp_code": unique_code,
            "name": "Regression Test Emp",
            "email": f"reg_{unique_code}@example.com",
            "department": "QA",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["emp_code"], unique_code)

        # 4. POST /attendance/punch-in
        now_ms = int(datetime(2026, 1, 15, 9, 30, tzinfo=IST).timestamp() * 1000)
        r = client.post("/attendance/punch-in", json={
            "emp_code": unique_code,
            "punched_at": now_ms,
            "status": "PRESENT"
        })
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["emp_code"], unique_code)

        # 5. POST /attendance/punch-out
        out_ms = int(datetime(2026, 1, 15, 18, 30, tzinfo=IST).timestamp() * 1000)
        r = client.post("/attendance/punch-out", json={
            "emp_code": unique_code,
            "punched_at": out_ms
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["work_hours"], 9.0)

        # 6. GET /attendance
        r = client.get(f"/attendance?emp_code={unique_code}")
        self.assertEqual(r.status_code, 200)
        self.assertGreaterEqual(r.json()["total"], 1)

        # 7. PATCH /attendance/{emp_code}/{date}
        r = client.patch(f"/attendance/{unique_code}/2026-01-15", json={
            "reason": "Regularizing punch out time",
            "regularized_by": "Manager Bob",
            "punch_out": int(datetime(2026, 1, 15, 19, 0, tzinfo=IST).timestamp() * 1000)
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["history"]), 1)

        # 8. GET /analytics/employees/{emp_code}/monthly
        r = client.get(f"/analytics/employees/{unique_code}/monthly?month=2026-01")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["emp_code"], unique_code)
        self.assertEqual(r.json()["month"], "2026-01")

        # 9. GET /analytics/departments/summary
        r = client.get("/analytics/departments/summary?month=2026-01")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["month"], "2026-01")

        # 10. GET /analytics/leaderboard/late
        r = client.get("/analytics/leaderboard/late?month=2026-01")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["month"], "2026-01")

        # Clean up
        db.employees.delete_one({"emp_code": unique_code})
        db.attendance_logs.delete_many({"emp_code": unique_code})


if __name__ == "__main__":
    unittest.main()
