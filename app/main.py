"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

The file is organised top to bottom as:
  1. Configuration, database connection, indexes
  2. Time helpers and the business rules R1-R5
  3. BSON <-> API conversion
  4. Request models (validation)
  5. Endpoints: health, employees, attendance
  6. Analytics: pipeline builders (shared with /admin/explain) and endpoints
  7. Explain endpoint

Conventions (see openapi.yaml / DATA_MODEL.md):
  * Instants are BSON dates (UTC) in MongoDB and integer epoch milliseconds in the API.
  * Dates are "YYYY-MM-DD" IST strings, shift times "HH:MM" IST strings, in both places.
  * `_id` never leaves this file. Employees are addressed by emp_code, records by (emp_code, date).
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import Decimal128, json_util
from bson.json_util import RELAXED_JSON_OPTIONS
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, Strict, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure, DuplicateKeyError, PyMongoError

log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration, database connection, indexes
# --------------------------------------------------------------------------- #
load_dotenv(override=False)  # never overrides variables that are already set

MONGO_URI = os.environ.get("MONGO_URI")
MONGO_DB = os.environ.get("MONGO_DB")
if not MONGO_URI or not MONGO_DB:
    raise RuntimeError("MONGO_URI and MONGO_DB must be set (environment variables or a local .env)")

# tz_aware=True: PyMongo returns timezone-aware UTC datetimes instead of naive ones, so arithmetic with
# IST shift boundaries is always aware-vs-aware and never silently uses the server's local time zone.
client = MongoClient(MONGO_URI, tz_aware=True, tzinfo=timezone.utc, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]
employees = db["employees"]
logs = db["attendance_logs"]

# (collection, keys, options). create_index is idempotent, so running this on every start is safe.
INDEXES = [
    # Uniqueness of the business key; also serves every lookup by emp_code (punch-in, $lookup from logs).
    ("employees", [("emp_code", 1)], {"unique": True}),
    # GET /employees?department=..: equality on department, already sorted by emp_code. Also the trend's $match.
    ("employees", [("department", 1), ("emp_code", 1)], {}),
    # Department summary starts from "employees who joined on or before the end of the month".
    ("employees", [("joined_on", 1)], {}),
    # One record per (emp_code, date): makes punch-in race-safe; also serves every per-employee date range
    # (list filter, monthly summary, the $lookup from employees in the summary and trend).
    ("attendance_logs", [("emp_code", 1), ("date", 1)], {"unique": True}),
    # GET /attendance sort (date desc, emp_code asc) and the month-wide range scan of the leaderboard.
    ("attendance_logs", [("date", -1), ("emp_code", 1)], {}),
    # Punch-out: "most recent record of this employee whose punch_in <= punched_at".
    ("attendance_logs", [("emp_code", 1), ("punch_in", -1)], {}),
    # GET /attendance?status=..: equality on status, already in the endpoint's sort order (and its count).
    ("attendance_logs", [("status", 1), ("date", -1), ("emp_code", 1)], {}),
]
_indexes_ready = False


