"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import calendar
from datetime import datetime, timedelta, timezone, time
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import PyMongoError
from pymongo.errors import DuplicateKeyError
from contextlib import asynccontextmanager
from decimal import Decimal, ROUND_HALF_UP
from bson.decimal128 import Decimal128

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index("joined_on")

    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True
    )

    db.attendance_logs.create_index(
        [("emp_code", 1), ("punch_in", -1)]
    )

    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)]
    )

    yield

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))
    start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
    diff_seconds = (punch_in - start).total_seconds()
    if diff_seconds > 600:
        return int(diff_seconds // 60)
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    duration_seconds = Decimal(
        str((punch_out - punch_in).total_seconds())
    )
    hours = duration_seconds / Decimal("3600")

    return float(
        hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: Optional[str] = None) -> int:
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)
    if shift_start and shift_end <= shift_start:
        end += timedelta(days=1)
    if punch_out.tzinfo is None:
        punch_out = punch_out.replace(tzinfo=timezone.utc)
    punch_out_ist = punch_out.astimezone(IST)
    diff_seconds = (punch_out_ist - end).total_seconds()
    if diff_seconds >= 1800:
        return int(diff_seconds // 60)
    return 0


def serialize_datetimes(value):
    if isinstance(value, datetime):
        
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        
        return int(value.timestamp()) * 1000

    if isinstance(value, dict):
        return {
            key: serialize_datetimes(item)
            for key, item in value.items()
        }

    if isinstance(value, list):
        return [serialize_datetimes(item) for item in value]

    return value


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
        max_length=120
    )
    department: str = Field(min_length=1, max_length=50)

    shift_start: str = Field(
        default="09:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$"
    )
    shift_end: str = Field(
        default="18:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$"
    )

    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")

        if parsed.strftime("%Y-%m-%d") != value:
            raise ValueError("joined_on must use YYYY-MM-DD format")

        return value

    @model_validator(mode="after")
    def validate_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self



class PunchInIn(BaseModel):
    model_config = {
        "json_schema_extra": {
            "examples": [
                {"emp_code": "string"}
            ]
        }
    }

    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True)
    status: str = "PRESENT"



class PunchOutIn(BaseModel):
    model_config = {
        "json_schema_extra": {
            "examples": [
                {"emp_code": "E101"}
            ]
        }
    }

    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True)


