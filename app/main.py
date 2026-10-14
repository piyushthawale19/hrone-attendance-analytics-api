"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (loaded from environment or .env)
"""

import calendar
from contextlib import asynccontextmanager
from datetime import date as dt_date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
import re
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, field_validator, model_validator
import pymongo
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_db():
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1)])

    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])
    db.attendance_logs.create_index([("date", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers & Business Logic (R1 - R10)
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[float | Decimal], places: int = 2) -> Optional[float]:
    if val is None:
        return None
    d = Decimal(str(val)) if not isinstance(val, Decimal) else val
    target = Decimal("10") ** -places
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def from_epoch_ms(ms: int) -> datetime:
    # R1: truncate to whole seconds
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def now_utc_sec() -> datetime:
    # R1: truncate to whole seconds
    return datetime.now(timezone.utc).replace(microsecond=0)


def get_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    punch_in_ist = punch_in_dt.astimezone(IST)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        end_h, end_m = map(int, shift_end.split(":"))
        shift_end_time = time(hour=end_h, minute=end_m)
        if punch_in_ist.time() < shift_end_time:
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_dt: datetime, date_str: str, shift_start: str) -> int:
    punch_in_ist = punch_in_dt.astimezone(IST)
    h, m = map(int, shift_start.split(":"))
    scheduled_start = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    diff_sec = (punch_in_ist - scheduled_start).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_overtime(punch_out_dt: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    punch_out_ist = punch_out_dt.astimezone(IST)
    end_h, end_m = map(int, shift_end.split(":"))
    is_overnight = shift_end <= shift_start
    if is_overnight:
        scheduled_end = (datetime.fromisoformat(date_str) + timedelta(days=1)).replace(
            hour=end_h, minute=end_m, tzinfo=IST
        )
    else:
        scheduled_end = datetime.fromisoformat(date_str).replace(
            hour=end_h, minute=end_m, tzinfo=IST
        )
    diff_sec = (punch_out_ist - scheduled_end).total_seconds()
    minutes = int(diff_sec // 60)
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    diff_sec = int((punch_out_dt - punch_in_dt).total_seconds())
    val = Decimal(diff_sec) / Decimal(3600)
    return float(val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    return work_hours is not None and work_hours < 4.50


def serialize_attendance_record(doc: dict) -> dict:
    res = {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [],
    }
    for h in doc.get("history", []):
        changes_api = {}
        for k, v in h.get("changes", {}).items():
            if k in ("punch_in", "punch_out"):
                from_val = to_epoch_ms(v.get("from")) if isinstance(v.get("from"), datetime) else v.get("from")
                to_val = to_epoch_ms(v.get("to")) if isinstance(v.get("to"), datetime) else v.get("to")
            else:
                from_val = v.get("from")
                to_val = v.get("to")
            changes_api[k] = {"from": from_val, "to": to_val}
        res["history"].append({
            "at": to_epoch_ms(h["at"]),
            "by": h["by"],
            "reason": h["reason"],
            "changes": changes_api,
        })
    return res


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc["created_at"]),
    }


# --------------------------------------------------------------------------- #
# Request & Response Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000, strict=True)
    status: str = Field(default="PRESENT")

    @field_validator("status")
    @classmethod
    def validate_presence_status(cls, v: str) -> str:
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000, strict=True)


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000, strict=True)
    punch_out: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000, strict=True)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise ValueError("invalid status value")
        return v


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    created_at = now_utc_sec()
    doc = body.model_dump()
    doc["created_at"] = created_at
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    punch_in_dt = from_epoch_ms(body.punched_at) if body.punched_at is not None else now_utc_sec()
    date_str = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(punch_in_dt, date_str, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return serialize_attendance_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    punch_out_dt = from_epoch_ms(body.punched_at) if body.punched_at is not None else now_utc_sec()

    # Find employee's most recent record with a punch_in
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="No matching punch-in found")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    if diff_sec <= 0 or diff_sec > 24 * 3600:
        raise HTTPException(
            status_code=422,
            detail="punched_at must be strictly after punch_in and within 24 hours",
        )

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out for this record")

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime_minutes = compute_overtime(
        punch_out_dt, record["date"], emp["shift_start"], emp["shift_end"]
    )
    half_day = compute_half_day(work_hours)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=pymongo.ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="already punched out for this record")

    return serialize_attendance_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        if status not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise HTTPException(status_code=422, detail="Invalid status")
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_attendance_record(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    old_status = record["status"]
    old_punch_in = record.get("punch_in")
    if old_punch_in and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)
    old_punch_out = record.get("punch_out")
    if old_punch_out and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)
    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes", 0)
    old_overtime_minutes = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)

    final_status = body.status if body.status is not None else old_status

    if final_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="punch_in and punch_out cannot be supplied when status is ABSENT or LEAVE",
            )
        final_punch_in = None
        final_punch_out = None
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            final_punch_in = from_epoch_ms(body.punch_in)
        else:
            final_punch_in = old_punch_in

        if final_punch_in is None:
            raise HTTPException(
                status_code=422, detail="Presence status requires a punch_in"
            )

        calc_date = get_attendance_date(final_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must stay on the record's attendance date",
            )

        if body.punch_out is not None:
            final_punch_out = from_epoch_ms(body.punch_out)
        else:
            final_punch_out = old_punch_out

        if final_punch_out is not None:
            diff_sec = (final_punch_out - final_punch_in).total_seconds()
            if diff_sec <= 0 or diff_sec > 24 * 3600:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be strictly after punch_in and within 24 hours",
                )
            final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            final_overtime_minutes = compute_overtime(
                final_punch_out, date, emp["shift_start"], emp["shift_end"]
            )
            final_half_day = compute_half_day(final_work_hours)
        else:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False

        final_late_minutes = compute_late_minutes(final_punch_in, date, emp["shift_start"])

    # Compute changes
    changes_db = {}
    if final_status != old_status:
        changes_db["status"] = {"from": old_status, "to": final_status}
    if final_punch_in != old_punch_in:
        changes_db["punch_in"] = {"from": old_punch_in, "to": final_punch_in}
    if final_punch_out != old_punch_out:
        changes_db["punch_out"] = {"from": old_punch_out, "to": final_punch_out}
    if final_work_hours != old_work_hours:
        changes_db["work_hours"] = {"from": old_work_hours, "to": final_work_hours}
    if final_late_minutes != old_late_minutes:
        changes_db["late_minutes"] = {"from": old_late_minutes, "to": final_late_minutes}
    if final_overtime_minutes != old_overtime_minutes:
        changes_db["overtime_minutes"] = {"from": old_overtime_minutes, "to": final_overtime_minutes}
    if final_half_day != old_half_day:
        changes_db["half_day"] = {"from": old_half_day, "to": final_half_day}

    if not changes_db:
        raise HTTPException(status_code=422, detail="A request that changes nothing is 422")

    history_entry = {
        "at": now_utc_sec(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_db,
    }

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "history": record.get("history", [])},
        {
            "$set": {
                "status": final_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": final_work_hours,
                "late_minutes": final_late_minutes,
                "overtime_minutes": final_overtime_minutes,
                "half_day": final_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=pymongo.ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent modification detected")

    return serialize_attendance_record(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    if emp["joined_on"] > month_end:
        working_days = 0
    else:
        start_date_str = max(month_start, emp["joined_on"])
        cur = datetime.fromisoformat(start_date_str).date()
        end_d = datetime.fromisoformat(month_end).date()
        working_days = 0
        while cur <= end_d:
            if cur.weekday() < 5:
                working_days += 1
            cur += timedelta(days=1)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        {
            "$project": {
                "is_weekday": {
                    "$in": [
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        [2, 3, 4, 5, 6],
                    ]
                },
                "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "is_leave": {"$eq": ["$status", "LEAVE"]},
                "is_late": {"$gt": ["$late_minutes", 0]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_weekday", "$is_present"]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": ["$is_leave", 1, 0]}},
                "late_count": {"$sum": {"$cond": ["$is_late", 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    agg = list(db.attendance_logs.aggregate(pipeline))
    if agg:
        res = agg[0]
        present_days = float(res.get("present_days", 0.0))
        leave_days = int(res.get("leave_days", 0))
        late_count = int(res.get("late_count", 0))
        total_late_minutes = int(res.get("total_late_minutes", 0))
        total_overtime_minutes = int(res.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        pct = (present_days / working_days) * 100
        attendance_pct = round_half_up(pct, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    match_stage = {"joined_on": {"$lte": month_end}}
    if department:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp_code"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "logs": {
                    "$map": {
                        "input": "$logs",
                        "as": "log",
                        "in": {
                            "is_weekday": {
                                "$in": [
                                    {"$dayOfWeek": {"$dateFromString": {"dateString": "$$log.date"}}},
                                    [2, 3, 4, 5, 6],
                                ]
                            },
                            "is_present": {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            "half_day": {"$ifNull": ["$$log.half_day", False]},
                            "work_hours": "$$log.work_hours",
                            "late_minutes": {"$ifNull": ["$$log.late_minutes", 0]},
                            "is_leave": {"$eq": ["$$log.status", "LEAVE"]},
                            "is_on_duty": {"$eq": ["$$log.status", "ON_DUTY"]},
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$and": ["$$l.is_weekday", "$$l.is_present"]},
                                    {"$cond": ["$$l.half_day", 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "emp_wh_sum": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$and": ["$$l.is_present", {"$ne": ["$$l.work_hours", None]}]},
                                    "$$l.work_hours",
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "emp_wh_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$and": ["$$l.is_present", {"$ne": ["$$l.work_hours", None]}]},
                                    1,
                                    0,
                                ]
                            },
                        }
                    }
                },
                "emp_late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]},
                        }
                    }
                },
                "emp_total_late_minutes": {
                    "$sum": {"$map": {"input": "$logs", "as": "l", "in": "$$l.late_minutes"}}
                },
                "emp_leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": ["$$l.is_leave", 1, 0]},
                        }
                    }
                },
                "emp_on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": ["$$l.is_on_duty", 1, 0]},
                        }
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present_days"},
                "wh_sum": {"$sum": "$emp_wh_sum"},
                "wh_count": {"$sum": "$emp_wh_count"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$wh_count", 0]},
                        {"$round": [{"$divide": ["$wh_sum", "$wh_count"]}, 2]},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]

    items = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    pipeline = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    # Verify department exists
    dept_emp = db.employees.find_one({"department": department})
    if not dept_emp:
        raise HTTPException(status_code=404, detail="Department not found")

    d_from = dt_date.fromisoformat(from_date)
    d_to = dt_date.fromisoformat(to_date)
    if d_to < d_from:
        raise HTTPException(status_code=422, detail="to cannot be before from")

    num_days = (d_to - d_from).days + 1
    if num_days > 92:
        raise HTTPException(status_code=422, detail="range cannot exceed 92 days")

    start_dt = datetime.fromisoformat(from_date)
    end_dt = datetime.fromisoformat(to_date) + timedelta(days=1)

    pipeline = [
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$project": {
                "date": 1,
                "date_dt": {"$dateFromString": {"dateString": "$date"}},
                "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "is_late": {"$gt": ["$late_minutes", 0]},
            }
        },
        {
            "$group": {
                "_id": "$date_dt",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            "$is_present",
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": ["$is_late", 1, 0]}},
            }
        },
        {
            "$densify": {
                "field": "_id",
                "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt]},
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
                "is_working_day": {
                    "$in": [
                        {"$dayOfWeek": "$_id"},
                        [2, 3, 4, 5, 6],
                    ]
                },
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"current_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$current_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "emp_headcount",
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": {"$ifNull": [{"$first": "$emp_headcount.count"}, 0]},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {"$round": ["$moving_avg_7d", 4]},
                        None,
                    ]
                },
            }
        },
    ]

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[str] = None,
    from_date: Optional[str] = Query(default=None, alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: Optional[str] = Query(default=None, alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            if status not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
                raise HTTPException(status_code=422, detail="Invalid status")
            q["status"] = status
        skip = (page - 1) * page_size
        find_cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        raw_explain = db.command("explain", find_cmd, verbosity="executionStats")
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": jsonable_encoder(raw_explain),
        }

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(status_code=422, detail="month is required for employee_monthly")

        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        month_start = f"{month}-01"
        month_end = f"{month}-{num_days:02d}"

        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
            {
                "$project": {
                    "is_weekday": {
                        "$in": [
                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                            [2, 3, 4, 5, 6],
                        ]
                    },
                    "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    "half_day": {"$ifNull": ["$half_day", False]},
                    "is_leave": {"$eq": ["$status", "LEAVE"]},
                    "is_late": {"$gt": ["$late_minutes", 0]},
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                    "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {"$and": ["$is_weekday", "$is_present"]},
                                {"$cond": ["$half_day", 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "leave_days": {"$sum": {"$cond": ["$is_leave", 1, 0]}},
                    "late_count": {"$sum": {"$cond": ["$is_late", 1, 0]}},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                }
            },
        ]
        raw_explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": jsonable_encoder(raw_explain),
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")

        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        month_start = f"{month}-01"
        month_end = f"{month}-{num_days:02d}"

        match_stage = {"joined_on": {"$lte": month_end}}
        if department:
            match_stage["department"] = department

        pipeline = [
            {"$match": match_stage},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp_code": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp_code"]},
                                        {"$gte": ["$date", month_start]},
                                        {"$lte": ["$date", month_end]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {
                "$project": {
                    "department": 1,
                    "logs": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "is_weekday": {
                                    "$in": [
                                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$$log.date"}}},
                                        [2, 3, 4, 5, 6],
                                    ]
                                },
                                "is_present": {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                "half_day": {"$ifNull": ["$$log.half_day", False]},
                                "work_hours": "$$log.work_hours",
                                "late_minutes": {"$ifNull": ["$$log.late_minutes", 0]},
                                "is_leave": {"$eq": ["$$log.status", "LEAVE"]},
                                "is_on_duty": {"$eq": ["$$log.status", "ON_DUTY"]},
                            },
                        }
                    },
                }
            },
            {
                "$project": {
                    "department": 1,
                    "emp_present_days": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {
                                    "$cond": [
                                        {"$and": ["$$l.is_weekday", "$$l.is_present"]},
                                        {"$cond": ["$$l.half_day", 0.5, 1.0]},
                                        0.0,
                                    ]
                                },
                            }
                        }
                    },
                    "emp_wh_sum": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {
                                    "$cond": [
                                        {"$and": ["$$l.is_present", {"$ne": ["$$l.work_hours", None]}]},
                                        "$$l.work_hours",
                                        0.0,
                                    ]
                                },
                            }
                        }
                    },
                    "emp_wh_count": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {
                                    "$cond": [
                                        {"$and": ["$$l.is_present", {"$ne": ["$$l.work_hours", None]}]},
                                        1,
                                        0,
                                    ]
                                },
                            }
                        }
                    },
                    "emp_late_count": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]},
                            }
                        }
                    },
                    "emp_total_late_minutes": {
                        "$sum": {"$map": {"input": "$logs", "as": "l", "in": "$$l.late_minutes"}}
                    },
                    "emp_leave_count": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {"$cond": ["$$l.is_leave", 1, 0]},
                            }
                        }
                    },
                    "emp_on_duty_count": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {"$cond": ["$$l.is_on_duty", 1, 0]},
                            }
                        }
                    },
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$sum": 1},
                    "present_days": {"$sum": "$emp_present_days"},
                    "wh_sum": {"$sum": "$emp_wh_sum"},
                    "wh_count": {"$sum": "$emp_wh_count"},
                    "late_count": {"$sum": "$emp_late_count"},
                    "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
                    "leave_count": {"$sum": "$emp_leave_count"},
                    "on_duty_count": {"$sum": "$emp_on_duty_count"},
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "department": "$_id",
                    "headcount": 1,
                    "present_days": 1,
                    "avg_work_hours": {
                        "$cond": [
                            {"$gt": ["$wh_count", 0]},
                            {"$round": [{"$divide": ["$wh_sum", "$wh_count"]}, 2]},
                            None,
                        ]
                    },
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1,
                }
            },
            {"$sort": {"department": 1}},
        ]
        raw_explain = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": jsonable_encoder(raw_explain),
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")

        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        month_start = f"{month}-01"
        month_end = f"{month}-{num_days:02d}"

        pipeline = [
            {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "emp",
                }
            },
            {"$unwind": "$emp"},
        ]

        if department:
            pipeline.append({"$match": {"emp.department": department}})

        pipeline.extend([
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ])

        raw_explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": jsonable_encoder(raw_explain),
        }

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(status_code=422, detail="department is required for department_trend")
        if not from_date:
            raise HTTPException(status_code=422, detail="from is required for department_trend")
        if not to_date:
            raise HTTPException(status_code=422, detail="to is required for department_trend")

        d_from = dt_date.fromisoformat(from_date)
        d_to = dt_date.fromisoformat(to_date)
        if d_to < d_from:
            raise HTTPException(status_code=422, detail="to cannot be before from")
        num_days = (d_to - d_from).days + 1
        if num_days > 92:
            raise HTTPException(status_code=422, detail="range cannot exceed 92 days")

        start_dt = datetime.fromisoformat(from_date)
        end_dt = datetime.fromisoformat(to_date) + timedelta(days=1)

        pipeline = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "emp",
                }
            },
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department}},
            {
                "$project": {
                    "date": 1,
                    "date_dt": {"$dateFromString": {"dateString": "$date"}},
                    "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    "half_day": {"$ifNull": ["$half_day", False]},
                    "is_late": {"$gt": ["$late_minutes", 0]},
                }
            },
            {
                "$group": {
                    "_id": "$date_dt",
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                "$is_present",
                                {"$cond": ["$half_day", 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "late_count": {"$sum": {"$cond": ["$is_late", 1, 0]}},
                }
            },
            {
                "$densify": {
                    "field": "_id",
                    "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt]},
                }
            },
            {"$sort": {"_id": 1}},
            {
                "$project": {
                    "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
                    "is_working_day": {
                        "$in": [
                            {"$dayOfWeek": "$_id"},
                            [2, 3, 4, 5, 6],
                        ]
                    },
                    "present_count": {"$ifNull": ["$present_count", 0.0]},
                    "late_count": {"$ifNull": ["$late_count", 0]},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "let": {"current_date": "$date"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$department", department]},
                                        {"$lte": ["$joined_on", "$$current_date"]},
                                    ]
                                }
                            }
                        },
                        {"$count": "count"},
                    ],
                    "as": "emp_headcount",
                }
            },
            {
                "$project": {
                    "date": 1,
                    "is_working_day": 1,
                    "headcount": {"$ifNull": [{"$first": "$emp_headcount.count"}, 0]},
                    "present_count": 1,
                    "late_count": 1,
                }
            },
            {
                "$project": {
                    "date": 1,
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": {
                        "$cond": [
                            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                            {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                            None,
                        ]
                    },
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"date": 1},
                    "output": {
                        "moving_avg_7d": {
                            "$avg": "$attendance_rate",
                            "window": {"documents": [-6, "current"]},
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": 1,
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": 1,
                    "moving_avg_7d": {
                        "$cond": [
                            {"$ne": ["$moving_avg_7d", None]},
                            {"$round": ["$moving_avg_7d", 4]},
                            None,
                        ]
                    },
                }
            },
        ]
        raw_explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": jsonable_encoder(raw_explain),
        }