def ensure_indexes() -> None:
    global _indexes_ready
    if _indexes_ready:
        return
    for coll, keys, opts in INDEXES:
        db[coll].create_index(keys, **opts)
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # MongoDB not reachable yet: /health retries and reports 503 meanwhile
        log.warning("could not create indexes at startup: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(ConnectionFailure)
async def _db_unavailable(_request: Request, _exc: ConnectionFailure):
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 2. Time helpers and business rules
# --------------------------------------------------------------------------- #
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MS_MIN, MS_MAX = 100_000_000_000, 4_102_444_800_000

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = PRESENCE + ("ABSENT", "LEAVE")
StatusT = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceT = Literal["PRESENT", "WFH", "ON_DUTY"]

GRACE_SECONDS = 10 * 60          # R2
MIN_OVERTIME_MINUTES = 30        # R3
HALF_DAY_BELOW_HOURS = 4.50      # R5
MAX_SHIFT = timedelta(hours=24)  # punch-out must be within 24 h of punch-in
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def as_utc(value: datetime) -> datetime:
    """Treat a naive datetime as UTC (that is what BSON dates are); convert aware ones to UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def to_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return (as_utc(value) - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def truncate(value: datetime) -> datetime:
    return as_utc(value).replace(microsecond=0)


def now_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def now_millis() -> datetime:
    """Current time at BSON precision, so the stored value and the returned value are identical."""
    now = datetime.now(UTC)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def parse_hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def is_overnight(emp: dict) -> bool:
    return parse_hhmm(emp["shift_end"]) <= parse_hhmm(emp["shift_start"])


def attendance_date(punch_in: datetime, emp: dict) -> date:
    """R1: the IST calendar date of the punch-in; for an overnight shift, a punch-in earlier than
    shift_end belongs to the shift that started the previous day."""
    local = as_utc(punch_in).astimezone(IST)
    if is_overnight(emp) and local.time() < parse_hhmm(emp["shift_end"]):
        return local.date() - timedelta(days=1)
    return local.date()


def shift_bounds(day: date, emp: dict) -> tuple[datetime, datetime]:
    """Start and end instants of the shift that starts on `day` (IST). Overnight shifts end the next day."""
    start = datetime.combine(day, parse_hhmm(emp["shift_start"]), IST)
    end = datetime.combine(day, parse_hhmm(emp["shift_end"]), IST)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def round_half_up(value: Any, places: int) -> float:
    """R8: decimal half-up rounding. Python's round() is banker's rounding on binary floats."""
    exact = value if isinstance(value, Decimal) else Decimal(str(value))
    return float(exact.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def compute_late_minutes(punch_in: datetime, day: date, emp: dict) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor minutes since shift start."""
    start, _ = shift_bounds(day, emp)
    seconds = (truncate(punch_in) - start) // timedelta(seconds=1)
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (out - in) seconds / 3600, 2 decimals half-up."""
    seconds = (truncate(punch_out) - truncate(punch_in)) // timedelta(seconds=1)
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def compute_overtime(punch_out: datetime, day: date, emp: dict) -> int:
    """R3: floor minutes from shift end to punch-out, only when >= 30."""
    _, end = shift_bounds(day, emp)
    minutes = ((truncate(punch_out) - end) // timedelta(seconds=1)) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def derive(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], day: date, emp: dict) -> dict:
    """The four derived fields of a record (DATA_MODEL.md section 3, 'For a record your own code writes')."""
    if status not in PRESENCE:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, day, emp)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, day, emp),
        "half_day": hours < HALF_DAY_BELOW_HOURS,  # R5 uses the rounded value
    }


# --------------------------------------------------------------------------- #
# 3. BSON <-> API conversion
# --------------------------------------------------------------------------- #
def to_api(value: Any) -> Any:
    """Recursively turn datetimes into epoch ms (used for history[].changes, where dates are nested)."""
    if isinstance(value, datetime):
        return to_ms(value)
    if isinstance(value, dict):
        return {k: to_api(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_api(v) for v in value]
    return value


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": to_ms(doc.get("created_at")),
    }


def stored_state(doc: dict) -> dict:
    """The correctable and derived fields of a stored record, with the documented defaults for missing ones."""
    return {
        "status": doc.get("status"),
        "punch_in": doc.get("punch_in"),
        "punch_out": doc.get("punch_out"),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day") or False),
    }


def record_out(doc: dict) -> dict:
    state = stored_state(doc)
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        **state,
        "punch_in": to_ms(state["punch_in"]),
        "punch_out": to_ms(state["punch_out"]),
        "history": [
            {"at": to_ms(h.get("at")), "by": h.get("by"), "reason": h.get("reason"),
             "changes": to_api(h.get("changes") or {})}
            for h in (doc.get("history") or [])
        ],
    }


def unprocessable(loc: list, msg: str, value: Any = None):
    """422 in FastAPI's validation-error shape ({"detail": [{loc, msg, type}]})."""
    raise HTTPException(422, [{"type": "value_error", "loc": loc, "msg": msg, "input": value}])


def parse_day(value: str, loc: list) -> date:
    if not DATE_RE.fullmatch(value):
        unprocessable(loc, "expected a date YYYY-MM-DD", value)
    try:
        return date.fromisoformat(value)
    except ValueError:
        unprocessable(loc, "not a valid calendar date", value)


def month_bounds(month: str) -> tuple[date, date]:
    year, mon = int(month[:4]), int(month[5:7])
    if year < 1:
        unprocessable(["query", "month"], "year out of range", month)
    return date(year, mon, 1), date(year, mon, calendar.monthrange(year, mon)[1])


def get_employee_or_404(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 4. Request models
# --------------------------------------------------------------------------- #
def _check_date(value: str) -> str:
    if not DATE_RE.fullmatch(value):
        raise ValueError("expected a date YYYY-MM-DD")
    date.fromisoformat(value)  # rejects 2026-02-30
    return value


# Strict: floats (1.78e12), numeric strings and booleans are rejected, not coerced. ge: rejects seconds.
EpochMillis = Annotated[int, Strict(), Field(ge=MS_MIN, le=MS_MAX)]
HHMM = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
DateStr = Annotated[str, AfterValidator(_check_date)]

Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"


class EmployeeCreate(BaseModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP[0-9]{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def _shift_not_empty(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceT = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    # Derived fields are not declared, so a client that sends them is ignored (pydantic drops extras).
    status: Optional[StatusT] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def _no_explicit_nulls(self):
        # Omitted means "unchanged"; the contract does not allow null for these fields.
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} may be omitted but not null")
        return self


# --------------------------------------------------------------------------- #
# 5. Endpoints: health, employees, attendance
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = now_millis()
    try:
        employees.insert_one(doc)  # the unique index decides; no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    query = {} if department is None else {"department": department}
    total = employees.count_documents(query)
    cursor = employees.find(query).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = get_employee_or_404(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_seconds()
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, day, emp),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        logs.insert_one(doc)  # unique (emp_code, date): concurrent duplicates fail here, atomically
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = get_employee_or_404(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_seconds()
    record = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)])
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    started = truncate(record["punch_in"])
    if ts <= started:
        unprocessable(["body", "punched_at"], "punch-out must be after punch-in", body.punched_at)
    if ts - started > MAX_SHIFT:
        unprocessable(["body", "punched_at"], "punch-out must be within 24 hours of punch-in", body.punched_at)

    derived = derive(record["status"], started, ts, date.fromisoformat(record["date"]), emp)
    # late_minutes was fixed at punch-in and is left as stored.
    update = {"punch_out": ts, "work_hours": derived["work_hours"],
              "overtime_minutes": derived["overtime_minutes"], "half_day": derived["half_day"]}
    # The filter on punch_out: null makes the close atomic: of two concurrent punch-outs only one matches.
    updated = logs.find_one_and_update({"_id": record["_id"], "punch_out": None}, {"$set": update},
                                       return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


ATTENDANCE_SORT = [("date", -1), ("emp_code", 1)]


def attendance_list_query(emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str],
                          status: Optional[str]) -> dict:
    if date_from is not None:
        parse_day(date_from, ["query", "date_from"])
    if date_to is not None:
        parse_day(date_to, ["query", "date_to"])
    if date_from is not None and date_to is not None and date_from > date_to:
        unprocessable(["query", "date_from"], "date_from must not be after date_to", date_from)
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    return query


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None,
                    date_to: Optional[str] = None, status: Optional[StatusT] = None,
                    page: Page = 1, page_size: PageSize = 20):
    query = attendance_list_query(emp_code, date_from, date_to, status)
    total = logs.count_documents(query) if query else logs.estimated_document_count()
    cursor = logs.find(query).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    day = parse_day(date, ["path", "date"])
    emp = get_employee_or_404(emp_code)
    record = logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "no attendance record for that date")

    old = stored_state(record)
    sent = body.model_fields_set
    status = body.status if "status" in sent else old["status"]
    if status in PRESENCE:
        new_in = from_ms(body.punch_in) if "punch_in" in sent else old["punch_in"]
        new_out = from_ms(body.punch_out) if "punch_out" in sent else old["punch_out"]
        if new_in is None:
            unprocessable(["body", "punch_in"], f"status {status} requires a punch_in")
        if "punch_in" in sent and attendance_date(new_in, emp) != day:
            unprocessable(["body", "punch_in"], "punch_in must stay on the record's attendance date", body.punch_in)
        if new_out is not None and not (truncate(new_in) < truncate(new_out) <= truncate(new_in) + MAX_SHIFT):
            unprocessable(["body", "punch_out"], "punch_out must be after punch_in and within 24 hours", body.punch_out)
    else:
        if "punch_in" in sent or "punch_out" in sent:
            unprocessable(["body"], f"status {status} cannot have punch times")
        new_in = new_out = None

    requested = {"status": status, "punch_in": new_in, "punch_out": new_out}
    if not any(requested[f] != old[f] for f in ("status", "punch_in", "punch_out") if f in sent):
        unprocessable(["body"], "the request changes nothing")

    new = {**requested, **derive(status, new_in and truncate(new_in), new_out and truncate(new_out), day, emp)}
    changes = {f: {"from": old[f], "to": new[f]} for f in new if new[f] != old[f]}
    entry = {"at": now_millis(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: only apply if nobody changed the record since we read it (same punch times,
    # same status, same history length). The loser of a race gets 409 instead of silently losing an entry.
    guard = {
        "_id": record["_id"],
        "status": record.get("status"),
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(record.get("history") or [])]},
    }
    updated = logs.find_one_and_update(guard, {"$set": new, "$push": {"history": entry}},
                                       return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "the record was modified concurrently; retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# 6. Analytics
#
# Each builder returns (collection, pipeline) and is used both by its endpoint and by /admin/explain, so the
# explained plan is the plan the endpoint really runs. All counting happens in MongoDB; Python only applies
# the final R8 rounding to a handful of scalars.
# --------------------------------------------------------------------------- #
IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}
IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]}
PRESENT_VALUE = {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}                  # R5: half day = 0.5
PRESENT_DAYS = {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]}, PRESENT_VALUE, 0]}  # R6 + R7
IS_LATE = {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}
HAS_WORK_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}


