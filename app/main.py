"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
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

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])

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
