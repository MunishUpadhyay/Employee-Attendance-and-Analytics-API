"""
Concurrency test for POST /attendance/punch-out.
Verifies that simultaneous punch-out requests do not incorrectly update
attendance multiple times or corrupt the record (OCC returns 200 for one and 409 for the other).
Fully isolated from pre-existing or seeded records.
"""
import asyncio
from datetime import datetime, timezone
import unittest
import uuid
import httpx

from app.main import app, db, IST


class TestConcurrentPunchOut(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Generate a unique 6-digit emp_code that does not collide with seed data (EMP0001-EMP0006)
        self.emp_code = f"EMP{uuid.uuid4().int % 900000 + 100000:06d}"
        self.date_str = "2026-01-15"

        db.employees.insert_one({
            "emp_code": self.emp_code,
            "name": "Concurrency Candidate",
            "email": f"{self.emp_code.lower()}@test.com",
            "department": "Engineering",
            "shift_start": "09:00",
            "shift_end": "18:00",
            "joined_on": "2026-01-01",
            "created_at": datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        })

        punch_in_dt = datetime(2026, 1, 15, 9, 0, tzinfo=IST)
        db.attendance_logs.insert_one({
            "emp_code": self.emp_code,
            "date": self.date_str,
            "status": "PRESENT",
            "punch_in": punch_in_dt,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False
        })

    async def asyncTearDown(self):
        # Deterministic cleanup only for the unique record created for this test run
        if hasattr(self, "emp_code") and self.emp_code:
            db.employees.delete_one({"emp_code": self.emp_code})
            db.attendance_logs.delete_many({"emp_code": self.emp_code})

    async def test_concurrent_punch_out(self):
        punch_out_ms = int(datetime(2026, 1, 15, 18, 0, tzinfo=IST).timestamp() * 1000)
        payload = {"emp_code": self.emp_code, "punched_at": punch_out_ms}

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            req1 = client.post("/attendance/punch-out", json=payload)
            req2 = client.post("/attendance/punch-out", json=payload)
            res1, res2 = await asyncio.gather(req1, req2)

        status_codes = sorted([res1.status_code, res2.status_code])
        # Exactly one request must succeed (200), and the other must be rejected (409)
        self.assertEqual(status_codes, [200, 409], f"Unexpected status codes: {res1.status_code}, {res2.status_code}")

        # Verify attendance record was updated cleanly and not corrupted
        record = db.attendance_logs.find_one({"emp_code": self.emp_code, "date": self.date_str})
        self.assertIsNotNone(record)
        self.assertIsNotNone(record["punch_out"])
        self.assertEqual(record["work_hours"], 9.0)


if __name__ == "__main__":
    unittest.main()