def count_if(condition: dict) -> dict:
    return {"$sum": {"$cond": [condition, 1, 0]}}


def half_up_4(expr: Any) -> dict:
    """Half-up to 4 decimals inside MongoDB ($round is half-to-even). Exact because expr is a Decimal128."""
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, 10000]}, Decimal128("0.5")]}}, 10000]}


def employee_monthly_pipeline(emp_code: str, first: date, last: date):
    return "attendance_logs", [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": PRESENT_DAYS},
            "leave_days": count_if({"$eq": ["$status", "LEAVE"]}),
            "late_count": count_if(IS_LATE),
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


# The cross-collection pipelines below share one pattern. A $lookup per document costs one query per
# document (hundreds per request), so instead the keys are first collected into a single array and joined
# with ONE $lookup whose localField is that array (MongoDB turns it into an indexed `emp_code: {$in: [...]}`
# query). The two sides are then concatenated, unwound and merged with $group on emp_code.
def merge_rows(left: str, right: str) -> list:
    return [
        {"$project": {"_id": 0, "rows": {"$concatArrays": [f"${left}", f"${right}"]}}},
        {"$unwind": "$rows"},
        {"$replaceRoot": {"newRoot": "$rows"}},
    ]


def department_summary_pipeline(first: date, last: date, department: Optional[str]):
    # Start from employees, not logs: every employee who has joined by the end of the month contributes a row
    # with headcount 1, whether or not they have logs (R9). Their log statistics are joined onto that row.
    match: dict = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        match["department"] = department
    return "employees", [
        {"$match": match},
        {"$group": {"_id": None, "codes": {"$push": "$emp_code"},
                    "people": {"$push": {"emp_code": "$emp_code", "department": "$department", "headcount": 1}}}},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "codes",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                {"$group": {
                    "_id": "$emp_code",
                    "present_days": {"$sum": PRESENT_DAYS},
                    # Summed as Decimal128 so the department average is exact before R8 rounding.
                    "work_hours_sum": {"$sum": {"$cond": [HAS_WORK_HOURS, {"$toDecimal": "$work_hours"}, 0]}},
                    "work_hours_n": count_if(HAS_WORK_HOURS),
                    "late_count": count_if(IS_LATE),
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "leave_count": count_if({"$eq": ["$status", "LEAVE"]}),
                    "on_duty_count": count_if({"$eq": ["$status", "ON_DUTY"]}),
                }},
                {"$set": {"emp_code": "$_id"}},
            ],
            "as": "stats",
        }},
        *merge_rows("people", "stats"),
        {"$group": {  # one row per employee: department + headcount from `people`, numbers from `stats`
            "_id": "$emp_code",
            "department": {"$max": "$department"},
            "headcount": {"$sum": "$headcount"},
            "present_days": {"$sum": "$present_days"},
            "work_hours_sum": {"$sum": "$work_hours_sum"},
            "work_hours_n": {"$sum": "$work_hours_n"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": "$headcount"},
            "present_days": {"$sum": "$present_days"},
            "work_hours_sum": {"$sum": "$work_hours_sum"},
            "work_hours_n": {"$sum": "$work_hours_n"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(first: date, last: date, limit: int, department: Optional[str]):
    employee_filter = [] if department is None else [{"$match": {"department": department}}]
    return "attendance_logs", [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$group": {"_id": None, "codes": {"$push": "$_id"},
                    "totals": {"$push": {"emp_code": "$_id", "total_late_minutes": "$total_late_minutes",
                                         "late_count": "$late_count"}}}},
        {"$lookup": {
            "from": "employees",
            "localField": "codes",
            "foreignField": "emp_code",
            "pipeline": [*employee_filter,  # with a department, ranking happens inside that department only
                         {"$project": {"_id": 0, "emp_code": 1, "name": 1, "department": 1, "found": {"$literal": True}}}],
            "as": "people",
        }},
        *merge_rows("totals", "people"),
        {"$group": {"_id": "$emp_code", "found": {"$max": "$found"}, "name": {"$max": "$name"},
                    "department": {"$max": "$department"}, "total_late_minutes": {"$sum": "$total_late_minutes"},
                    "late_count": {"$sum": "$late_count"}}},
        {"$match": {"found": True}},  # drops logs whose employee does not exist (or is in another department)
        # Standard competition ranking (1, 2, 2, 4); the limit is then applied to the rank, not to the row count.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": "$rank", "emp_code": "$_id", "name": "$name", "department": "$department",
                      "total_late_minutes": "$total_late_minutes", "late_count": "$late_count"}},
    ]


