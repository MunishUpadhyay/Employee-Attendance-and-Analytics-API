"""
Comprehensive test suite for GET /analytics/departments/{department}/trend.
Tests contract validation, date range bounds, 404 unknown department, 422 errors,
densified daily series, headcount with joining dates, attendance metrics,
moving 7-day average, query plans, and regressions.
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
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


class TestDepartmentTrend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.employees.create_index("emp_code", unique=True)
        db.employees.create_index([("department", 1), ("emp_code", 1)])
        db.employees.create_index("joined_on")
        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
        db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])

    # ----------------------------------------------------------------------- #
    # 1. Contract & Parameter Validation
    # ----------------------------------------------------------------------- #
    def test_01_valid_request_baseline(self):
        # Engineering exists in seed data
        res = client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-01-07")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["department"], "Engineering")
        self.assertIn("items", data)
        self.assertEqual(len(data["items"]), 7)
        self.assertEqual(data["items"][0]["date"], "2026-01-01")
        self.assertEqual(data["items"][-1]["date"], "2026-01-07")

    def test_02_unknown_department_404(self):
        res = client.get("/analytics/departments/NonExistentDept999/trend?from=2026-01-01&to=2026-01-07")
        self.assertEqual(res.status_code, 404)
        self.assertIn("detail", res.json())

    def test_03_missing_parameters(self):
        # Missing from
        res1 = client.get("/analytics/departments/Engineering/trend?to=2026-01-07")
        self.assertEqual(res1.status_code, 422)

        # Missing to
        res2 = client.get("/analytics/departments/Engineering/trend?from=2026-01-01")
        self.assertEqual(res2.status_code, 422)

        # Missing both
        res3 = client.get("/analytics/departments/Engineering/trend")
        self.assertEqual(res3.status_code, 422)

    def test_04_malformed_and_invalid_dates(self):
        invalid_dates = ["invalid", "2026-13-01", "2026-02-30", "2026/01/01", "2026-1-1"]
        for inv in invalid_dates:
            with self.subTest(invalid_from=inv):
                res = client.get(f"/analytics/departments/Engineering/trend?from={inv}&to=2026-01-07")
                self.assertEqual(res.status_code, 422)
            with self.subTest(invalid_to=inv):
                res = client.get(f"/analytics/departments/Engineering/trend?from=2026-01-01&to={inv}")
                self.assertEqual(res.status_code, 422)

    def test_05_reversed_date_range(self):
        res = client.get("/analytics/departments/Engineering/trend?from=2026-01-10&to=2026-01-05")
        self.assertEqual(res.status_code, 422)
        self.assertIn("detail", res.json())

    def test_06_range_limit_92_days(self):
        # 92 days is valid: 2026-01-01 to 2026-04-02 (Jan 31 + Feb 28 + Mar 31 + Apr 2 = 92 days)
        res_92 = client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-04-02")
        self.assertEqual(res_92.status_code, 200)
        self.assertEqual(len(res_92.json()["items"]), 92)

        # 93 days exceeds limit: 2026-01-01 to 2026-04-03 -> 422
        res_93 = client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-04-03")
        self.assertEqual(res_93.status_code, 422)
        self.assertIn("detail", res_93.json())

    def test_07_exact_response_schema_and_no_id_leak(self):
        res = client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-01-03")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(set(data.keys()), {"department", "items"})
        required_item_keys = {
            "date", "is_working_day", "headcount", "present_count",
            "late_count", "attendance_rate", "moving_avg_7d"
        }
        for item in data["items"]:
            self.assertEqual(set(item.keys()), required_item_keys)
            self.assertNotIn("_id", item)
            self.assertIsInstance(item["date"], str)
            self.assertIsInstance(item["is_working_day"], bool)
            self.assertIsInstance(item["headcount"], int)
            self.assertIsInstance(item["present_count"], (int, float))
            self.assertIsInstance(item["late_count"], int)
            if item["attendance_rate"] is not None:
                self.assertIsInstance(item["attendance_rate"], (int, float))
            if item["moving_avg_7d"] is not None:
                self.assertIsInstance(item["moving_avg_7d"], (int, float))

    # ----------------------------------------------------------------------- #
    # 2. Daily Series, Headcount, Attendance Metrics & 7-Day Moving Avg
    # ----------------------------------------------------------------------- #
    def test_department_trend_complete_behavior(self):
        test_dept = f"TrendQA_{uuid.uuid4().hex[:6]}"
        emp1_code = f"EMP{uuid.uuid4().int % 900000 + 100000:06d}"
        emp2_code = f"EMP{uuid.uuid4().int % 900000 + 100000:06d}"

        # emp1 joins 2026-05-04 (Monday)
        # emp2 joins 2026-05-09 (Saturday)
        db.employees.insert_one({
            "emp_code": emp1_code,
            "name": "Trend Emp 1",
            "email": f"{emp1_code.lower()}@test.com",
            "department": test_dept,
            "shift_start": "09:00",
            "shift_end": "18:00",
            "joined_on": "2026-05-04",
            "created_at": datetime(2026, 5, 4, 0, 0, tzinfo=timezone.utc)
        })
        db.employees.insert_one({
            "emp_code": emp2_code,
            "name": "Trend Emp 2",
            "email": f"{emp2_code.lower()}@test.com",
            "department": test_dept,
            "shift_start": "09:00",
            "shift_end": "18:00",
            "joined_on": "2026-05-09",
            "created_at": datetime(2026, 5, 9, 0, 0, tzinfo=timezone.utc)
        })

        # Insert attendance logs:
        # 2026-05-04 (Mon): emp1 PRESENT, full day (1.0), late_minutes = 20
        # 2026-05-05 (Tue): emp1 WFH, half_day (0.5), late_minutes = 0
        # 2026-05-06 (Wed): emp1 ON_DUTY, missing half_day (default 1.0), late_minutes = 15
        # 2026-05-07 (Thu): emp1 LEAVE (0.0), late_minutes = 0
        # 2026-05-08 (Fri): emp1 ABSENT (0.0), late_minutes = 0
        # 2026-05-09 (Sat, weekend): emp1 PRESENT on weekend (1.0), late_minutes = 30
        # 2026-05-10 (Sun, weekend): no logs
        # 2026-05-11 (Mon): emp1 PRESENT (1.0), emp2 PRESENT (1.0) -> total present_count = 2.0, late = 0
        db.attendance_logs.insert_many([
            {"emp_code": emp1_code, "date": "2026-05-04", "status": "PRESENT", "half_day": False, "late_minutes": 20},
            {"emp_code": emp1_code, "date": "2026-05-05", "status": "WFH", "half_day": True, "late_minutes": 0},
            {"emp_code": emp1_code, "date": "2026-05-06", "status": "ON_DUTY", "late_minutes": 15},
            {"emp_code": emp1_code, "date": "2026-05-07", "status": "LEAVE", "late_minutes": 0},
            {"emp_code": emp1_code, "date": "2026-05-08", "status": "ABSENT", "late_minutes": 0},
            {"emp_code": emp1_code, "date": "2026-05-09", "status": "PRESENT", "half_day": False, "late_minutes": 30},
            {"emp_code": emp1_code, "date": "2026-05-11", "status": "PRESENT", "half_day": False, "late_minutes": 0},
            {"emp_code": emp2_code, "date": "2026-05-11", "status": "PRESENT", "half_day": False, "late_minutes": 0},
        ])

        try:
            # Query from 2026-05-01 (Friday) to 2026-05-11 (Monday) -> exactly 11 calendar days
            res = client.get(f"/analytics/departments/{test_dept}/trend?from=2026-05-01&to=2026-05-11")
            self.assertEqual(res.status_code, 200)
            items = res.json()["items"]
            self.assertEqual(len(items), 11)

            # Map by date
            by_date = {i["date"]: i for i in items}

            # 1. 2026-05-01 (Fri): working day, but NO employee joined yet -> headcount = 0
            # Contract: only on working days with headcount > 0, otherwise null
            d1 = by_date["2026-05-01"]
            self.assertTrue(d1["is_working_day"])
            self.assertEqual(d1["headcount"], 0)
            self.assertEqual(d1["present_count"], 0.0)
            self.assertEqual(d1["late_count"], 0)
            self.assertIsNone(d1["attendance_rate"])
            self.assertIsNone(d1["moving_avg_7d"])

            # 2. 2026-05-02 (Sat) & 2026-05-03 (Sun): weekends -> rate is null, moving_avg is null
            d2 = by_date["2026-05-02"]
            self.assertFalse(d2["is_working_day"])
            self.assertEqual(d2["headcount"], 0)
            self.assertIsNone(d2["attendance_rate"])
            self.assertIsNone(d2["moving_avg_7d"])

            # 3. 2026-05-04 (Mon): emp1 joins -> headcount = 1
            # PRESENT full day (1.0), late 20m -> rate = 1.0 / 1 = 1.0
            # moving_avg_7d over window [May 1..May 4]: only May 4 is non-null -> avg is 1.0
            d4 = by_date["2026-05-04"]
            self.assertTrue(d4["is_working_day"])
            self.assertEqual(d4["headcount"], 1)
            self.assertEqual(d4["present_count"], 1.0)
            self.assertEqual(d4["late_count"], 1)
            self.assertEqual(d4["attendance_rate"], 1.0)
            self.assertEqual(d4["moving_avg_7d"], 1.0)

            # 4. 2026-05-05 (Tue): headcount = 1, WFH half day (0.5), late 0m
            # rate = 0.5 / 1 = 0.5
            # moving_avg_7d over [May 4, May 5]: (1.0 + 0.5) / 2 = 0.75
            d5 = by_date["2026-05-05"]
            self.assertTrue(d5["is_working_day"])
            self.assertEqual(d5["headcount"], 1)
            self.assertEqual(d5["present_count"], 0.5)
            self.assertEqual(d5["late_count"], 0)
            self.assertEqual(d5["attendance_rate"], 0.5)
            self.assertEqual(d5["moving_avg_7d"], 0.75)

            # 5. 2026-05-06 (Wed): headcount = 1, ON_DUTY (1.0), late 15m
            # rate = 1.0 / 1 = 1.0
            # moving_avg_7d over [May 4, May 5, May 6]: (1.0 + 0.5 + 1.0) / 3 = 0.8333
            d6 = by_date["2026-05-06"]
            self.assertTrue(d6["is_working_day"])
            self.assertEqual(d6["headcount"], 1)
            self.assertEqual(d6["present_count"], 1.0)
            self.assertEqual(d6["late_count"], 1)
            self.assertEqual(d6["attendance_rate"], 1.0)
            self.assertEqual(d6["moving_avg_7d"], 0.8333)

            # 6. 2026-05-07 (Thu): headcount = 1, LEAVE -> present_count = 0.0
            # rate = 0.0 / 1 = 0.0 (working day with no present attendance has rate 0.0)
            # moving_avg_7d over [May 4..May 7]: (1.0 + 0.5 + 1.0 + 0.0) / 4 = 0.625
            d7 = by_date["2026-05-07"]
            self.assertTrue(d7["is_working_day"])
            self.assertEqual(d7["headcount"], 1)
            self.assertEqual(d7["present_count"], 0.0)
            self.assertEqual(d7["attendance_rate"], 0.0)
            self.assertEqual(d7["moving_avg_7d"], 0.625)

            # 7. 2026-05-08 (Fri): headcount = 1, ABSENT -> present_count = 0.0
            # rate = 0.0
            # moving_avg_7d over [May 4..May 8]: (1.0 + 0.5 + 1.0 + 0.0 + 0.0) / 5 = 0.5
            d8 = by_date["2026-05-08"]
            self.assertTrue(d8["is_working_day"])
            self.assertEqual(d8["attendance_rate"], 0.0)
            self.assertEqual(d8["moving_avg_7d"], 0.5)

            # 8. 2026-05-09 (Sat, weekend): emp2 joins -> headcount = 2
            # Weekend present record (1.0), late = 1.
            # Weekend -> attendance_rate = null.
            # moving_avg_7d: weekend has no non-null rate, retains mean of non-null rates in window!
            d9 = by_date["2026-05-09"]
            self.assertFalse(d9["is_working_day"])
            self.assertEqual(d9["headcount"], 2)
            self.assertEqual(d9["present_count"], 1.0)
            self.assertEqual(d9["late_count"], 1)
            self.assertIsNone(d9["attendance_rate"])
            self.assertEqual(d9["moving_avg_7d"], 0.5)

            # 9. 2026-05-10 (Sun, weekend): no records
            d10 = by_date["2026-05-10"]
            self.assertFalse(d10["is_working_day"])
            self.assertEqual(d10["headcount"], 2)
            self.assertEqual(d10["present_count"], 0.0)
            self.assertIsNone(d10["attendance_rate"])
            self.assertEqual(d10["moving_avg_7d"], 0.5)

            # 10. 2026-05-11 (Mon): working day, headcount = 2 (emp1 and emp2)
            # Both present -> present_count = 2.0. rate = 2.0 / 2 = 1.0
            # 7-day window covers May 5 to May 11 (7 rows: May 5, 6, 7, 8, 9, 10, 11).
            # Non-null rates: May 5 (0.5), May 6 (1.0), May 7 (0.0), May 8 (0.0), May 11 (1.0).
            # Count = 5. Sum = 0.5 + 1.0 + 0.0 + 0.0 + 1.0 = 2.5
            # Mean = 2.5 / 5 = 0.5
            d11 = by_date["2026-05-11"]
            self.assertTrue(d11["is_working_day"])
            self.assertEqual(d11["headcount"], 2)
            self.assertEqual(d11["present_count"], 2.0)
            self.assertEqual(d11["attendance_rate"], 1.0)
            self.assertEqual(d11["moving_avg_7d"], 0.5)

        finally:
            db.employees.delete_many({"department": test_dept})
            db.attendance_logs.delete_many({"emp_code": {"$in": [emp1_code, emp2_code]}})

    # ----------------------------------------------------------------------- #
    # 3. Query Plan & Index Verification
    # ----------------------------------------------------------------------- #
    def test_explain_plan_department_trend(self):
        from app.main import build_department_trend_pipeline

        # Engineering has employees in seed data
        pipeline = build_department_trend_pipeline("Engineering", "2026-01-01", "2026-01-07")
        exp = db.command('explain', {
            'aggregate': 'employees',
            'pipeline': pipeline,
            'cursor': {}
        }, verbosity='executionStats')

        # Cursor winning plan on employees
        cursor_plan = exp.get('queryPlanner', {}).get('winningPlan')
        if not cursor_plan:
            for s in exp.get('stages', []):
                if '$cursor' in s:
                    cursor_plan = s['$cursor'].get('queryPlanner', {}).get('winningPlan')
                    break

        stages = extract_stages(cursor_plan)
        self.assertNotIn('COLLSCAN', stages, f"Unexpected COLLSCAN in employees stages: {stages}")
        self.assertIn('IXSCAN', stages, f"Expected IXSCAN in employees stages: {stages}")

        # Foreign lookup on attendance_logs
        lookup_stage = None
        for s in exp.get('stages', []):
            if '$lookup' in s:
                lookup_stage = s
                break
        self.assertIsNotNone(lookup_stage, "Lookup stage not found in explain output")
        self.assertEqual(lookup_stage.get('collectionScans', 0), 0)
        self.assertIn('date_-1_emp_code_1', lookup_stage.get('indexesUsed', []))

    # ----------------------------------------------------------------------- #
    # 4. Edge Cases: Rounding Ties, Zero Logs, Future Joiners, Boundaries
    # ----------------------------------------------------------------------- #
    def test_department_trend_exact_half_up_rounding_boundaries(self):
        """Verify true ROUND_HALF_UP on exact 4-decimal ties (e.g. 0.03125 -> 0.0313)."""
        test_dept = f"TrendTie_{uuid.uuid4().hex[:6]}"
        # Create 16 employees joining 2026-06-01
        emp_codes = [f"TIE_{uuid.uuid4().hex[:6]}_{i}" for i in range(16)]
        for ec in emp_codes:
            db.employees.insert_one({
                "emp_code": ec,
                "name": f"Tie Emp {ec}",
                "department": test_dept,
                "joined_on": "2026-06-01"
            })

        # 2026-06-01 is Monday (working day).
        # Give exactly 1 half_day present log (0.5).
        # 0.5 / 16 = 0.03125 -> exact half-up must round to 0.0313 (Banker's round would give 0.0312).
        db.attendance_logs.insert_one({
            "emp_code": emp_codes[0],
            "date": "2026-06-01",
            "status": "PRESENT",
            "half_day": True,
            "late_minutes": 0
        })

        try:
            res = client.get(f"/analytics/departments/{test_dept}/trend?from=2026-06-01&to=2026-06-02")
            self.assertEqual(res.status_code, 200)
            items = res.json()["items"]
            self.assertEqual(len(items), 2)

            d1 = items[0]
            self.assertEqual(d1["date"], "2026-06-01")
            self.assertEqual(d1["headcount"], 16)
            self.assertEqual(d1["present_count"], 0.5)
            self.assertEqual(d1["attendance_rate"], 0.0313)
            self.assertEqual(d1["moving_avg_7d"], 0.0313)

            d2 = items[1]
            self.assertEqual(d2["date"], "2026-06-02")
            self.assertEqual(d2["headcount"], 16)
            self.assertEqual(d2["present_count"], 0.0)
            self.assertEqual(d2["attendance_rate"], 0.0)
            # Window has [0.0313, 0.0] -> mean is 0.0313 / 2 = 0.01565 -> half-up rounds to 0.0157
            expected_mavg = float(Decimal("0.01565").quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
            self.assertEqual(d2["moving_avg_7d"], expected_mavg)
        finally:
            db.employees.delete_many({"department": test_dept})
            db.attendance_logs.delete_many({"emp_code": {"$in": emp_codes}})

    def test_department_trend_no_attendance_logs(self):
        """A department with employees but no attendance logs emits all days with 0 counts."""
        test_dept = f"TrendNoLogs_{uuid.uuid4().hex[:6]}"
        emp_code = f"EMP_NOLOG_{uuid.uuid4().hex[:6]}"
        db.employees.insert_one({
            "emp_code": emp_code,
            "name": "No Logs Emp",
            "department": test_dept,
            "joined_on": "2026-01-01"
        })

        try:
            res = client.get(f"/analytics/departments/{test_dept}/trend?from=2026-01-01&to=2026-01-07")
            self.assertEqual(res.status_code, 200)
            items = res.json()["items"]
            self.assertEqual(len(items), 7)
            for item in items:
                self.assertEqual(item["headcount"], 1)
                self.assertEqual(item["present_count"], 0.0)
                self.assertEqual(item["late_count"], 0)
                if item["is_working_day"]:
                    self.assertEqual(item["attendance_rate"], 0.0)
                    self.assertEqual(item["moving_avg_7d"], 0.0)
                else:
                    self.assertIsNone(item["attendance_rate"])
                    self.assertEqual(item["moving_avg_7d"], 0.0)
        finally:
            db.employees.delete_many({"department": test_dept})

    def test_department_trend_all_future_joiners(self):
        """When headcount is 0 for all days, attendance_rate and moving_avg_7d are null."""
        test_dept = f"TrendFuture_{uuid.uuid4().hex[:6]}"
        emp_code = f"EMP_FUT_{uuid.uuid4().hex[:6]}"
        db.employees.insert_one({
            "emp_code": emp_code,
            "name": "Future Emp",
            "department": test_dept,
            "joined_on": "2026-12-31"
        })

        try:
            res = client.get(f"/analytics/departments/{test_dept}/trend?from=2026-01-01&to=2026-01-05")
            self.assertEqual(res.status_code, 200)
            items = res.json()["items"]
            self.assertEqual(len(items), 5)
            for item in items:
                self.assertEqual(item["headcount"], 0)
                self.assertIsNone(item["attendance_rate"])
                self.assertIsNone(item["moving_avg_7d"])
        finally:
            db.employees.delete_many({"department": test_dept})

    def test_department_trend_single_day_range(self):
        """Querying from == to returns exactly 1 day."""
        res = client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-01-01")
        self.assertEqual(res.status_code, 200)
        items = res.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["date"], "2026-01-01")

    def test_department_trend_intermediate_record_only(self):
        """Records only on day 3 of 5-day range; densification accurately leaves other days at 0."""
        test_dept = f"TrendInterm_{uuid.uuid4().hex[:6]}"
        emp_code = f"EMP_INT_{uuid.uuid4().hex[:6]}"
        db.employees.insert_one({
            "emp_code": emp_code,
            "name": "Interm Emp",
            "department": test_dept,
            "joined_on": "2026-05-01"
        })
        # 2026-05-06 is Wednesday (day 3 of [2026-05-04..2026-05-08])
        db.attendance_logs.insert_one({
            "emp_code": emp_code,
            "date": "2026-05-06",
            "status": "PRESENT",
            "half_day": False,
            "late_minutes": 10
        })

        try:
            res = client.get(f"/analytics/departments/{test_dept}/trend?from=2026-05-04&to=2026-05-08")
            self.assertEqual(res.status_code, 200)
            items = res.json()["items"]
            self.assertEqual(len(items), 5)
            by_date = {i["date"]: i for i in items}
            self.assertEqual(by_date["2026-05-04"]["present_count"], 0.0)
            self.assertEqual(by_date["2026-05-05"]["present_count"], 0.0)
            self.assertEqual(by_date["2026-05-06"]["present_count"], 1.0)
            self.assertEqual(by_date["2026-05-06"]["late_count"], 1)
            self.assertEqual(by_date["2026-05-07"]["present_count"], 0.0)
            self.assertEqual(by_date["2026-05-08"]["present_count"], 0.0)
        finally:
            db.employees.delete_many({"department": test_dept})
            db.attendance_logs.delete_many({"emp_code": emp_code})

    def test_department_trend_exact_end_date_inclusion_and_exclusion(self):
        """End date 'to' is included with its attendance records; 'to + 1' is excluded."""
        test_dept = f"TrendEnd_{uuid.uuid4().hex[:6]}"
        emp_code = f"EMP_END_{uuid.uuid4().hex[:6]}"
        db.employees.insert_one({
            "emp_code": emp_code,
            "name": "End Emp",
            "department": test_dept,
            "joined_on": "2026-07-01"
        })
        # 2026-07-03 is the 'to' date; 2026-07-04 is 'to + 1' outside the range
        db.attendance_logs.insert_many([
            {"emp_code": emp_code, "date": "2026-07-03", "status": "PRESENT", "half_day": False, "late_minutes": 0},
            {"emp_code": emp_code, "date": "2026-07-04", "status": "PRESENT", "half_day": False, "late_minutes": 0}
        ])

        try:
            res = client.get(f"/analytics/departments/{test_dept}/trend?from=2026-07-01&to=2026-07-03")
            self.assertEqual(res.status_code, 200)
            items = res.json()["items"]
            self.assertEqual(len(items), 3)
            self.assertEqual(items[0]["date"], "2026-07-01")
            self.assertEqual(items[-1]["date"], "2026-07-03")
            self.assertEqual(items[-1]["present_count"], 1.0)
            # Ensure 2026-07-04 is completely absent
            self.assertNotIn("2026-07-04", [i["date"] for i in items])
        finally:
            db.employees.delete_many({"department": test_dept})
            db.attendance_logs.delete_many({"emp_code": emp_code})

    def test_department_trend_zero_headcount_at_range_start(self):
        """Zero headcount at the start of requested range emits null rate and moving_avg."""
        test_dept = f"TrendStartZero_{uuid.uuid4().hex[:6]}"
        emp_code = f"EMP_SZ_{uuid.uuid4().hex[:6]}"
        # Employee joins on 2026-08-04 (Tuesday)
        db.employees.insert_one({
            "emp_code": emp_code,
            "name": "Start Zero Emp",
            "department": test_dept,
            "joined_on": "2026-08-04"
        })
        # Log on 2026-08-04
        db.attendance_logs.insert_one({
            "emp_code": emp_code,
            "date": "2026-08-04",
            "status": "PRESENT",
            "half_day": False,
            "late_minutes": 0
        })

        try:
            # Query from 2026-08-01 (Saturday) to 2026-08-05 (Wednesday)
            res = client.get(f"/analytics/departments/{test_dept}/trend?from=2026-08-01&to=2026-08-05")
            self.assertEqual(res.status_code, 200)
            items = res.json()["items"]
            self.assertEqual(len(items), 5)
            by_date = {i["date"]: i for i in items}

            # 2026-08-01 (Sat) & 2026-08-02 (Sun) & 2026-08-03 (Mon): headcount == 0
            for d in ["2026-08-01", "2026-08-02", "2026-08-03"]:
                self.assertEqual(by_date[d]["headcount"], 0)
                self.assertIsNone(by_date[d]["attendance_rate"])
                self.assertIsNone(by_date[d]["moving_avg_7d"])

            # 2026-08-04 (Tue): employee joins -> headcount = 1, present = 1.0 -> rate = 1.0
            self.assertEqual(by_date["2026-08-04"]["headcount"], 1)
            self.assertEqual(by_date["2026-08-04"]["attendance_rate"], 1.0)
            self.assertEqual(by_date["2026-08-04"]["moving_avg_7d"], 1.0)

            # 2026-08-05 (Wed): headcount = 1, present = 0.0 -> rate = 0.0 -> moving_avg = (1.0 + 0.0)/2 = 0.5
            self.assertEqual(by_date["2026-08-05"]["headcount"], 1)
            self.assertEqual(by_date["2026-08-05"]["attendance_rate"], 0.0)
            self.assertEqual(by_date["2026-08-05"]["moving_avg_7d"], 0.5)
        finally:
            db.employees.delete_many({"department": test_dept})
            db.attendance_logs.delete_many({"emp_code": emp_code})

    def test_department_trend_moving_average_7d_window_sliding(self):
        """Moving average window [-6, 0] covers up to 7 rows and correctly drops the 8th row."""
        test_dept = f"TrendWindow_{uuid.uuid4().hex[:6]}"
        emp_code = f"EMP_WIN_{uuid.uuid4().hex[:6]}"
        db.employees.insert_one({
            "emp_code": emp_code,
            "name": "Window Emp",
            "department": test_dept,
            "joined_on": "2026-09-01"
        })
        # Record only on day 1 (2026-09-01, Tuesday)
        db.attendance_logs.insert_one({
            "emp_code": emp_code,
            "date": "2026-09-01",
            "status": "PRESENT",
            "half_day": False,
            "late_minutes": 0
        })

        try:
            # 8 calendar days: 2026-09-01 (Tue) through 2026-09-08 (Tue)
            res = client.get(f"/analytics/departments/{test_dept}/trend?from=2026-09-01&to=2026-09-08")
            self.assertEqual(res.status_code, 200)
            items = res.json()["items"]
            self.assertEqual(len(items), 8)
            by_date = {i["date"]: i for i in items}

            # Day 1 (Sep 01, Tue): rate 1.0 -> moving_avg 1.0
            self.assertEqual(by_date["2026-09-01"]["attendance_rate"], 1.0)
            self.assertEqual(by_date["2026-09-01"]["moving_avg_7d"], 1.0)

            # Day 2 (Sep 02, Wed): rate 0.0 -> window [1.0, 0.0] -> avg 0.5
            self.assertEqual(by_date["2026-09-02"]["attendance_rate"], 0.0)
            self.assertEqual(by_date["2026-09-02"]["moving_avg_7d"], 0.5)

            # Day 7 (Sep 07, Mon): 7th row in range. Window covers Sep 01..Sep 07.
            # Working days: Sep 01 (1.0), Sep 02 (0.0), Sep 03 (0.0), Sep 04 (0.0), Sep 07 (0.0).
            # 5 working days -> sum = 1.0, mean = 1.0 / 5 = 0.2
            self.assertEqual(by_date["2026-09-07"]["moving_avg_7d"], 0.2)

            # Day 8 (Sep 08, Tue): 8th row in range. Window [-6, 0] covers Sep 02..Sep 08!
            # Day 1 (Sep 01) has dropped out of the 7-row window!
            # Working days in window: Sep 02, 03, 04, 07, 08 (all rate 0.0).
            # Moving average MUST be 0.0!
            self.assertEqual(by_date["2026-09-08"]["moving_avg_7d"], 0.0)
        finally:
            db.employees.delete_many({"department": test_dept})
            db.attendance_logs.delete_many({"emp_code": emp_code})


if __name__ == "__main__":
    unittest.main()