class RegularizeIn(BaseModel):
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(default=None, strict=True, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(default=None, strict=True, ge=100000000000, le=4102444800000)


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable"
        )


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")

    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists"
        )

    doc.pop("_id", None)
    return serialize_datetimes(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100)
):
    q = {}
    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": serialize_datetimes(items),
        "total": total,
        "page": page,
        "page_size": page_size
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found"
        )

    if body.status not in {"PRESENT", "WFH", "ON_DUTY"}:
        raise HTTPException(
            status_code=422,
            detail="Invalid status for punch-in"
        )
  
    if body.punched_at is not None and not (
        100000000000 <= body.punched_at <= 4102444800000
    ):
        raise HTTPException(
            status_code=422,
            detail="Invalid punched_at timestamp"
        )

    try:
        ts = (
            datetime.fromtimestamp(
                body.punched_at / 1000,
                tz=timezone.utc
            )
            if body.punched_at is not None
            else datetime.now(timezone.utc)
        )
        ts = ts.replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        raise HTTPException(
            status_code=422,
            detail="Invalid punched_at timestamp"
        )

    ts = ts.astimezone(IST)
    end_h, end_m = map(int, emp["shift_end"].split(":"))
    if emp["shift_end"] <= emp["shift_start"] and ts.time() < time(end_h, end_m):
        d = (ts - timedelta(days=1)).date().isoformat()
    else:
        d = ts.date().isoformat()

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            ts, emp["shift_start"]
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="already punched in for this date"
        )

    doc.pop("_id", None)
    return serialize_datetimes(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found"
        )

    if body.punched_at is not None and not (
        100000000000 <= body.punched_at <= 4102444800000
    ):
        raise HTTPException(
            status_code=422,
            detail="Invalid punched_at timestamp"
        )

    try:
        ts = (
            datetime.fromtimestamp(
                body.punched_at / 1000,
                tz=timezone.utc
            )
            if body.punched_at is not None
            else datetime.now(timezone.utc)
        )
        ts = ts.replace(microsecond=0)

    except (OverflowError, OSError, ValueError):
        raise HTTPException(
            status_code=422,
            detail="Invalid punched_at timestamp"
        )

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": ts}
        },
        sort=[("punch_in", -1)]
    )

    if record is None:
        record = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$ne": None, "$gt": ts}
            },
            sort=[("punch_in", 1)]
        )

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="No punch-in record found"
        )

    punch_in_time = record["punch_in"]

    if punch_in_time.tzinfo is None:
        punch_in_time = punch_in_time.replace(
            tzinfo=timezone.utc
        )

    if ts <= punch_in_time:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in"
        )

    elapsed = ts - punch_in_time

    if elapsed > timedelta(hours=24):
        raise HTTPException(
            status_code=422,
            detail="Punch-out cannot be more than 24 hours after punch-in"
        )

    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="Attendance record has already been punched out"
        )

    work_hours = compute_work_hours(
        punch_in_time,
        ts
    )

    overtime_minutes = compute_overtime(
        ts, emp["shift_end"], record["date"], emp["shift_start"]
    )

    half_day = work_hours < 4.50

    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if updated is None:
        raise HTTPException(
            status_code=409,
            detail="Attendance record has already been punched out"
        )

    updated.pop("_id", None)
    updated.setdefault("history", [])
    updated.setdefault("late_minutes", 0)
    updated.setdefault("overtime_minutes", 0)
    updated.setdefault("half_day", False)

    return serialize_datetimes(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):

    q = {}

    if emp_code:
        q["emp_code"] = emp_code

    if status:
        q["status"] = status

    try:
        start_date = (
            datetime.strptime(date_from, "%Y-%m-%d").date()
            if date_from
            else None
        )
        end_date = (
            datetime.strptime(date_to, "%Y-%m-%d").date()
            if date_to
            else None
        )
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Dates must use YYYY-MM-DD format",
        )

    if date_from and start_date.isoformat() != date_from:
        raise HTTPException(
            status_code=422,
            detail="date_from must use YYYY-MM-DD format",
        )

    if date_to and end_date.isoformat() != date_to:
        raise HTTPException(
            status_code=422,
            detail="date_to must use YYYY-MM-DD format",
        )

    if start_date and end_date and start_date > end_date:
        raise HTTPException(
            status_code=422,
            detail="date_from must not be after date_to",
        )

    if start_date or end_date:
        q["date"] = {}

        if start_date:
            q["date"]["$gte"] = start_date.isoformat()

        if end_date:
            q["date"]["$lte"] = end_date.isoformat()

    # Count only records matching the filters.
    total = db.attendance_logs.count_documents(q)

    # Let MongoDB sort and paginate instead of loading the whole collection.
    skip = (page - 1) * page_size

    items = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    for item in items:
        item.setdefault("half_day", False)
        item.setdefault("history", [])
        item.setdefault("late_minutes", 0)
        item.setdefault("overtime_minutes", 0)

    return {
        "items": serialize_datetimes(items),
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    try:
        parsed_date = datetime.strptime(date, "%Y-%m-%d").date()
        if parsed_date.isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="date must use YYYY-MM-DD format"
        )

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found"
        )

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(
            status_code=404,
            detail="Attendance record not found"
        )

    target_status = body.status if body.status is not None else record["status"]

    if target_status in {"ABSENT", "LEAVE"} and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(
            status_code=422,
            detail="Cannot supply punch times when status is ABSENT or LEAVE"
        )

    old_punch_in = record.get("punch_in")
    if old_punch_in is not None:
        if old_punch_in.tzinfo is None:
            old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)
        old_punch_in = old_punch_in.replace(microsecond=0)

    old_punch_out = record.get("punch_out")
    if old_punch_out is not None:
        if old_punch_out.tzinfo is None:
            old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)
        old_punch_out = old_punch_out.replace(microsecond=0)

    if target_status in {"ABSENT", "LEAVE"}:
        effective_punch_in = None
        effective_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        if body.punch_in is not None:
            effective_punch_in = datetime.fromtimestamp(body.punch_in / 1000, tz=timezone.utc).replace(microsecond=0)
        else:
            effective_punch_in = old_punch_in

        if effective_punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="Presence status requires a punch_in"
            )

        ts_ist = effective_punch_in.astimezone(IST)
        end_h, end_m = map(int, emp["shift_end"].split(":"))
        if emp["shift_end"] <= emp["shift_start"] and ts_ist.time() < time(end_h, end_m):
            punch_date = (ts_ist - timedelta(days=1)).date().isoformat()
        else:
            punch_date = ts_ist.date().isoformat()

        if punch_date != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must stay on the record's attendance date"
            )

        if body.punch_out is not None:
            effective_punch_out = datetime.fromtimestamp(body.punch_out / 1000, tz=timezone.utc).replace(microsecond=0)
        else:
            effective_punch_out = old_punch_out

        if effective_punch_out is not None:
            if effective_punch_out <= effective_punch_in:
                raise HTTPException(
                    status_code=422,
                    detail="Punch-out must be after punch-in"
                )
            if (effective_punch_out - effective_punch_in) > timedelta(hours=24):
                raise HTTPException(
                    status_code=422,
                    detail="Punch-out cannot be more than 24 hours after punch-in"
                )
            new_work_hours = compute_work_hours(effective_punch_in, effective_punch_out)
            new_overtime_minutes = compute_overtime(effective_punch_out, emp["shift_end"], date, emp["shift_start"])
            new_half_day = new_work_hours < 4.50
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

        new_late_minutes = compute_late_minutes(ts_ist, emp["shift_start"])

    changes = {}
    if record["status"] != target_status:
        changes["status"] = {"from": record["status"], "to": target_status}
    if old_punch_in != effective_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": effective_punch_in}
    if old_punch_out != effective_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": effective_punch_out}
    if record.get("work_hours") != new_work_hours:
        changes["work_hours"] = {"from": record.get("work_hours"), "to": new_work_hours}
    if record.get("late_minutes", 0) != new_late_minutes:
        changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": new_late_minutes}
    if record.get("overtime_minutes", 0) != new_overtime_minutes:
        changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": new_overtime_minutes}
    if record.get("half_day", False) != new_half_day:
        changes["half_day"] = {"from": record.get("half_day", False), "to": new_half_day}

    if not changes:
        raise HTTPException(
            status_code=422,
            detail="Request changes nothing"
        )

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    history_len = len(record.get("history", []))

    persisted_punch_in = effective_punch_in if old_punch_in != effective_punch_in else record.get("punch_in")
    persisted_punch_out = effective_punch_out if old_punch_out != effective_punch_out else record.get("punch_out")

    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            f"history.{history_len}": {"$exists": False}
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": persisted_punch_in,
                "punch_out": persisted_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day
            },
            "$push": {
                "history": history_entry
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if updated is None:
        raise HTTPException(
            status_code=409,
            detail="Concurrent regularization conflict"
        )

    updated.pop("_id", None)
    updated.setdefault("history", [])
    updated.setdefault("late_minutes", 0)
    updated.setdefault("overtime_minutes", 0)
    updated.setdefault("half_day", False)

    return serialize_datetimes(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found"
        )

    year, month_num = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, month_num)
    month_start = datetime(year, month_num, 1).date()
    month_end = datetime(year, month_num, last_day).date()

    emp_joined_on = datetime.strptime(emp["joined_on"], "%Y-%m-%d").date()
    start_date = max(month_start, emp_joined_on)

    if start_date > month_end:
        working_days = 0
    else:
        working_days = sum(
            1 for day_offset in range((month_end - start_date).days + 1)
            if (start_date + timedelta(days=day_offset)).weekday() < 5
        )

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-{last_day:02d}"}
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]}
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1.0
                                ]
                            },
                            0.0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                }
            }
        }
    ]

    results = list(db.attendance_logs.aggregate(pipeline))

    if results:
        m = results[0]
        present_days = float(m["present_days"])
        leave_days = int(m["leave_days"])
        late_count = int(m["late_count"])
        total_late_minutes = int(m["total_late_minutes"])
        total_overtime_minutes = int(m["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days == 0:
        attendance_pct = None
    else:
        pct_dec = (Decimal(str(present_days)) * Decimal(100)) / Decimal(working_days)
        attendance_pct = float(pct_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(default=None)
):
    parts = month.split("-")
    year = int(parts[0])
    m_int = int(parts[1])
    _, last_day = calendar.monthrange(year, m_int)

    match_stage = {"joined_on": {"$lte": f"{month}-{last_day:02d}"}}
    if department is not None:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {
                        "$match": {
                            "date": {
                                "$gte": f"{month}-01",
                                "$lte": f"{month}-{last_day:02d}"
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]}
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": ["$half_day", True]},
                                                0.5,
                                                1.0
                                            ]
                                        },
                                        0.0
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0
                                    ]
                                }
                            },
                            "total_late_minutes": {
                                "$sum": {"$ifNull": ["$late_minutes", 0]}
                            },
                            "leave_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$status", "LEAVE"]},
                                        1,
                                        0
                                    ]
                                }
                            },
                            "on_duty_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$status", "ON_DUTY"]},
                                        1,
                                        0
                                    ]
                                }
                            },
                            "work_hours_sum": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$isNumber": "$work_hours"}
                                            ]
                                        },
                                        {"$toDecimal": "$work_hours"},
                                        {"$toDecimal": 0}
                                    ]
                                }
                            },
                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$isNumber": "$work_hours"}
                                            ]
                                        },
                                        1,
                                        0
                                    ]
                                }
                            }
                        }
                    }
                ],
                "as": "stats"
            }
        },
        {
            "$addFields": {
                "emp_stats": {"$ifNull": [{"$arrayElemAt": ["$stats", 0]}, {}]}
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": {"$ifNull": ["$emp_stats.present_days", 0.0]}},
                "late_count": {"$sum": {"$ifNull": ["$emp_stats.late_count", 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$emp_stats.total_late_minutes", 0]}},
                "leave_count": {"$sum": {"$ifNull": ["$emp_stats.leave_count", 0]}},
                "on_duty_count": {"$sum": {"$ifNull": ["$emp_stats.on_duty_count", 0]}},
                "work_hours_sum": {"$sum": {"$ifNull": ["$emp_stats.work_hours_sum", {"$toDecimal": 0}]}},
                "work_hours_count": {"$sum": {"$ifNull": ["$emp_stats.work_hours_count", 0]}}
            }
        },
        {"$sort": {"_id": 1}}
    ]

    results = list(db.employees.aggregate(pipeline))

    items = []
    for doc in results:
        wh_count = int(doc["work_hours_count"])
        if wh_count > 0:
            wh_sum = doc["work_hours_sum"].to_decimal() if hasattr(doc["work_hours_sum"], "to_decimal") else Decimal(str(doc["work_hours_sum"]))
            wh_cnt = Decimal(wh_count)
            avg_work_hours = float((wh_sum / wh_cnt).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        else:
            avg_work_hours = None

        items.append({
            "department": doc["_id"],
            "headcount": int(doc["headcount"]),
            "present_days": float(doc["present_days"]),
            "avg_work_hours": avg_work_hours,
            "late_count": int(doc["late_count"]),
            "total_late_minutes": int(doc["total_late_minutes"]),
            "leave_count": int(doc["leave_count"]),
            "on_duty_count": int(doc["on_duty_count"])
        })

    return {
        "month": month,
        "items": items
    }


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None):
    parts = month.split("-")
    year = int(parts[0])
    m_int = int(parts[1])
    _, last_day = calendar.monthrange(year, m_int)

    pipeline = [
        {
            "$match": {
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-{last_day:02d}"},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"}
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = Query(default=None)
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    results = list(db.attendance_logs.aggregate(pipeline))

    items = [
        {
            "rank": int(doc["rank"]),
            "emp_code": str(doc["emp_code"]),
            "name": str(doc["name"]),
            "department": str(doc["department"]),
            "total_late_minutes": int(doc["total_late_minutes"]),
            "late_count": int(doc["late_count"])
        }
        for doc in results
    ]

    return {
        "month": month,
        "items": items
    }



# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