def department_trend_pipeline(department: str, first: date, last: date):
    lo, hi = first.isoformat(), last.isoformat()
    start = datetime.combine(first, time(), UTC)
    stop = datetime.combine(last + timedelta(days=1), time(), UTC)  # $densify upper bound is exclusive
    # Each employee contributes a "join" row on max(joined_on, from): a running sum of those rows over the days
    # is the headcount of each day (R9). The department's logs, already totalled per day, are joined in with
    # one $lookup (see merge_rows above).
    return "employees", [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "codes": {"$push": "$emp_code"},
                    "joins": {"$push": {"date": {"$max": ["$joined_on", lo]}, "joins": 1}}}},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "codes",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": lo, "$lte": hi}}},
                {"$group": {"_id": "$date", "present": {"$sum": {"$cond": [IS_PRESENT, PRESENT_VALUE, 0]}},
                            "late": count_if(IS_LATE)}},
                {"$set": {"date": "$_id"}},
            ],
            "as": "days",
        }},
        *merge_rows("joins", "days"),
        {"$group": {"_id": "$date", "joins": {"$sum": "$joins"}, "present": {"$sum": "$present"},
                    "late": {"$sum": "$late"}}},
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d", "timezone": "UTC"}}}},
        # Gap filling: exactly one document per calendar day in [from, to].
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start, stop]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "headcount": {"$sum": "$joins", "window": {"documents": ["unbounded", "current"]}}}}},
        {"$match": {"day": {"$gte": start, "$lt": stop}}},  # drop join events of people joining after `to`
        {"$set": {"is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
                  "present_count": {"$ifNull": ["$present", 0]},
                  "late_count": {"$ifNull": ["$late", 0]}}},
        {"$set": {"rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            half_up_4({"$divide": [{"$toDecimal": "$present_count"}, {"$toDecimal": "$headcount"}]}),
            None,
        ]}}},
        # Current row and up to 6 preceding rows; only rows inside the range exist here, so early rows get a
        # shorter window. $avg ignores nulls and returns null if the window has no number.
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "moving_avg": {"$avg": "$rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"day": 1}},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
            "is_working_day": "$is_working_day",
            "headcount": "$headcount",
            "present_count": "$present_count",
            "late_count": "$late_count",
            "attendance_rate": {"$toDouble": "$rate"},
            "moving_avg_7d": {"$toDouble": half_up_4("$moving_avg")},
        }},
    ]


