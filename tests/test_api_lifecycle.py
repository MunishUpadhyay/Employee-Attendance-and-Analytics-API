"""
Lifecycle, CRUD, and business rule tests for:
- Helper unit tests (R1, R2 lateness including overnight shifts)
- Employee management (POST /employees, GET /employees)
- Attendance punch-in (POST /attendance/punch-in)
- Attendance punch-out (POST /attendance/punch-out)
- Attendance listing (GET /attendance)
- Attendance regularization (PATCH /attendance/{emp_code}/{date})
- Employee monthly analytics (GET /analytics/employees/{emp_code}/monthly)
- Comprehensive 12-endpoint system regression flow

All tests dynamically isolate their records and clean up completely without
modifying seeded data (EMP0001-EMP0006).
"""
import calendar
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal
import unittest
import uuid

from starlette.testclient import TestClient

from app.main import (
    app,
    db,
    IST,
    compute_late_minutes,
    compute_work_hours,
    compute_overtime,
)

client = TestClient(app)


class TestLateMinutesUnit(unittest.TestCase):
    """Unit tests for compute_late_minutes covering R1 and R2."""

    def test_daytime_shift_on_time_and_early(self):
        # Shift 09:30
        on_time = datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST)
        early = datetime(2026, 7, 6, 9, 20, 0, tzinfo=IST)
        self.assertEqual(compute_late_minutes(on_time, "09:30", "2026-07-06"), 0)
        self.assertEqual(compute_late_minutes(early, "09:30", "2026-07-06"), 0)

    def test_daytime_shift_grace_period_boundaries(self):
        # Grace period is 10 min: 09:40:00 -> 0; 09:40:01 -> 10; 10:15:59 -> 45
        exact_10m = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST)
        just_over = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST)
        deep_late = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST)

        self.assertEqual(compute_late_minutes(exact_10m, "09:30", "2026-07-06"), 0)
        self.assertEqual(compute_late_minutes(just_over, "09:30", "2026-07-06"), 10)
        self.assertEqual(compute_late_minutes(deep_late, "09:30", "2026-07-06"), 45)

    def test_overnight_shift_grace_and_boundaries(self):
        # Shift 22:00 - 06:00, attendance date 2026-07-06
        at_start = datetime(2026, 7, 6, 22, 0, 0, tzinfo=IST)
        exact_10m = datetime(2026, 7, 6, 22, 10, 0, tzinfo=IST)
        just_over = datetime(2026, 7, 6, 22, 10, 1, tzinfo=IST)

        self.assertEqual(compute_late_minutes(at_start, "22:00", "2026-07-06"), 0)
        self.assertEqual(compute_late_minutes(exact_10m, "22:00", "2026-07-06"), 0)
        self.assertEqual(compute_late_minutes(just_over, "22:00", "2026-07-06"), 10)

    def test_overnight_shift_next_calendar_day_punch_in(self):
        # Shift 22:00 - 06:00 started 2026-07-06.
        # Punch-in at 01:00 on 2026-07-07 is 3 hours (180 minutes) late.
        punch_in_next_day = datetime(2026, 7, 7, 1, 0, 0, tzinfo=IST)
        late_mins = compute_late_minutes(punch_in_next_day, "22:00", "2026-07-06")
        self.assertEqual(late_mins, 180)

    def test_backward_compatibility_when_attendance_date_omitted(self):
        # When attendance_date is None, preserves existing daytime behavior
        exact_10m = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST)
        just_over = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST)
        self.assertEqual(compute_late_minutes(exact_10m, "09:30"), 0)
        self.assertEqual(compute_late_minutes(just_over, "09:30"), 10)


