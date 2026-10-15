"""
Employee Attendance & Analytics API (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is read, but real environment variables win)

Layout of this file:
  1. configuration and the Mongo client
  2. business rules R1-R5 as small pure functions (no I/O, easy to test)
  3. request models and conversion / error helpers
  4. index creation at startup
  5. employees and attendance endpoints
  6. analytics: pipeline builders + endpoints
  7. explain endpoint (re-uses the same builders as the real endpoints)
"""
import json
import logging
import os
import re
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from datetime import time as Time
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration
# --------------------------------------------------------------------------- #
load_dotenv()  # never overrides variables that are already set in the environment

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True: every datetime read from MongoDB is an aware UTC datetime, so naive and
# aware values can never get mixed by accident.
client = MongoClient(MONGO_URI, tz_aware=True, tzinfo=timezone.utc, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]


# --------------------------------------------------------------------------- #
# 2. Business rules (pure functions)
# --------------------------------------------------------------------------- #
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
ONE_SECOND = timedelta(seconds=1)
ONE_MS = timedelta(milliseconds=1)

MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000  # range the contract allows for instants
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")  # R6
GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_BELOW_HUNDREDTHS = 450  # R5: rounded work_hours < 4.50
MAX_SHIFT_SECONDS = 24 * 3600  # punch-out may be at most 24h after punch-in
MAX_TREND_DAYS = 92


def now_ms() -> int:
    return (datetime.now(UTC) - EPOCH) // ONE_MS


def ms_to_dt(ms: int) -> datetime:
    return EPOCH + timedelta(milliseconds=ms)


def dt_to_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:  # should not happen (client is tz_aware) but be safe
        value = value.replace(tzinfo=UTC)
    return (value - EPOCH) // ONE_MS


def instant_from_ms(ms: int) -> datetime:
    """Epoch ms -> UTC datetime truncated to whole seconds (R1)."""
    return ms_to_dt(ms - ms % 1000)


def floor_to_second(value: datetime) -> datetime:
    return value.replace(microsecond=0)


def parse_hhmm(text: str) -> Time:
    hour, minute = text.split(":")
    return Time(int(hour), int(minute))


def half_up_div(num: int, den: int) -> int:
    """num/den rounded half-up to an integer, using integers only (num >= 0, den > 0)."""
    return (2 * num + den) // (2 * den)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # "HH:MM" strings compare like times


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> Date:
    """R1: IST calendar date of the punch-in, moved back a day for the early part of an overnight shift."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < parse_hhmm(shift_end):
        day -= timedelta(days=1)
    return day


def shift_window(day: Date, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """Start and end instants of the shift that belongs to attendance date `day`."""
    start = datetime.combine(day, parse_hhmm(shift_start), tzinfo=IST)
    end = datetime.combine(day, parse_hhmm(shift_end), tzinfo=IST)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def late_minutes_for(punch_in: datetime, shift_start_at: datetime) -> int:
    """R2: strictly more than 10:00 after shift start; then whole minutes since shift start."""
    seconds = (punch_in - shift_start_at) // ONE_SECOND
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def overtime_minutes_for(punch_out: datetime, shift_end_at: datetime) -> int:
    """R3: whole minutes after shift end, only when that is at least 30."""
    minutes = ((punch_out - shift_end_at) // ONE_SECOND) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def work_hours_and_half_day(punch_in: datetime, punch_out: datetime) -> tuple[float, bool]:
    """R4 + R5. Rounds to hundredths half-up with integer maths, so no float surprises."""
    seconds = (punch_out - punch_in) // ONE_SECOND
    hundredths = half_up_div(seconds * 100, 3600)
    return hundredths / 100, hundredths < HALF_DAY_BELOW_HUNDREDTHS


def derived_fields(status, punch_in, punch_out, shift_start, shift_end, day: Date) -> dict:
    """All four stored derived fields for a record. One place, used by punch-in, punch-out and PATCH."""
    result = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE or punch_in is None:
        return result
    start_at, end_at = shift_window(day, shift_start, shift_end)
    result["late_minutes"] = late_minutes_for(punch_in, start_at)
    if punch_out is not None:
        result["work_hours"], result["half_day"] = work_hours_and_half_day(punch_in, punch_out)
        result["overtime_minutes"] = overtime_minutes_for(punch_out, end_at)
    return result


def count_weekdays(first: Date, last: Date) -> int:
    """Mon-Fri days in [first, last] (R7: no holiday calendar)."""
    count, day = 0, first
    while day <= last:
        if day.weekday() < 5:
            count += 1
        day += timedelta(days=1)
    return count


DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
_DATE_RE = re.compile(DATE_PATTERN)


def parse_date(text: str) -> Date:
    """Strict YYYY-MM-DD (date.fromisoformat on 3.11 also accepts '20260706', which we do not want)."""
    if not _DATE_RE.match(text):
        raise ValueError("not YYYY-MM-DD")
    return Date(int(text[0:4]), int(text[5:7]), int(text[8:10]))


# --------------------------------------------------------------------------- #
# 3. Models, conversion and error helpers
# --------------------------------------------------------------------------- #
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

# strict=True: floats (even 1.0), strings and bools are rejected with 422, as the contract asks
EpochMs = Annotated[int, Field(strict=True, ge=MIN_MS, le=MAX_MS)]
HHMM = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def joined_on_is_a_real_date(cls, value: str) -> str:
        try:
            parse_date(value)
        except ValueError:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")
        return value

    @model_validator(mode="after")
    def shifts_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeRequest(BaseModel):
    # The "= None" defaults are not validated by pydantic, so an omitted field is fine while an
    # explicit null is rejected (422). model_fields_set tells us what the client actually sent.
    status: Status = None  # type: ignore[assignment]
    punch_in: EpochMs = None  # type: ignore[assignment]
    punch_out: EpochMs = None  # type: ignore[assignment]
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


def unprocessable(field: str, message: str, where: str = "query") -> RequestValidationError:
    """A 422 with FastAPI's normal body shape, for checks that pydantic cannot express."""
    return RequestValidationError([{"type": "value_error", "loc": (where, field), "msg": message, "input": None}])