def count_weekdays(first: date, last: date) -> int:
    if first > last:
        return 0
    weeks, rest = divmod((last - first).days + 1, 7)
    return weeks * 5 + sum(1 for i in range(rest) if (first + timedelta(days=i)).weekday() < 5)


def decimal_of(value: Any) -> Decimal:
    return value.to_decimal() if isinstance(value, Decimal128) else Decimal(str(value))


def number(value: Any) -> float | int:
    """present_days style values: 3 stays 3, 3.5 stays 3.5."""
    value = float(value or 0)
    return int(value) if value.is_integer() else value


def trend_range(from_: str, to: str) -> tuple[date, date]:
    first, last = parse_day(from_, ["query", "from"]), parse_day(to, ["query", "to"])
    if last < first:
        unprocessable(["query", "to"], "to must not be before from", to)
    if (last - first).days + 1 > 92:
        unprocessable(["query", "to"], "the range may not exceed 92 days", to)
    return first, last


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Annotated[str, Query(pattern=MONTH_PATTERN)]):
    first, last = month_bounds(month)
    emp = get_employee_or_404(emp_code)
    coll, pipeline = employee_monthly_pipeline(emp_code, first, last)
    stats = next(db[coll].aggregate(pipeline), {})

    joined = emp.get("joined_on")
    working_days = count_weekdays(max(first, date.fromisoformat(joined)) if joined else first, last)  # R7
    present = Decimal(str(stats.get("present_days", 0)))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": number(present),
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": int(stats.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0)),
        "attendance_pct": round_half_up(present * 100 / working_days, 2) if working_days else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: Annotated[str, Query(pattern=MONTH_PATTERN)], department: Optional[str] = None):
    first, last = month_bounds(month)
    coll, pipeline = department_summary_pipeline(first, last, department)
    items = []
    for row in db[coll].aggregate(pipeline):
        n = row["work_hours_n"]
        items.append({
            "department": row["_id"],
            "headcount": row["headcount"],
            "present_days": number(row["present_days"]),
            "avg_work_hours": round_half_up(decimal_of(row["work_hours_sum"]) / n, 2) if n else None,
            "late_count": row["late_count"],
            "total_late_minutes": int(row["total_late_minutes"]),
            "leave_count": row["leave_count"],
            "on_duty_count": row["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Annotated[str, Query(pattern=MONTH_PATTERN)],
                     limit: Annotated[int, Query(ge=1, le=50)] = 10, department: Optional[str] = None):
    first, last = month_bounds(month)
    coll, pipeline = late_leaderboard_pipeline(first, last, limit, department)
    return {"month": month, "items": list(db[coll].aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: Annotated[str, Query(alias="from")], to: str):
    first, last = trend_range(from_, to)
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    coll, pipeline = department_trend_pipeline(department, first, last)
    return {"department": department, "items": list(db[coll].aggregate(pipeline))}


# --------------------------------------------------------------------------- #
# 7. Explain
# --------------------------------------------------------------------------- #
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard",
                        "department_trend"]


def require(value: Optional[str], name: str) -> str:
    if value is None:
        raise HTTPException(422, [{"type": "missing", "loc": ["query", name], "msg": "Field required",
                                   "input": None}])
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=MONTH_PATTERN)] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusT] = None,
    from_: Annotated[Optional[str], Query(alias="from")] = None,
    to: Optional[str] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    if endpoint == "attendance_list":
        coll = "attendance_logs"
        command = {"find": coll, "filter": attendance_list_query(emp_code, date_from, date_to, status),
                   "sort": dict(ATTENDANCE_SORT), "skip": (page - 1) * page_size, "limit": page_size}
    else:
        if endpoint == "employee_monthly":
            emp_code = require(emp_code, "emp_code")
            coll, pipeline = employee_monthly_pipeline(emp_code, *month_bounds(require(month, "month")))
        elif endpoint == "department_summary":
            coll, pipeline = department_summary_pipeline(*month_bounds(require(month, "month")), department)
        elif endpoint == "late_leaderboard":
            first, last = month_bounds(require(month, "month"))
            coll, pipeline = late_leaderboard_pipeline(first, last, limit, department)
        else:
            department = require(department, "department")
            first, last = trend_range(require(from_, "from"), require(to, "to"))
            coll, pipeline = department_trend_pipeline(department, first, last)
        command = {"aggregate": coll, "pipeline": pipeline, "cursor": {}}

    raw = db.command({"explain": command, "verbosity": "executionStats"})
    plain = json.loads(json_util.dumps(raw, json_options=RELAXED_JSON_OPTIONS))  # Timestamps etc. -> JSON
    return {"endpoint": endpoint, "collection": coll, "explain": plain}