class TestApiLifecycleAndCrud(unittest.TestCase):
    """End-to-end CRUD, lifecycle, error handling, and business rules."""

    def setUp(self):
        self.created_emp_codes = []

    def tearDown(self):
        for code in self.created_emp_codes:
            db.employees.delete_one({"emp_code": code})
            db.attendance_logs.delete_many({"emp_code": code})

    def make_unique_emp_code(self) -> str:
        code = f"EMP{uuid.uuid4().int % 900000 + 100000:06d}"
        self.created_emp_codes.append(code)
        return code

    # ----------------------------------------------------------------------- #
    # 1. Health Probe
    # ----------------------------------------------------------------------- #
    def test_health_probe(self):
        r = client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok"})

    # ----------------------------------------------------------------------- #
    # 2. Employees API
    # ----------------------------------------------------------------------- #
    def test_create_employee_success_and_schema(self):
        emp_code = self.make_unique_emp_code()
        payload = {
            "emp_code": emp_code,
            "name": "Alice Engineer",
            "email": f"{emp_code.lower()}@company.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-10",
        }
        r = client.post("/employees", json=payload)
        self.assertEqual(r.status_code, 201)
        data = r.json()
        self.assertEqual(data["emp_code"], emp_code)
        self.assertEqual(data["name"], "Alice Engineer")
        self.assertEqual(data["department"], "Engineering")
        self.assertIn("created_at", data)
        self.assertIsInstance(data["created_at"], int)
        self.assertNotIn("_id", data)

    def test_create_employee_duplicate_409(self):
        emp_code = self.make_unique_emp_code()
        payload = {
            "emp_code": emp_code,
            "name": "First Employee",
            "email": f"{emp_code.lower()}@company.com",
            "department": "Sales",
            "shift_start": "09:00",
            "shift_end": "18:00",
            "joined_on": "2026-02-01",
        }
        r1 = client.post("/employees", json=payload)
        self.assertEqual(r1.status_code, 201)

        r2 = client.post("/employees", json=payload)
        self.assertEqual(r2.status_code, 409)
        self.assertIn("already exists", r2.json()["detail"].lower())

    def test_create_employee_validation_errors_422(self):
        emp_code = self.make_unique_emp_code()
        # 1. shift_start == shift_end must fail
        r1 = client.post("/employees", json={
            "emp_code": emp_code,
            "name": "Bad Shift",
            "email": "shift@company.com",
            "department": "Ops",
            "shift_start": "09:00",
            "shift_end": "09:00",
            "joined_on": "2026-01-01",
        })
        self.assertEqual(r1.status_code, 422)

        # 2. Invalid email format
        r2 = client.post("/employees", json={
            "emp_code": emp_code,
            "name": "Bad Email",
            "email": "not-an-email",
            "department": "Ops",
            "shift_start": "09:00",
            "shift_end": "18:00",
            "joined_on": "2026-01-01",
        })
        self.assertEqual(r2.status_code, 422)

        # 3. Invalid emp_code pattern
        r3 = client.post("/employees", json={
            "emp_code": "INVALID_123",
            "name": "Bad Code",
            "email": "bad@company.com",
            "department": "Ops",
            "shift_start": "09:00",
            "shift_end": "18:00",
            "joined_on": "2026-01-01",
        })
        self.assertEqual(r3.status_code, 422)

    def test_list_employees_pagination_and_filter(self):
        dept_name = f"TestDept_{uuid.uuid4().hex[:6]}"
        codes = [self.make_unique_emp_code(), self.make_unique_emp_code()]
        codes.sort()

        for c in codes:
            client.post("/employees", json={
                "emp_code": c,
                "name": f"Name {c}",
                "email": f"{c.lower()}@test.com",
                "department": dept_name,
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-01",
            })

        # List filtered by department
        r = client.get(f"/employees?department={dept_name}&page=1&page_size=1")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["total"], 2)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["emp_code"], codes[0])

        r2 = client.get(f"/employees?department={dept_name}&page=2&page_size=1")
        self.assertEqual(r2.status_code, 200)
        data2 = r2.json()
        self.assertEqual(len(data2["items"]), 1)
        self.assertEqual(data2["items"][0]["emp_code"], codes[1])

    # ----------------------------------------------------------------------- #
    # 3. Attendance Punch-In API
    # ----------------------------------------------------------------------- #
    def test_punch_in_lifecycle_and_duplicates(self):
        emp_code = self.make_unique_emp_code()
        client.post("/employees", json={
            "emp_code": emp_code,
            "name": "Punch Tester",
            "email": f"{emp_code.lower()}@test.com",
            "department": "QA",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        })

        # Normal punch-in at 09:45:00 IST (15 minutes late -> late_minutes = 15)
        dt = datetime(2026, 8, 10, 9, 45, 0, tzinfo=IST)
        ms = int(dt.timestamp() * 1000)

        r = client.post("/attendance/punch-in", json={
            "emp_code": emp_code,
            "punched_at": ms,
            "status": "PRESENT"
        })
        self.assertEqual(r.status_code, 201)
        record = r.json()
        self.assertEqual(record["emp_code"], emp_code)
        self.assertEqual(record["date"], "2026-08-10")
        self.assertEqual(record["late_minutes"], 15)
        self.assertIsNone(record["punch_out"])
        self.assertIsNone(record["work_hours"])
        self.assertEqual(record["overtime_minutes"], 0)
        self.assertFalse(record["half_day"])
        self.assertEqual(record["history"], [])
        self.assertNotIn("_id", record)

        # Duplicate punch-in for the same date -> 409
        r_dup = client.post("/attendance/punch-in", json={
            "emp_code": emp_code,
            "punched_at": ms + 60000,
            "status": "PRESENT"
        })
        self.assertEqual(r_dup.status_code, 409)

    def test_punch_in_overnight_shift_end_to_end(self):
        emp_code = self.make_unique_emp_code()
        # Shift 22:00 to 06:00
        client.post("/employees", json={
            "emp_code": emp_code,
            "name": "Night Guard",
            "email": f"{emp_code.lower()}@test.com",
            "department": "Security",
            "shift_start": "22:00",
            "shift_end": "06:00",
            "joined_on": "2026-01-01",
        })

        # Punch in at 01:00 on 2026-08-11 IST
        # Earlier than shift_end (06:00) -> belongs to shift that started 2026-08-10.
        # Late elapsed time = 3 hours (180 minutes).
        dt = datetime(2026, 8, 11, 1, 0, 0, tzinfo=IST)
        ms = int(dt.timestamp() * 1000)

        r = client.post("/attendance/punch-in", json={
            "emp_code": emp_code,
            "punched_at": ms,
            "status": "PRESENT"
        })
        self.assertEqual(r.status_code, 201)
        record = r.json()
        self.assertEqual(record["date"], "2026-08-10")
        self.assertEqual(record["late_minutes"], 180)

    def test_punch_in_errors(self):
        # 1. Unknown employee -> 404
        r1 = client.post("/attendance/punch-in", json={"emp_code": "EMP999999"})
        self.assertEqual(r1.status_code, 404)

        # 2. Invalid status (e.g. ABSENT is only created by back-office) -> 422
        emp_code = self.make_unique_emp_code()
        client.post("/employees", json={
            "emp_code": emp_code,
            "name": "Status Tester",
            "email": f"{emp_code.lower()}@test.com",
            "department": "QA",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        })
        r2 = client.post("/attendance/punch-in", json={
            "emp_code": emp_code,
            "status": "ABSENT"
        })
        self.assertEqual(r2.status_code, 422)

    # ----------------------------------------------------------------------- #
    # 4. Attendance Punch-Out API
    # ----------------------------------------------------------------------- #
    def test_punch_out_computations_and_errors(self):
        emp_code = self.make_unique_emp_code()
        client.post("/employees", json={
            "emp_code": emp_code,
            "name": "Out Tester",
            "email": f"{emp_code.lower()}@test.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        })

        in_dt = datetime(2026, 8, 12, 9, 30, 0, tzinfo=IST)
        in_ms = int(in_dt.timestamp() * 1000)
        client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": in_ms})

        # 1. Punch-out before punch-in -> 422
        r_err1 = client.post("/attendance/punch-out", json={
            "emp_code": emp_code,
            "punched_at": in_ms - 1000
        })
        self.assertEqual(r_err1.status_code, 422)

        # 2. Punch-out > 24 hours -> 422
        r_err2 = client.post("/attendance/punch-out", json={
            "emp_code": emp_code,
            "punched_at": in_ms + (25 * 3600 * 1000)
        })
        self.assertEqual(r_err2.status_code, 422)

        # 3. Successful punch-out: worked 9.75 hours, 45 minutes overtime (19:15 IST)
        out_dt = datetime(2026, 8, 12, 19, 15, 0, tzinfo=IST)
        out_ms = int(out_dt.timestamp() * 1000)
        r_ok = client.post("/attendance/punch-out", json={
            "emp_code": emp_code,
            "punched_at": out_ms
        })
        self.assertEqual(r_ok.status_code, 200)
        data = r_ok.json()
        self.assertEqual(data["work_hours"], 9.75)
        self.assertEqual(data["overtime_minutes"], 45)
        self.assertFalse(data["half_day"])

        # 4. Duplicate punch-out -> 409
        r_dup = client.post("/attendance/punch-out", json={
            "emp_code": emp_code,
            "punched_at": out_ms + 1000
        })
        self.assertEqual(r_dup.status_code, 409)

    # ----------------------------------------------------------------------- #
    # 5. Attendance Listing API
    # ----------------------------------------------------------------------- #
    def test_list_attendance_filters(self):
        emp_code = self.make_unique_emp_code()
        client.post("/employees", json={
            "emp_code": emp_code,
            "name": "List Tester",
            "email": f"{emp_code.lower()}@test.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        })

        dt1 = datetime(2026, 8, 1, 9, 30, 0, tzinfo=IST)
        client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": int(dt1.timestamp() * 1000)})

        # date_from > date_to -> 422
        r_err = client.get(f"/attendance?emp_code={emp_code}&date_from=2026-08-10&date_to=2026-08-01")
        self.assertEqual(r_err.status_code, 422)

        # valid query
        r_ok = client.get(f"/attendance?emp_code={emp_code}&date_from=2026-08-01&date_to=2026-08-05")
        self.assertEqual(r_ok.status_code, 200)
        self.assertGreaterEqual(r_ok.json()["total"], 1)

    # ----------------------------------------------------------------------- #
    # 6. Regularization API (PATCH)
    # ----------------------------------------------------------------------- #
    def test_regularize_attendance_flow_and_audit(self):
        emp_code = self.make_unique_emp_code()
        client.post("/employees", json={
            "emp_code": emp_code,
            "name": "Reg Tester",
            "email": f"{emp_code.lower()}@test.com",
            "department": "HR",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        })

        in_dt = datetime(2026, 8, 15, 9, 30, 0, tzinfo=IST)
        client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": int(in_dt.timestamp() * 1000)})

        # 1. No-op regularization -> 422
        r_noop = client.patch(f"/attendance/{emp_code}/2026-08-15", json={
            "reason": "Nothing changed here",
            "regularized_by": "Manager Bob"
        })
        self.assertEqual(r_noop.status_code, 422)

        # 2. Add punch_out to complete day
        out_dt = datetime(2026, 8, 15, 18, 30, 0, tzinfo=IST)
        r_reg = client.patch(f"/attendance/{emp_code}/2026-08-15", json={
            "reason": "Forgot to punch out",
            "regularized_by": "Manager Bob",
            "punch_out": int(out_dt.timestamp() * 1000)
        })
        self.assertEqual(r_reg.status_code, 200)
        record = r_reg.json()
        self.assertEqual(len(record["history"]), 1)
        entry = record["history"][0]
        self.assertEqual(entry["by"], "Manager Bob")
        self.assertEqual(entry["reason"], "Forgot to punch out")
        self.assertIn("punch_out", entry["changes"])
        self.assertIn("work_hours", entry["changes"])
        self.assertEqual(record["work_hours"], 9.0)

        # 3. Regularize with ABSENT while supplying punch times -> 422
        r_absent_err = client.patch(f"/attendance/{emp_code}/2026-08-15", json={
            "status": "ABSENT",
            "punch_in": int(in_dt.timestamp() * 1000),
            "reason": "Change to absent with punch times",
            "regularized_by": "Manager Bob"
        })
        self.assertEqual(r_absent_err.status_code, 422)

    # ----------------------------------------------------------------------- #
    # 7. Employee Monthly Summary API
    # ----------------------------------------------------------------------- #
    def test_employee_monthly_analytics_edge_cases(self):
        emp_code = self.make_unique_emp_code()
        # Mid-month joiner: joined on Monday 2026-08-17
        client.post("/employees", json={
            "emp_code": emp_code,
            "name": "Mid Month Joiner",
            "email": f"{emp_code.lower()}@test.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-08-17",
        })

        # Punch in on weekday 2026-08-17 and punch out for 9 hours
        in_dt = datetime(2026, 8, 17, 9, 30, 0, tzinfo=IST)
        out_dt = datetime(2026, 8, 17, 18, 30, 0, tzinfo=IST)
        client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": int(in_dt.timestamp() * 1000)})
        client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": int(out_dt.timestamp() * 1000)})

        r = client.get(f"/analytics/employees/{emp_code}/monthly?month=2026-08")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["emp_code"], emp_code)
        self.assertEqual(data["month"], "2026-08")
        # Working days in August 2026 from 17th to 31st (11 weekdays: Aug 17-21, 24-28, 31)
        self.assertEqual(data["working_days"], 11)
        self.assertEqual(data["present_days"], 1.0)
        # 1 present day / 11 working days * 100 = 9.0909... -> 9.09%
        self.assertEqual(data["attendance_pct"], 9.09)

        # Employee who joined after requested month -> working_days = 0, attendance_pct = null
        r_past = client.get(f"/analytics/employees/{emp_code}/monthly?month=2026-07")
        self.assertEqual(r_past.status_code, 200)
        data_past = r_past.json()
        self.assertEqual(data_past["working_days"], 0)
        self.assertIsNone(data_past["attendance_pct"])

    # ----------------------------------------------------------------------- #
    # 8. Complete 12-Endpoint Integration Regression Flow
    # ----------------------------------------------------------------------- #
    def test_complete_system_integration_regression(self):
        # 1. GET /health
        r1 = client.get("/health")
        self.assertEqual(r1.status_code, 200)

        # 2. GET /employees
        r2 = client.get("/employees")
        self.assertEqual(r2.status_code, 200)

        # 3. POST /employees
        unique_code = self.make_unique_emp_code()
        r3 = client.post("/employees", json={
            "emp_code": unique_code,
            "name": "Integration Reg Emp",
            "email": f"{unique_code.lower()}@example.com",
            "department": "QA",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })
        self.assertEqual(r3.status_code, 201)

        # 4. POST /attendance/punch-in
        now_ms = int(datetime(2026, 1, 15, 9, 30, tzinfo=IST).timestamp() * 1000)
        r4 = client.post("/attendance/punch-in", json={
            "emp_code": unique_code,
            "punched_at": now_ms,
            "status": "PRESENT"
        })
        self.assertEqual(r4.status_code, 201)

        # 5. POST /attendance/punch-out
        out_ms = int(datetime(2026, 1, 15, 18, 30, tzinfo=IST).timestamp() * 1000)
        r5 = client.post("/attendance/punch-out", json={
            "emp_code": unique_code,
            "punched_at": out_ms
        })
        self.assertEqual(r5.status_code, 200)

        # 6. GET /attendance
        r6 = client.get(f"/attendance?emp_code={unique_code}")
        self.assertEqual(r6.status_code, 200)

        # 7. PATCH /attendance/{emp_code}/{date}
        r7 = client.patch(f"/attendance/{unique_code}/2026-01-15", json={
            "reason": "Regularizing punch out time",
            "regularized_by": "Manager Bob",
            "punch_out": int(datetime(2026, 1, 15, 19, 0, tzinfo=IST).timestamp() * 1000)
        })
        self.assertEqual(r7.status_code, 200)

        # 8. GET /analytics/employees/{emp_code}/monthly
        r8 = client.get(f"/analytics/employees/{unique_code}/monthly?month=2026-01")
        self.assertEqual(r8.status_code, 200)

        # 9. GET /analytics/departments/summary
        r9 = client.get("/analytics/departments/summary?month=2026-01")
        self.assertEqual(r9.status_code, 200)

        # 10. GET /analytics/leaderboard/late
        r10 = client.get("/analytics/leaderboard/late?month=2026-01")
        self.assertEqual(r10.status_code, 200)

        # 11. GET /analytics/departments/{department}/trend
        r11 = client.get("/analytics/departments/QA/trend?from=2026-01-01&to=2026-01-05")
        self.assertEqual(r11.status_code, 200)

        # 12. GET /admin/explain/{endpoint}
        r12 = client.get("/admin/explain/attendance_list")
        self.assertEqual(r12.status_code, 200)


if __name__ == "__main__":
    unittest.main()