def query_date(value: Optional[str], name: str, where: str = "query") -> Optional[Date]:
    if value is None:
        return None
    try:
        return parse_date(value)
    except ValueError:
        raise unprocessable(name, "must be a valid YYYY-MM-DD date", where)


def month_bounds(month: str) -> tuple[Date, Date]:
    try:
        year, mon = int(month[:4]), int(month[5:7])
        return Date(year, mon, 1), Date(year, mon, monthrange(year, mon)[1])
    except ValueError:
        raise unprocessable("month", "must be a valid YYYY-MM month")


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc.get("joined_on"),
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def _history_value(value):
    return dt_to_ms(value) if isinstance(value, datetime) else value


def history_out(entry: dict) -> dict:
    changes = {
        field: {"from": _history_value(pair.get("from")), "to": _history_value(pair.get("to"))}
        for field, pair in (entry.get("changes") or {}).items()
    }
    return {"at": dt_to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def record_out(doc: dict) -> dict:
    """Stored attendance document -> API shape. Old records may lack late/overtime/half_day/history."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


def get_employee(emp_code: str, projection: Optional[dict] = None) -> dict:
    doc = db.employees.find_one({"emp_code": emp_code}, projection)
    if doc is None:
        raise HTTPException(404, f"employee {emp_code} not found")
    return doc


# --------------------------------------------------------------------------- #
# 4. Indexes (created at startup, idempotent) and app wiring
# --------------------------------------------------------------------------- #
INDEXES = [
    # collection, keys, options
    ("employees", [("emp_code", ASCENDING)], {"unique": True, "name": "uniq_emp_code"}),
    ("employees", [("department", ASCENDING), ("emp_code", ASCENDING)], {"name": "dept_emp_code"}),
    ("employees", [("joined_on", ASCENDING)], {"name": "joined_on"}),
    ("attendance_logs", [("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True, "name": "uniq_emp_date"}),
    ("attendance_logs", [("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "date_emp"}),
    ("attendance_logs", [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "status_date_emp"}),
    ("attendance_logs", [("emp_code", ASCENDING), ("punch_in", DESCENDING)], {"name": "emp_punch_in"}),
]


def ensure_indexes() -> None:
    for collection, keys, options in INDEXES:
        try:
            db[collection].create_index(keys, **options)
        except PyMongoError:
            # one bad index (e.g. dirty seed data under a unique index) must not stop the whole app
            log.exception("could not create index %s on %s", options.get("name"), collection)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except Exception:  # MongoDB not reachable yet: /health will say 503 until it is
        log.exception("startup index creation failed")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError):
    log.error("database error: %r", exc)
    return JSONResponse(status_code=503, content={"detail": "database error"})


# --------------------------------------------------------------------------- #
# 5a. System + employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not reachable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = ms_to_dt(now_ms())
    try:
        db.employees.insert_one(doc)  # the unique index is the real guard against duplicates
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


MAX_SKIP = 10**12  # guards against a skip value that would overflow BSON int64


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    flt = {} if department is None else {"department": department}
    skip = (page - 1) * page_size
    total = db.employees.count_documents(flt)
    items = []
    if skip <= MAX_SKIP:
        cursor = db.employees.find(flt, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
        items = [employee_out(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# 5b. Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = get_employee(body.emp_code)
    shift_start, shift_end = emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")

    at = instant_from_ms(body.punched_at if body.punched_at is not None else now_ms())
    day = attendance_date(at, shift_start, shift_end)

    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": at,
        "punch_out": None,
        **derived_fields(body.status, at, None, shift_start, shift_end, day),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one parallel request wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = get_employee(body.emp_code)
    shift_start, shift_end = emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")
    at = instant_from_ms(body.punched_at if body.punched_at is not None else now_ms())

    # most recent record whose punch_in is at or before the punch-out (works across midnight)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": at}}, sort=[("punch_in", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found for this employee")

    seconds = (at - floor_to_second(rec["punch_in"])) // ONE_SECOND
    if seconds <= 0:
        raise HTTPException(422, "punched_at must be after the punch-in")
    if seconds > MAX_SHIFT_SECONDS:
        raise HTTPException(422, "punch-out is more than 24 hours after the punch-in")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    day = parse_date(rec["date"])
    d = derived_fields(rec["status"], floor_to_second(rec["punch_in"]), at, shift_start, shift_end, day)

    # The filter repeats "punch_out is empty" so two simultaneous punch-outs cannot both succeed,
    # and "punch_in unchanged" so a concurrent correction cannot be overwritten with stale numbers.
    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": rec["emp_code"], "date": rec["date"], "punch_out": None, "punch_in": rec["punch_in"]},
        {"$set": {
            "punch_out": at,
            "work_hours": d["work_hours"],
            "overtime_minutes": d["overtime_minutes"],
            "half_day": d["half_day"],
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was already punched out or changed by someone else")
    return record_out(updated)


LIST_SORT = {"date": -1, "emp_code": 1}


def plan_attendance_list(emp_code, date_from, date_to, status, page, page_size) -> dict:
    lo, hi = query_date(date_from, "date_from"), query_date(date_to, "date_to")
    if lo and hi and lo > hi:
        raise unprocessable("date_from", "date_from must not be after date_to")
    flt: dict = {}
    if emp_code is not None:
        flt["emp_code"] = emp_code
    if lo or hi:
        flt["date"] = {}
        if lo:
            flt["date"]["$gte"] = lo.isoformat()
        if hi:
            flt["date"]["$lte"] = hi.isoformat()
    if status is not None:
        flt["status"] = status
    return {
        "collection": "attendance_logs",
        "filter": flt,
        "projection": {"_id": 0},
        "sort": LIST_SORT,
        "skip": (page - 1) * page_size,
        "limit": page_size,
    }


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    plan = plan_attendance_list(emp_code, date_from, date_to, status, page, page_size)
    coll = db[plan["collection"]]
    total = coll.count_documents(plan["filter"])
    items = []
    if plan["skip"] <= MAX_SKIP:
        cursor = (
            coll.find(plan["filter"], plan["projection"])
            .sort(list(plan["sort"].items()))
            .skip(plan["skip"])
            .limit(plan["limit"])
        )
        items = [record_out(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, body: RegularizeRequest, date: str = Path(..., pattern=DATE_PATTERN)):
    day = query_date(date, "date", where="path")
    emp = get_employee(emp_code)
    shift_start, shift_end = emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, f"no attendance record for {emp_code} on {date}")

    sent = body.model_fields_set
    old_status, old_in, old_out = rec["status"], rec.get("punch_in"), rec.get("punch_out")
    times_sent = "punch_in" in sent or "punch_out" in sent

    new_status = body.status if "status" in sent else old_status
    if new_status in PRESENCE:
        new_in = instant_from_ms(body.punch_in) if "punch_in" in sent else old_in
        new_out = instant_from_ms(body.punch_out) if "punch_out" in sent else old_out
        if new_in is None:
            raise HTTPException(422, "a presence status needs a punch_in")
        if "punch_in" in sent and attendance_date(new_in, shift_start, shift_end) != day:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None and times_sent:
            gap = (floor_to_second(new_out) - floor_to_second(new_in)) // ONE_SECOND
            if gap <= 0 or gap > MAX_SHIFT_SECONDS:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours of it")
    else:  # ABSENT / LEAVE clear both punch times
        if times_sent:
            raise HTTPException(422, f"{new_status} cannot be combined with punch_in or punch_out")
        new_in = new_out = None

    calc_in = floor_to_second(new_in) if new_in else None
    calc_out = floor_to_second(new_out) if new_out else None
    after = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        **derived_fields(new_status, calc_in, calc_out, shift_start, shift_end, day),
    }
    before = {
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    changes = {k: {"from": before[k], "to": after[k]} for k in after if before[k] != after[k]}
    if not changes:
        raise HTTPException(422, "the request changes nothing")

    entry = {"at": ms_to_dt(now_ms()), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency without adding a version field to the documents: the update only
    # applies if status, punch times and the NUMBER of history entries are still what we read.
    guard = {
        "emp_code": emp_code,
        "date": date,
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(rec.get("history") or [])]},
    }
    update = {"$set": dict(after)}
    if "history" in rec and rec["history"] is None:
        update["$set"]["history"] = [entry]  # a null history cannot be $push-ed to
    else:
        update["$push"] = {"history": entry}
    updated = db.attendance_logs.find_one_and_update(guard, update, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "the record was modified concurrently, please retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# 6. Analytics: pipeline builders
# --------------------------------------------------------------------------- #
def mongo_half_up_div(num, den) -> dict:
    """floor(num/den + 1/2) as a Mongo expression. $round would be half-to-even, which R8 forbids."""
    return {"$floor": {"$divide": [{"$add": [{"$multiply": [2, num]}, den]}, {"$multiply": [2, den]}]}}


def derive_stage() -> dict:
    """Adds helper booleans to each log. Analytics read the STORED derived values, nothing is recomputed."""
    day_of_week = {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}  # 1=Sun .. 7=Sat
    return {"$set": {
        "_present": {"$in": ["$status", list(PRESENCE)]},
        "_weekday": {"$in": [day_of_week, [2, 3, 4, 5, 6]]},
        "_halves": {"$cond": [{"$ifNull": ["$half_day", False]}, 1, 2]},  # a present day is 2 halves, a half day 1
        "_late": {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
    }}


def metrics_group() -> dict:
    """Accumulators shared by the employee monthly and department summary pipelines."""
    has_hours = {"$and": ["$_present", {"$isNumber": "$work_hours"}]}
    return {
        "present_halves": {"$sum": {"$cond": [{"$and": ["$_present", "$_weekday"]}, "$_halves", 0]}},
        "leave": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": ["$_late", 1, 0]}},
        "late_minutes": {"$sum": {"$cond": ["$_late", "$late_minutes", 0]}},
        "overtime": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        # work hours are summed as whole hundredths so the later mean can be rounded half-up exactly
        "hours_hundredths": {"$sum": {"$cond": [has_hours, {"$toLong": {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}}, 0]}},
        "hours_n": {"$sum": {"$cond": [has_hours, 1, 0]}},
    }


METRIC_KEYS = list(metrics_group().keys())


def plan_employee_monthly(emp_code: str, month: str) -> dict:
    first, last = month_bounds(month)
    return {
        "collection": "attendance_logs",
        "first": first,
        "last": last,
        "pipeline": [
            {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
            derive_stage(),
            {"$group": {"_id": None, **metrics_group()}},
        ],
    }


def plan_department_summary(month: str, department: Optional[str]) -> dict:
    first, last = month_bounds(month)
    match: dict = {"joined_on": {"$lte": last.isoformat()}}  # R9
    if department is not None:
        match["department"] = department
    return {
        "collection": "employees",
        "pipeline": [
            {"$match": match},
            # left join: an employee without logs gets m = [] and is still counted below
            {"$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                    derive_stage(),
                    {"$group": {"_id": None, **metrics_group()}},
                ],
                "as": "m",
            }},
            {"$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                **{k: {"$sum": {"$ifNull": [{"$arrayElemAt": [f"$m.{k}", 0]}, 0]}} for k in METRIC_KEYS},
            }},
            {"$sort": {"_id": 1}},
        ],
    }


def plan_late_leaderboard(month: str, limit: int, department: Optional[str]) -> dict:
    first, last = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        # inner join: logs whose employee does not exist disappear here
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking (1, 2, 2, 4); the array position is NOT the rank
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit is applied after ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]
    return {"collection": "attendance_logs", "pipeline": pipeline}


def plan_department_trend(department: str, date_from: str, date_to: str) -> dict:
    first, last = query_date(date_from, "from"), query_date(date_to, "to")
    if last < first:
        raise unprocessable("to", "to must not be before from")
    n_days = (last - first).days + 1
    if n_days > MAX_TREND_DAYS:
        raise unprocessable("to", f"the range may not exceed {MAX_TREND_DAYS} days")
    first_dt = datetime(first.year, first.month, first.day, tzinfo=UTC)
    window = {"documents": [-6, 0]}  # current row + up to 6 before it, never earlier than `from`

    per_day_logs = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$set": {
            "_present": {"$in": ["$status", list(PRESENCE)]},
            "_halves": {"$cond": [{"$ifNull": ["$half_day", False]}, 1, 2]},
            "_late": {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
        }},
        {"$group": {
            "_id": "$date",
            "present_halves": {"$sum": {"$cond": ["$_present", "$_halves", 0]}},
            "late_count": {"$sum": {"$cond": ["$_late", 1, 0]}},
        }},
    ]
    return {
        "collection": "employees",
        "n_days": n_days,
        "pipeline": [
            {"$match": {"department": department}},
            {"$group": {"_id": None, "codes": {"$push": "$emp_code"}, "joins": {"$push": "$joined_on"}}},
            # one lookup for the whole department; an array localField matches any of its elements
            {"$lookup": {"from": "attendance_logs", "localField": "codes", "foreignField": "emp_code",
                         "pipeline": per_day_logs, "as": "per_day"}},
            # the day axis is generated inside the database: 0..n-1 days added to `from`
            {"$project": {"_id": 0, "joins": 1, "per_day": 1, "days": {"$map": {
                "input": {"$range": [0, n_days]},
                "as": "i",
                "in": {"$dateToString": {"format": "%Y-%m-%d", "date": {
                    "$add": [first_dt, {"$multiply": ["$$i", 86_400_000]}]}}},
            }}}},
            {"$unwind": "$days"},
            {"$set": {
                "date": "$days",
                "_dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$days", "format": "%Y-%m-%d"}}},
                "headcount": {"$size": {"$filter": {"input": "$joins", "as": "j", "cond": {"$lte": ["$$j", "$days"]}}}},
                "_agg": {"$arrayElemAt": [
                    {"$filter": {"input": "$per_day", "as": "p", "cond": {"$eq": ["$$p._id", "$days"]}}}, 0]},
            }},
            {"$set": {
                "is_working_day": {"$in": ["$_dow", [2, 3, 4, 5, 6]]},
                "present_halves": {"$ifNull": ["$_agg.present_halves", 0]},
                "late_count": {"$ifNull": ["$_agg.late_count", 0]},
            }},
            {"$set": {"_rated": {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}}},
            {"$set": {
                # rate in ten-thousandths, rounded half-up with integer maths
                "q": {"$cond": ["$_rated",
                                mongo_half_up_div({"$multiply": ["$present_halves", 10000]}, {"$multiply": [2, "$headcount"]}),
                                None]},
                "n_q": {"$cond": ["$_rated", 1, 0]},
            }},
            {"$setWindowFields": {"sortBy": {"date": 1}, "output": {
                "sum_q": {"$sum": "$q", "window": window},
                "cnt_q": {"$sum": "$n_q", "window": window},
            }}},
            {"$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": {"$divide": ["$present_halves", 2]},
                "late_count": 1,
                "attendance_rate": {"$cond": ["$_rated", {"$divide": ["$q", 10000]}, None]},
                "moving_avg_7d": {"$cond": [{"$gt": ["$cnt_q", 0]},
                                            {"$divide": [mongo_half_up_div("$sum_q", "$cnt_q"), 10000]},
                                            None]},
            }},
            {"$sort": {"date": 1}},
        ],
    }


# --------------------------------------------------------------------------- #
# 6b. Analytics: endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    plan = plan_employee_monthly(emp_code, month)
    emp = get_employee(emp_code, {"joined_on": 1})
    rows = list(db[plan["collection"]].aggregate(plan["pipeline"]))
    m = rows[0] if rows else {}

    working_days = count_weekdays(max(plan["first"], parse_date(emp["joined_on"])), plan["last"])
    halves = m.get("present_halves", 0)
    pct = None if working_days == 0 else half_up_div(5000 * halves, working_days) / 100  # halves/2/wd*100, 2dp
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": halves / 2,
        "leave_days": m.get("leave", 0),
        "late_count": m.get("late_count", 0),
        "total_late_minutes": m.get("late_minutes", 0),
        "total_overtime_minutes": m.get("overtime", 0),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    plan = plan_department_summary(month, department)
    items = []
    for row in db[plan["collection"]].aggregate(plan["pipeline"]):
        n = row["hours_n"]
        items.append({
            "department": row["_id"],
            "headcount": row["headcount"],
            "present_days": row["present_halves"] / 2,
            # mean over RECORDS (sum of all hours / number of records), not a mean of per-employee means
            "avg_work_hours": None if n == 0 else half_up_div(row["hours_hundredths"], n) / 100,
            "late_count": row["late_count"],
            "total_late_minutes": row["late_minutes"],
            "leave_count": row["leave"],
            "on_duty_count": row["on_duty"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    plan = plan_late_leaderboard(month, limit, department)
    return {"month": month, "items": list(db[plan["collection"]].aggregate(plan["pipeline"]))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    plan = plan_department_trend(department, from_, to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, f"unknown department {department}")
    return {"department": department, "items": list(db[plan["collection"]].aggregate(plan["pipeline"]))}


# --------------------------------------------------------------------------- #
# 7. Explain endpoint
# --------------------------------------------------------------------------- #
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def require(**params) -> None:
    for name, value in params.items():
        if value is None:
            raise unprocessable(name, "this parameter is required for this endpoint")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    # Each plan comes from the same builder the real endpoint calls, so the explained query
    # is the one that really runs (same filter, sort, skip, limit / same pipeline).
    if endpoint == "attendance_list":
        plan = plan_attendance_list(emp_code, date_from, date_to, status, page, page_size)
        inner = {"find": plan["collection"], "filter": plan["filter"], "projection": plan["projection"],
                 "sort": plan["sort"], "skip": plan["skip"], "limit": plan["limit"]}
    else:
        if endpoint == "employee_monthly":
            require(emp_code=emp_code, month=month)
            plan = plan_employee_monthly(emp_code, month)
        elif endpoint == "department_summary":
            require(month=month)
            plan = plan_department_summary(month, department)
        elif endpoint == "late_leaderboard":
            require(month=month)
            plan = plan_late_leaderboard(month, limit, department)
        else:
            require(department=department, **{"from": from_, "to": to})
            plan = plan_department_trend(department, from_, to)
        inner = {"aggregate": plan["collection"], "pipeline": plan["pipeline"], "cursor": {}}

    raw = db.command({"explain": inner, "verbosity": "executionStats"})
    # explain output can hold BSON types (Int64, dates...); round-trip through extended JSON
    return {"endpoint": endpoint, "collection": plan["collection"], "explain": json.loads(json_util.dumps(raw))}
