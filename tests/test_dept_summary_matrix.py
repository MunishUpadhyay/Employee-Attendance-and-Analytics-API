"""
Comprehensive edge-case test matrix and regression test suite
for GET /analytics/departments/summary (Phase 3 Step 2).
Uses standard library unittest and starlette / httpx TestClient.
"""
import calendar
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
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


class TestDepartmentSummaryMatrix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.employees.create_index("emp_code", unique=True)
        db.employees.create_index([("department", 1), ("emp_code", 1)])
        db.employees.create_index("joined_on")
        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
        db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])

    # ----------------------------------------------------------------------- #
    # Group A: Contract and Validation
    # ----------------------------------------------------------------------- #
    def test_01_valid_request_normal_data(self):
        res = client.get("/analytics/departments/summary?month=2026-01")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("month", data)
        self.assertEqual(data["month"], "2026-01")
        self.assertIn("items", data)
        self.assertIsInstance(data["items"], list)

    def test_02_missing_month_parameter(self):
        res = client.get("/analytics/departments/summary")
        self.assertEqual(res.status_code, 422)
        self.assertIn("detail", res.json())

    def test_03_malformed_month_inputs(self):
        invalid_months = [
            "2026-13", "2026-00", "2026/01", "2026-1", "26-01", "invalid", "2026-01-01"
        ]
        for inv in invalid_months:
            with self.subTest(invalid_month=inv):
                res = client.get(f"/analytics/departments/summary?month={inv}")
                self.assertEqual(res.status_code, 422)

    def test_04_department_filter_nonexistent(self):
        res = client.get("/analytics/departments/summary?month=2026-01&department=NonExistentDept999")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["month"], "2026-01")
        self.assertEqual(data["items"], [])

    def test_05_06_response_schema_and_no_internal_ids(self):
        res = client.get("/analytics/departments/summary?month=2026-01")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(set(data.keys()), {"month", "items"})
        required_fields = {
            "department", "headcount", "present_days", "avg_work_hours",
            "late_count", "total_late_minutes", "leave_count", "on_duty_count"
        }
        for item in data["items"]:
            self.assertEqual(set(item.keys()), required_fields)
            self.assertNotIn("_id", item)
            self.assertIsInstance(item["department"], str)
            self.assertIsInstance(item["headcount"], int)
            self.assertIsInstance(item["present_days"], (int, float))
            if item["avg_work_hours"] is not None:
                self.assertIsInstance(item["avg_work_hours"], (int, float))
            self.assertIsInstance(item["late_count"], int)
            self.assertIsInstance(item["total_late_minutes"], int)
            self.assertIsInstance(item["leave_count"], int)
            self.assertIsInstance(item["on_duty_count"], int)

    # ----------------------------------------------------------------------- #
    # Group B & C & D & E: Headcount, Statuses, Boundaries, Numerics
    # ----------------------------------------------------------------------- #
    def test_matrix_edge_cases(self):
        test_dept = "TestQA_Dept"
        month = "2026-05" # 31 days. May 1, 2026 is Friday. May 2 Sat, May 3 Sun.
        
        db.employees.delete_many({"department": test_dept})
        test_emp_codes = [f"EMPTEST{i:03d}" for i in range(1, 10)]
        db.attendance_logs.delete_many({"emp_code": {"$in": test_emp_codes}})

        try:
            # 1. Joins on first day (2026-05-01)
            db.employees.insert_one({
                "emp_code": "EMPTEST001",
                "name": "Emp First Day",
                "email": "e1@test.com",
                "department": test_dept,
                "shift_start": "09:00",
                "shift_end": "18:00",
                "joined_on": "2026-05-01",
                "created_at": datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
            })
            # 2. Joins midway (2026-05-15)
            db.employees.insert_one({
                "emp_code": "EMPTEST002",
                "name": "Emp Mid Month",
                "email": "e2@test.com",
                "department": test_dept,
                "shift_start": "09:00",
                "shift_end": "18:00",
                "joined_on": "2026-05-15",
                "created_at": datetime(2026, 5, 15, 0, 0, tzinfo=timezone.utc)
            })
            # 3. Joins exactly on last day (2026-05-31)
            db.employees.insert_one({
                "emp_code": "EMPTEST003",
                "name": "Emp Last Day",
                "email": "e3@test.com",
                "department": test_dept,
                "shift_start": "09:00",
                "shift_end": "18:00",
                "joined_on": "2026-05-31",
                "created_at": datetime(2026, 5, 31, 0, 0, tzinfo=timezone.utc)
            })
            # 4. Joins AFTER period end (2026-06-01) -> MUST NOT be in headcount!
            db.employees.insert_one({
                "emp_code": "EMPTEST004",
                "name": "Emp Future Joiner",
                "email": "e4@test.com",
                "department": test_dept,
                "shift_start": "09:00",
                "shift_end": "18:00",
                "joined_on": "2026-06-01",
                "created_at": datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
            })
            # 5. Zero attendance records employee -> MUST be included in headcount!
            db.employees.insert_one({
                "emp_code": "EMPTEST005",
                "name": "Emp Zero Logs",
                "email": "e5@test.com",
                "department": test_dept,
                "shift_start": "09:00",
                "shift_end": "18:00",
                "joined_on": "2026-05-05",
                "created_at": datetime(2026, 5, 5, 0, 0, tzinfo=timezone.utc)
            })

            # Attendance records for EMPTEST001:
            # - Friday 2026-05-01: PRESENT, full day, work_hours = 8.525, late_minutes = 15
            db.attendance_logs.insert_one({
                "emp_code": "EMPTEST001",
                "date": "2026-05-01",
                "status": "PRESENT",
                "punch_in": datetime(2026, 5, 1, 3, 45, tzinfo=timezone.utc),
                "punch_out": datetime(2026, 5, 1, 12, 16, 30, tzinfo=timezone.utc),
                "work_hours": 8.525,
                "late_minutes": 15,
                "overtime_minutes": 0,
                "half_day": False
            })
            # - Saturday 2026-05-02 (Weekend): PRESENT, work_hours = 4.0, late_minutes = 20
            # Weekend must not increment present_days, but late_minutes AND work_hours are included in their respective totals!
            db.attendance_logs.insert_one({
                "emp_code": "EMPTEST001",
                "date": "2026-05-02",
                "status": "PRESENT",
                "punch_in": datetime(2026, 5, 2, 4, 0, tzinfo=timezone.utc),
                "punch_out": datetime(2026, 5, 2, 8, 0, tzinfo=timezone.utc),
                "work_hours": 4.0,
                "late_minutes": 20,
                "overtime_minutes": 0,
                "half_day": False
            })
            # - Monday 2026-05-04: WFH, half_day = True, work_hours = 4.0, late_minutes = 0
            db.attendance_logs.insert_one({
                "emp_code": "EMPTEST001",
                "date": "2026-05-04",
                "status": "WFH",
                "punch_in": datetime(2026, 5, 4, 3, 30, tzinfo=timezone.utc),
                "punch_out": datetime(2026, 5, 4, 7, 30, tzinfo=timezone.utc),
                "work_hours": 4.0,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": True
            })
            # - Tuesday 2026-05-05: ON_DUTY, missing half_day (legacy default False -> 1.0), work_hours = 8.525, late_minutes = 0
            db.attendance_logs.insert_one({
                "emp_code": "EMPTEST001",
                "date": "2026-05-05",
                "status": "ON_DUTY",
                "punch_in": datetime(2026, 5, 5, 3, 30, tzinfo=timezone.utc),
                "punch_out": datetime(2026, 5, 5, 12, 1, 30, tzinfo=timezone.utc),
                "work_hours": 8.525,
                "late_minutes": 0,
                "overtime_minutes": 0
            })
            # - Wednesday 2026-05-06: LEAVE (does not count as present, adds to leave_count)
            db.attendance_logs.insert_one({
                "emp_code": "EMPTEST001",
                "date": "2026-05-06",
                "status": "LEAVE",
                "punch_in": None,
                "punch_out": None,
                "work_hours": None,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": False
            })
            # - Thursday 2026-05-07: ABSENT (does not count as present)
            db.attendance_logs.insert_one({
                "emp_code": "EMPTEST001",
                "date": "2026-05-07",
                "status": "ABSENT",
                "punch_in": None,
                "punch_out": None,
                "work_hours": None,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": False
            })
            # - Outside boundary: 2026-04-30 (April) -> excluded
            db.attendance_logs.insert_one({
                "emp_code": "EMPTEST001",
                "date": "2026-04-30",
                "status": "PRESENT",
                "punch_in": datetime(2026, 4, 30, 3, 30, tzinfo=timezone.utc),
                "punch_out": datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc),
                "work_hours": 8.5,
                "late_minutes": 50,
                "overtime_minutes": 0,
                "half_day": False
            })
            # - Outside boundary: 2026-06-01 (June) -> excluded
            db.attendance_logs.insert_one({
                "emp_code": "EMPTEST001",
                "date": "2026-06-01",
                "status": "PRESENT",
                "punch_in": datetime(2026, 6, 1, 3, 30, tzinfo=timezone.utc),
                "punch_out": datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
                "work_hours": 8.5,
                "late_minutes": 50,
                "overtime_minutes": 0,
                "half_day": False
            })

            # Attendance records for EMPTEST002:
            # - Friday 2026-05-15: PRESENT, work_hours null, missing late_minutes
            db.attendance_logs.insert_one({
                "emp_code": "EMPTEST002",
                "date": "2026-05-15",
                "status": "PRESENT",
                "punch_in": datetime(2026, 5, 15, 3, 30, tzinfo=timezone.utc),
                "punch_out": None,
                "work_hours": None,
                "half_day": False
            })

            res = client.get(f"/analytics/departments/summary?month={month}&department={test_dept}")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["month"], month)
            self.assertEqual(len(data["items"]), 1)
            dept_item = data["items"][0]

            # 1. Headcount = 4 (EMPTEST001, EMPTEST002, EMPTEST003, EMPTEST005)
            self.assertEqual(dept_item["headcount"], 4)

            # 2. Present days = 1.0 (May 1) + 0.0 (May 2) + 0.5 (May 4) + 1.0 (May 5) + 1.0 (May 15) = 3.5
            self.assertEqual(dept_item["present_days"], 3.5)

            # 3. Late count = 2 (May 1 = 15m, May 2 weekend = 20m)
            self.assertEqual(dept_item["late_count"], 2)
            self.assertEqual(dept_item["total_late_minutes"], 35)

            # 4. Leave & On duty
            self.assertEqual(dept_item["leave_count"], 1)
            self.assertEqual(dept_item["on_duty_count"], 1)

            # 5. Average work hours:
            # Sum = 8.525 + 4.0 + 4.0 + 8.525 = 25.050 across 4 records (including Saturday May 2 per contract)
            # 25.050 / 4 = 6.2625 -> half-up rounded to 6.26
            expected_avg = float((Decimal("25.050") / Decimal(4)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            self.assertEqual(dept_item["avg_work_hours"], expected_avg)

        finally:
            db.employees.delete_many({"department": test_dept})
            db.attendance_logs.delete_many({"emp_code": {"$in": test_emp_codes}})

    def test_rounding_boundary_tie(self):
        test_dept = "TestRoundDept"
        month = "2026-07"
        db.employees.delete_many({"department": test_dept})
        test_emp_code = "EMPTESTROUND"
        db.attendance_logs.delete_many({"emp_code": test_emp_code})

        try:
            db.employees.insert_one({
                "emp_code": test_emp_code,
                "name": "Emp Round",
                "email": "round@test.com",
                "department": test_dept,
                "shift_start": "09:00",
                "shift_end": "18:00",
                "joined_on": "2026-07-01",
                "created_at": datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
            })

            # Exact tie at 2.125:
            # (Decimal("2.10") + Decimal("2.15")) / Decimal("2") = Decimal("2.125")
            # Contract Rule R8 requires ROUND_HALF_UP, yielding exactly 2.13
            expected_exact_dec = (Decimal("2.10") + Decimal("2.15")) / Decimal("2")
            expected_rounded_val = float(expected_exact_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            self.assertEqual(expected_rounded_val, 2.13)
            
            db.attendance_logs.insert_one({
                "emp_code": test_emp_code,
                "date": "2026-07-01",
                "status": "PRESENT",
                "punch_in": datetime(2026, 7, 1, 3, 30, tzinfo=timezone.utc),
                "punch_out": datetime(2026, 7, 1, 5, 36, tzinfo=timezone.utc),
                "work_hours": 2.10,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": True
            })
            db.attendance_logs.insert_one({
                "emp_code": test_emp_code,
                "date": "2026-07-02",
                "status": "PRESENT",
                "punch_in": datetime(2026, 7, 2, 3, 30, tzinfo=timezone.utc),
                "punch_out": datetime(2026, 7, 2, 5, 39, tzinfo=timezone.utc),
                "work_hours": 2.15,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": True
            })

            res = client.get(f"/analytics/departments/summary?month={month}&department={test_dept}")
            self.assertEqual(res.status_code, 200)
            item = res.json()["items"][0]
            # Verify the endpoint returns exactly 2.13 for an exact average of 2.125
            self.assertEqual(item["avg_work_hours"], 2.13)

        finally:
            db.employees.delete_many({"department": test_dept})
            db.attendance_logs.delete_many({"emp_code": test_emp_code})

    def test_zero_work_hours_returns_null(self):
        test_dept = "TestNullAvgDept"
        month = "2026-08"
        db.employees.delete_many({"department": test_dept})
        test_emp_code = "EMPTESTNULLAVG"
        db.attendance_logs.delete_many({"emp_code": test_emp_code})

        try:
            db.employees.insert_one({
                "emp_code": test_emp_code,
                "name": "Emp Null Avg",
                "email": "nullavg@test.com",
                "department": test_dept,
                "shift_start": "09:00",
                "shift_end": "18:00",
                "joined_on": "2026-08-01",
                "created_at": datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
            })

            db.attendance_logs.insert_one({
                "emp_code": test_emp_code,
                "date": "2026-08-01",
                "status": "LEAVE",
                "punch_in": None,
                "punch_out": None,
                "work_hours": None,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": False
            })

            res = client.get(f"/analytics/departments/summary?month={month}&department={test_dept}")
            self.assertEqual(res.status_code, 200)
            item = res.json()["items"][0]
            self.assertEqual(item["headcount"], 1)
            self.assertEqual(item["present_days"], 0.0)
            self.assertIsNone(item["avg_work_hours"])
            self.assertEqual(item["leave_count"], 1)

        finally:
            db.employees.delete_many({"department": test_dept})
            db.attendance_logs.delete_many({"emp_code": test_emp_code})

    # ----------------------------------------------------------------------- #
    # Group F: Explain Plan Verification
    # ----------------------------------------------------------------------- #
    def test_explain_plan_no_collscan_and_lookup_index(self):
        month = "2026-01"
        _, last_day = calendar.monthrange(2026, 1)

        # 1. Pipeline without department
        pipeline_no_dept = [
            {"$match": {"joined_on": {"$lte": f"{month}-{last_day:02d}"}}},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "pipeline": [
                        {"$match": {"date": {"$gte": f"{month}-01", "$lte": f"{month}-{last_day:02d}"}}}
                    ],
                    "as": "stats"
                }
            }
        ]

        exp1 = db.command('explain', {
            'aggregate': 'employees',
            'pipeline': pipeline_no_dept,
            'cursor': {}
        }, verbosity='executionStats')

        cursor_plan1 = exp1.get('queryPlanner', {}).get('winningPlan')
        if not cursor_plan1:
            for s in exp1.get('stages', []):
                if '$cursor' in s:
                    cursor_plan1 = s['$cursor'].get('queryPlanner', {}).get('winningPlan')
                    break

        stages1 = extract_stages(cursor_plan1)
        self.assertNotIn('COLLSCAN', stages1, f"Unexpected COLLSCAN in stages: {stages1}")
        self.assertIn('IXSCAN', stages1, f"Expected IXSCAN in stages: {stages1}")

        lookup_stage1 = None
        for s in exp1.get('stages', []):
            if '$lookup' in s:
                lookup_stage1 = s
                break
        self.assertIsNotNone(lookup_stage1, "Lookup stage not found in explain output")
        self.assertIn('emp_code_1_date_1', lookup_stage1.get('indexesUsed', []))
        self.assertEqual(lookup_stage1.get('collectionScans', 0), 0)

        # 2. Pipeline with department
        pipeline_with_dept = [
            {"$match": {"department": "Engineering", "joined_on": {"$lte": f"{month}-{last_day:02d}"}}},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "pipeline": [
                        {"$match": {"date": {"$gte": f"{month}-01", "$lte": f"{month}-{last_day:02d}"}}}
                    ],
                    "as": "stats"
                }
            }
        ]

        exp2 = db.command('explain', {
            'aggregate': 'employees',
            'pipeline': pipeline_with_dept,
            'cursor': {}
        }, verbosity='executionStats')

        cursor_plan2 = exp2.get('queryPlanner', {}).get('winningPlan')
        if not cursor_plan2:
            for s in exp2.get('stages', []):
                if '$cursor' in s:
                    cursor_plan2 = s['$cursor'].get('queryPlanner', {}).get('winningPlan')
                    break

        stages2 = extract_stages(cursor_plan2)
        self.assertNotIn('COLLSCAN', stages2, f"Unexpected COLLSCAN in stages: {stages2}")
        self.assertIn('IXSCAN', stages2, f"Expected IXSCAN in stages: {stages2}")

        lookup_stage2 = None
        for s in exp2.get('stages', []):
            if '$lookup' in s:
                lookup_stage2 = s
                break
        self.assertIsNotNone(lookup_stage2, "Lookup stage not found in explain output")
        self.assertIn('emp_code_1_date_1', lookup_stage2.get('indexesUsed', []))
        self.assertEqual(lookup_stage2.get('collectionScans', 0), 0)

    # ----------------------------------------------------------------------- #
    # Regression Tests for All Endpoints
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

        # Clean up
        db.employees.delete_one({"emp_code": unique_code})
        db.attendance_logs.delete_many({"emp_code": unique_code})


if __name__ == "__main__":
    unittest.main()
