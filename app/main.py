"""
Employee Attendance & Analytics API - Production Implementation
Single-file service satisfying OpenAPI 3.0.3 contract and business rules R1-R10.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import enum
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple, Union

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# Environment & Configuration
# --------------------------------------------------------------------------- #
# Real environment variables take precedence; load_dotenv must not override them.
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# Timezone constant for IST (UTC+05:30)
IST = timezone(timedelta(hours=5, minutes=30))

# PyMongo client initialized with tz_aware=True and a short selection timeout.
client: MongoClient = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=2000)
db = client[MONGO_DB]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Database Indexes Setup
# --------------------------------------------------------------------------- #
def init_db_indexes() -> None:
    """Create all required indexes idempotently without raising if Mongo is briefly unavailable."""
    try:
        # employees indexes
        db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="idx_emp_code_unique")
        db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="idx_dept_emp_code")
        db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="idx_joined_dept")
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="idx_dept_joined")

        # attendance_logs indexes
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="idx_emp_date_unique"
        )
        db.attendance_logs.create_index(
            [("date", DESCENDING), ("emp_code", ASCENDING)], name="idx_date_desc_emp_asc"
        )
        db.attendance_logs.create_index(
            [("date", ASCENDING), ("emp_code", ASCENDING)], name="idx_date_asc_emp_asc"
        )
        db.attendance_logs.create_index(
            [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="idx_status_date_emp"
        )
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="idx_emp_punch_in"
        )
        db.attendance_logs.create_index(
            [("date", ASCENDING), ("late_minutes", ASCENDING)], name="idx_date_late_minutes"
        )
    except PyMongoError:
        # Non-fatal during early startup; /health probe will signal 503 until Mongo is ready
        pass


@app.on_event("startup")
def on_startup() -> None:
    init_db_indexes()


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class Status(str, enum.Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, enum.Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class ExplainEndpoint(str, enum.Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"


# --------------------------------------------------------------------------- #
# Math, Rounding & Time Helpers
# --------------------------------------------------------------------------- #
def round_half_up(val: Union[float, Decimal, int], decimals: int) -> float:
    """Exact half-up rounding (R8) using Decimal(ROUND_HALF_UP)."""
    d = Decimal(str(val))
    q = Decimal("10") ** -decimals
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def validate_epoch_millis(v: Any) -> Optional[int]:
    """Strictly validate EpochMillis: int only (no bool, float, str), within allowed range."""
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("Instant must be an integer count of milliseconds")
    if not (100000000000 <= v <= 4102444800000):
        raise ValueError("Instant out of range [100000000000, 4102444800000]")
    return v


def truncate_ms(ms: int) -> int:
    """R1: Truncate instant to whole seconds."""
    return (ms // 1000) * 1000


def ms_to_dt(ms: int) -> datetime:
    """Convert truncated epoch ms to UTC tz-aware datetime."""
    truncated = truncate_ms(ms)
    return datetime.fromtimestamp(truncated / 1000, tz=timezone.utc)


def dt_to_ms(dt: Optional[Union[datetime, int, float]]) -> Optional[int]:
    """Convert BSON datetime or numeric instant to truncated epoch milliseconds."""
    if dt is None:
        return None
    if isinstance(dt, (int, float)):
        return truncate_ms(int(dt))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return truncate_ms(int(dt.timestamp() * 1000))


def now_ms() -> int:
    """Current UTC instant truncated to whole seconds."""
    return truncate_ms(int(datetime.now(timezone.utc).timestamp() * 1000))


def parse_time_hh_mm(s: str) -> dt_time:
    """Parse 'HH:MM' string to datetime.time."""
    h, m = map(int, s.split(":"))
    return dt_time(hour=h, minute=m)


# --------------------------------------------------------------------------- #
# Business Rules (R1 - R5)
# --------------------------------------------------------------------------- #
def compute_attendance_date(punch_in_ms: int, shift_start: str, shift_end: str) -> str:
    """
    R1 Time zone and attendance date:
    Dates are IST. A record's date is the IST calendar date of punch_in.
    Overnight shifts (shift_end <= shift_start): if punch_in IST time is earlier
    than shift_end, it belongs to the shift that started the previous day.
    """
    dt_utc = ms_to_dt(punch_in_ms)
    dt_ist = dt_utc.astimezone(IST)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        end_time = parse_time_hh_mm(shift_end)
        if dt_ist.time() < end_time:
            return (dt_ist.date() - timedelta(days=1)).isoformat()
    return dt_ist.date().isoformat()


def compute_late_minutes(punch_in_ms: int, attendance_date_str: str, shift_start: str) -> int:
    """
    R2 Late:
    Grace period is 10 minutes. Late only if punch-in is strictly more than 10:00
    after shift_start. When late, late_minutes = whole minutes (floor) since shift_start.
    """
    dt_utc = ms_to_dt(punch_in_ms)
    punch_ist = dt_utc.astimezone(IST)
    att_date = date.fromisoformat(attendance_date_str)
    start_time = parse_time_hh_mm(shift_start)
    shift_start_ist = datetime.combine(att_date, start_time, tzinfo=IST)

    grace_end_ist = shift_start_ist + timedelta(minutes=10)
    if punch_ist > grace_end_ist:
        diff_sec = (punch_ist - shift_start_ist).total_seconds()
        return int(diff_sec // 60)
    return 0


def compute_overtime(
    punch_out_ms: int, attendance_date_str: str, shift_start: str, shift_end: str
) -> int:
    """
    R3 Overtime:
    Whole minutes (floor) from shift_end to punch_out, counted only if >= 30; otherwise 0.
    For overnight shifts, shift_end falls on the next calendar day.
    """
    dt_utc = ms_to_dt(punch_out_ms)
    punch_ist = dt_utc.astimezone(IST)
    att_date = date.fromisoformat(attendance_date_str)
    is_overnight = shift_end <= shift_start
    shift_end_date = att_date + timedelta(days=1) if is_overnight else att_date
    end_time = parse_time_hh_mm(shift_end)
    shift_end_ist = datetime.combine(shift_end_date, end_time, tzinfo=IST)

    if punch_ist > shift_end_ist:
        diff_sec = (punch_ist - shift_end_ist).total_seconds()
        minutes = int(diff_sec // 60)
        return minutes if minutes >= 30 else 0
    return 0


def compute_work_hours(punch_in_ms: int, punch_out_ms: int) -> float:
    """
    R4 Work hours:
    (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = (truncate_ms(punch_out_ms) - truncate_ms(punch_in_ms)) / 1000
    return round_half_up(diff_sec / 3600, 2)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5 Half day:
    After punch-out, half_day is true when the rounded work_hours < 4.50.
    """
    return work_hours is not None and work_hours < 4.50


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be a valid calendar date YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def validate_shift(self) -> EmployeeCreate:
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[int] = None
    status: PresenceStatus = PresenceStatus.PRESENT

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v: Any) -> Optional[int]:
        return validate_epoch_millis(v)


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v: Any) -> Optional[int]:
        return validate_epoch_millis(v)


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[Status] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def validate_punches(cls, v: Any) -> Optional[int]:
        return validate_epoch_millis(v)


# --------------------------------------------------------------------------- #
# Document Formatting Helpers (MongoDB <-> API)
# --------------------------------------------------------------------------- #
def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def format_history_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    changes_formatted = {}
    changes = entry.get("changes", {})
    for k, v in changes.items():
        if k in ("punch_in", "punch_out"):
            changes_formatted[k] = {
                "from": dt_to_ms(v.get("from")) if isinstance(v.get("from"), datetime) else v.get("from"),
                "to": dt_to_ms(v.get("to")) if isinstance(v.get("to"), datetime) else v.get("to"),
            }
        else:
            changes_formatted[k] = {"from": v.get("from"), "to": v.get("to")}
    return {
        "at": dt_to_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes_formatted,
    }


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    history = [format_history_entry(h) for h in doc.get("history", [])]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


def json_safe(obj: Any) -> Any:
    """Recursively convert BSON/MongoDB objects (ObjectId, datetime, Decimal128) to JSON-safe types."""
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [json_safe(v) for v in obj]
    if isinstance(obj, datetime):
        return int(obj.timestamp() * 1000)
    if hasattr(obj, "to_decimal"):  # Decimal128
        return float(obj.to_decimal())
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (bytes, bytearray)):
        return str(obj)
    if hasattr(obj, "__str__") and type(obj).__name__ == "ObjectId":
        return str(obj)
    return obj


# --------------------------------------------------------------------------- #
# Query & Pipeline Builders (Shared between endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
def build_attendance_list_query(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> Tuple[str, Dict[str, Any], List[Tuple[str, int]], int, int]:
    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status_filter is not None:
        q["status"] = status_filter

    sort_spec = [("date", DESCENDING), ("emp_code", ASCENDING)]
    skip = (page - 1) * page_size
    return "attendance_logs", q, sort_spec, skip, page_size


def build_employee_monthly_pipeline(
    emp_code: str, month: str
) -> Tuple[str, List[Dict[str, Any]]]:
    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "day_of_week": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date"}
                    }
                },
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
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]
    return "attendance_logs", pipeline


def build_department_summary_pipeline(
    month: str, department: Optional[str] = None
) -> Tuple[str, List[Dict[str, Any]]]:
    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "date": 1,
                            "status": 1,
                            "work_hours": 1,
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "day_of_week": {
                                "$dayOfWeek": {
                                    "$dateFromString": {"dateString": "$date"}
                                }
                            },
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "emp_present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.day_of_week", [2, 3, 4, 5, 6]]},
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        ]
                                    },
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "emp_work_hours_sum": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$$l.work_hours", None]},
                                        ]
                                    },
                                    "$$l.work_hours",
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "emp_work_hours_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$$l.work_hours", None]},
                                        ]
                                    },
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
                            "in": {
                                "$cond": [
                                    {"$gt": ["$$l.late_minutes", 0]},
                                    1,
                                    0,
                                ]
                            },
                        }
                    }
                },
                "emp_total_late_minutes": {"$sum": "$logs.late_minutes"},
                "emp_leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$eq": ["$$l.status", "LEAVE"]},
                                    1,
                                    0,
                                ]
                            },
                        }
                    }
                },
                "emp_on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$eq": ["$$l.status", "ON_DUTY"]},
                                    1,
                                    0,
                                ]
                            },
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
                "total_work_hours": {"$sum": "$emp_work_hours_sum"},
                "work_hours_count": {"$sum": "$emp_work_hours_count"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
            }
        },
        {"$sort": {"_id": ASCENDING}},
    ]
    return "employees", pipeline


def build_late_leaderboard_pipeline(
    month: str, limit: int = 10, department: Optional[str] = None
) -> Tuple[str, List[Dict[str, Any]]]:
    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
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

    pipeline.extend(
        [
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
                    "sortBy": {"total_late_minutes": DESCENDING},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
        ]
    )
    return "attendance_logs", pipeline


def build_department_trend_pipeline(
    department: str, from_date_str: str, to_date_str: str
) -> Tuple[str, List[Dict[str, Any]]]:
    d_from = date.fromisoformat(from_date_str)
    d_to = date.fromisoformat(to_date_str)
    num_days = (d_to - d_from).days + 1

    pipeline = [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": "$department",
                "employees": {
                    "$push": {
                        "emp_code": "$emp_code",
                        "joined_on": "$joined_on",
                    }
                },
            }
        },
        {
            "$project": {
                "employees": 1,
                "day_offsets": {"$range": [0, num_days]},
            }
        },
        {"$unwind": "$day_offsets"},
        {
            "$project": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": {
                            "$dateAdd": {
                                "startDate": {"$dateFromString": {"dateString": from_date_str}},
                                "unit": "day",
                                "amount": "$day_offsets",
                            }
                        },
                    }
                },
                "employees": 1,
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": {
                    "$in": [
                        {
                            "$dayOfWeek": {
                                "$dateFromString": {"dateString": "$date"}
                            }
                        },
                        [2, 3, 4, 5, 6],
                    ]
                },
                "active_emp_codes": {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": "$employees",
                                "as": "e",
                                "cond": {"$lte": ["$$e.joined_on", "$date"]},
                            }
                        },
                        "as": "e",
                        "in": "$$e.emp_code",
                    }
                },
            }
        },
        {"$set": {"headcount": {"$size": "$active_emp_codes"}}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"cur_date": "$date", "codes": "$active_emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$cur_date"]},
                                    {"$in": ["$emp_code", "$$codes"]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "status": 1,
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$gt": ["$$l.late_minutes", 0]},
                                    1,
                                    0,
                                ]
                            },
                        }
                    }
                },
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
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": ASCENDING},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {"$sort": {"date": ASCENDING}},
    ]
    return "employees", pipeline


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/", include_in_schema=False)
def root():
    """Redirect root path to interactive Swagger API docs."""
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["system"])
def health():
    """Readiness probe. Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Database unavailable: {str(e)}")


@app.post("/employees", status_code=status.HTTP_201_CREATED, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """
    Create an employee.
    Enforces unique emp_code via unique index. Race-safe bare insert_one.
    """
    doc = body.model_dump()
    created_now = datetime.now(timezone.utc)
    doc["created_at"] = created_now
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="emp_code already exists")

    return format_employee_doc(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees sorted by emp_code asc. Paginated. Total reflects filters."""
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [format_employee_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """
    Punch-in endpoint.
    Creates day's record (R1, R2). Enforces uniqueness via unique index on (emp_code, date).
    Race safe: DuplicateKeyError -> 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    punch_ms = body.punched_at if body.punched_at is not None else now_ms()
    punch_ms = truncate_ms(punch_ms)
    att_date = compute_attendance_date(punch_ms, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_ms, att_date, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status.value,
        "punch_in": ms_to_dt(punch_ms),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="already punched in for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """
    Punch-out endpoint.
    Finds most recent punch-in <= punched_at.
    Check order:
      1. Employee existence (404)
      2. Candidate punch-in record existence (404)
      3. Already punched out check (409)
      4. punched_at <= punch_in or > 24 hours (422)
      5. Atomic update via find_one_and_update; if race conflict -> 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    out_ms = body.punched_at if body.punched_at is not None else now_ms()
    out_ms = truncate_ms(out_ms)
    out_dt = ms_to_dt(out_ms)

    # Find the employee's most recent record whose punch_in <= punched_at
    cand = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": out_dt}},
        sort=[("punch_in", DESCENDING)],
    )
    if not cand:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No matching punch-in record found")

    if cand.get("punch_out") is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Record already punched out")

    in_ms = dt_to_ms(cand["punch_in"])
    if in_ms is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Candidate record missing punch_in")

    if out_ms <= in_ms:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punched_at must be strictly after punch_in")

    duration_sec = (out_ms - in_ms) / 1000
    if duration_sec > 24 * 3600:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch duration cannot exceed 24 hours")

    work_hrs = compute_work_hours(in_ms, out_ms)
    overtime = compute_overtime(out_ms, cand["date"], emp["shift_start"], emp["shift_end"])
    is_half = compute_half_day(work_hrs)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": cand["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": out_dt,
                "work_hours": work_hrs,
                "overtime_minutes": overtime,
                "half_day": is_half,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Concurrent punch-out conflict; record already punched out")

    return format_attendance_doc(updated)


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    body: RegularizeRequest,
    emp_code: str = Path(...),
    date_str: str = Path(..., alias="date"),
):
    """
    Regularize attendance record with optimistic concurrency retries.
    Appends one history entry documenting only fields that actually changed.
    """
    try:
        date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid date format YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    # Retry loop for optimistic concurrency
    max_retries = 5
    for attempt in range(max_retries):
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
        if not rec:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attendance record not found")

        curr_status = rec["status"]
        new_status = body.status.value if body.status is not None else curr_status

        # ABSENT / LEAVE rule: clears punch times; supplying punches is 422
        if new_status in ("ABSENT", "LEAVE"):
            if body.punch_in is not None or body.punch_out is not None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Cannot supply punch_in or punch_out when setting ABSENT or LEAVE",
                )
            final_punch_in_ms = None
            final_punch_out_ms = None
            final_work_hours = None
            final_late_minutes = 0
            final_overtime_minutes = 0
            final_half_day = False
        else:
            # Presence status requires punch_in
            if body.punch_in is not None:
                final_punch_in_ms = truncate_ms(body.punch_in)
            else:
                final_punch_in_ms = dt_to_ms(rec.get("punch_in"))

            if final_punch_in_ms is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Presence status requires a punch_in instant",
                )

            # punch_in must map to the same attendance date under R1
            computed_date = compute_attendance_date(final_punch_in_ms, emp["shift_start"], emp["shift_end"])
            if computed_date != date_str:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"punch_in maps to {computed_date}, which differs from record date {date_str}",
                )

            # punch_out calculation
            if body.punch_out is not None:
                final_punch_out_ms = truncate_ms(body.punch_out)
            else:
                final_punch_out_ms = dt_to_ms(rec.get("punch_out"))

            final_late_minutes = compute_late_minutes(final_punch_in_ms, date_str, emp["shift_start"])

            if final_punch_out_ms is not None:
                if final_punch_out_ms <= final_punch_in_ms:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="punch_out must be strictly greater than punch_in",
                    )
                if (final_punch_out_ms - final_punch_in_ms) / 1000 > 24 * 3600:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="punch duration cannot exceed 24 hours",
                    )
                final_work_hours = compute_work_hours(final_punch_in_ms, final_punch_out_ms)
                final_overtime_minutes = compute_overtime(
                    final_punch_out_ms, date_str, emp["shift_start"], emp["shift_end"]
                )
                final_half_day = compute_half_day(final_work_hours)
            else:
                final_work_hours = None
                final_overtime_minutes = 0
                final_half_day = False

        # Detect changes between existing stored values and final values
        changes_api: Dict[str, Any] = {}
        changes_bson: Dict[str, Any] = {}

        if new_status != curr_status:
            changes_api["status"] = {"from": curr_status, "to": new_status}
            changes_bson["status"] = {"from": curr_status, "to": new_status}

        old_in_ms = dt_to_ms(rec.get("punch_in"))
        if final_punch_in_ms != old_in_ms:
            changes_api["punch_in"] = {"from": old_in_ms, "to": final_punch_in_ms}
            changes_bson["punch_in"] = {
                "from": rec.get("punch_in"),
                "to": ms_to_dt(final_punch_in_ms) if final_punch_in_ms is not None else None,
            }

        old_out_ms = dt_to_ms(rec.get("punch_out"))
        if final_punch_out_ms != old_out_ms:
            changes_api["punch_out"] = {"from": old_out_ms, "to": final_punch_out_ms}
            changes_bson["punch_out"] = {
                "from": rec.get("punch_out"),
                "to": ms_to_dt(final_punch_out_ms) if final_punch_out_ms is not None else None,
            }

        old_wh = rec.get("work_hours")
        if final_work_hours != old_wh:
            changes_api["work_hours"] = {"from": old_wh, "to": final_work_hours}
            changes_bson["work_hours"] = {"from": old_wh, "to": final_work_hours}

        old_late = rec.get("late_minutes", 0)
        if final_late_minutes != old_late:
            changes_api["late_minutes"] = {"from": old_late, "to": final_late_minutes}
            changes_bson["late_minutes"] = {"from": old_late, "to": final_late_minutes}

        old_ot = rec.get("overtime_minutes", 0)
        if final_overtime_minutes != old_ot:
            changes_api["overtime_minutes"] = {"from": old_ot, "to": final_overtime_minutes}
            changes_bson["overtime_minutes"] = {"from": old_ot, "to": final_overtime_minutes}

        old_half = rec.get("half_day", False)
        if final_half_day != old_half:
            changes_api["half_day"] = {"from": old_half, "to": final_half_day}
            changes_bson["half_day"] = {"from": old_half, "to": final_half_day}

        # A request that changes nothing is 422
        if not changes_bson:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Request changes nothing")

        now_utc = datetime.now(timezone.utc)
        history_entry_bson = {
            "at": now_utc,
            "by": body.regularized_by,
            "reason": body.reason,
            "changes": changes_bson,
        }

        # Optimistic concurrency check: match specific document ID, status, punch times, and history
        hist = rec.get("history")
        hist_len = len(hist) if hist is not None else 0
        if hist_len == 0:
            hist_clause = {"$or": [{"history": {"$size": 0}}, {"history": {"$exists": False}}]}
        else:
            hist_clause = {"history": {"$size": hist_len}}

        update_filter = {
            "$and": [
                {"_id": rec["_id"]},
                {"status": rec["status"]},
                {"punch_in": rec.get("punch_in")},
                {"punch_out": rec.get("punch_out")},
                hist_clause,
            ]
        }

        update_fields = {
            "status": new_status,
            "punch_in": ms_to_dt(final_punch_in_ms) if final_punch_in_ms is not None else None,
            "punch_out": ms_to_dt(final_punch_out_ms) if final_punch_out_ms is not None else None,
            "work_hours": final_work_hours,
            "late_minutes": final_late_minutes,
            "overtime_minutes": final_overtime_minutes,
            "half_day": final_half_day,
        }

        res = db.attendance_logs.update_one(
            update_filter,
            {"$set": update_fields, "$push": {"history": history_entry_bson}},
        )

        if res.matched_count == 1:
            updated = db.attendance_logs.find_one({"_id": rec["_id"]})
            return format_attendance_doc(updated)

        time.sleep(0.01 * (attempt + 1))

    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Concurrent regularize modification conflict")


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[Status] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List attendance records sorted by date desc, emp_code asc. Paginated."""
    if date_from:
        try:
            date.fromisoformat(date_from)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid date_from")
    if date_to:
        try:
            date.fromisoformat(date_to)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid date_to")

    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from must be <= date_to")

    st_val = status_filter.value if status_filter else None
    col, q, sort_spec, skip, limit = build_attendance_list_query(
        emp_code=emp_code,
        date_from=date_from,
        date_to=date_to,
        status_filter=st_val,
        page=page,
        page_size=page_size,
    )

    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q).sort(sort_spec).skip(skip).limit(limit)
    items = [format_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary for one employee."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    joined_date_str = emp["joined_on"]

    # R7: working_days Mon-Fri in month from max(first_of_month, joined_on) to end of month
    working_days = 0
    for day_num in range(1, last_day + 1):
        cur_d_str = f"{month}-{day_num:02d}"
        cur_d = date(year, m, day_num)
        if cur_d_str >= joined_date_str and cur_d.weekday() < 5:
            working_days += 1

    col, pipeline = build_employee_monthly_pipeline(emp_code, month)
    agg_res = list(db.attendance_logs.aggregate(pipeline))

    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late = int(row.get("total_late_minutes", 0))
        total_ot = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    attendance_pct = (
        round_half_up((present_days / working_days) * 100, 2) if working_days > 0 else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """Department summary for a month, including zero-log employees."""
    col, pipeline = build_department_summary_pipeline(month, department)
    agg_res = list(db.employees.aggregate(pipeline))

    items = []
    for row in agg_res:
        dept_name = row["_id"]
        headcount = int(row.get("headcount", 0))
        if headcount == 0:
            continue
        present_days = float(row.get("present_days", 0.0))
        wh_sum = float(row.get("total_work_hours", 0.0))
        wh_cnt = int(row.get("work_hours_count", 0))
        avg_wh = round_half_up(wh_sum / wh_cnt, 2) if wh_cnt > 0 else None

        items.append(
            {
                "department": dept_name,
                "headcount": headcount,
                "present_days": present_days,
                "avg_work_hours": avg_wh,
                "late_count": int(row.get("late_count", 0)),
                "total_late_minutes": int(row.get("total_late_minutes", 0)),
                "leave_count": int(row.get("leave_count", 0)),
                "on_duty_count": int(row.get("on_duty_count", 0)),
            }
        )

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """Late-comers leaderboard for a month using competition ranking with ties."""
    col, pipeline = build_late_leaderboard_pipeline(month, limit, department)
    agg_res = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for row in agg_res:
        items.append(
            {
                "rank": int(row["rank"]),
                "emp_code": row["emp_code"],
                "name": row["name"],
                "department": row["department"],
                "total_late_minutes": int(row["total_late_minutes"]),
                "late_count": int(row["late_count"]),
            }
        )

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date_str: str = Query(..., alias="from"),
    to_date_str: str = Query(..., alias="to"),
):
    """Daily attendance trend for a department generated inside MongoDB."""
    try:
        d_from = date.fromisoformat(from_date_str)
        d_to = date.fromisoformat(to_date_str)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid date format YYYY-MM-DD")

    if d_to < d_from:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="to date must be >= from date")

    day_count = (d_to - d_from).days + 1
    if day_count > 92:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Date range exceeds 92 days")

    # 404 if department has no employees
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")

    col, pipeline = build_department_trend_pipeline(department, from_date_str, to_date_str)
    agg_res = list(db.employees.aggregate(pipeline))

    # Apply 4-decimal half-up rounding on reported rates
    items = []
    for row in agg_res:
        raw_rate = row.get("attendance_rate")
        rate = round_half_up(raw_rate, 4) if raw_rate is not None else None
        raw_mavg = row.get("moving_avg_7d")
        mavg = round_half_up(raw_mavg, 4) if raw_mavg is not None else None

        items.append(
            {
                "date": row["date"],
                "is_working_day": bool(row["is_working_day"]),
                "headcount": int(row.get("headcount", 0)),
                "present_count": float(row.get("present_count", 0.0)),
                "late_count": int(row.get("late_count", 0)),
                "attendance_rate": rate,
                "moving_avg_7d": mavg,
            }
        )

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: ExplainEndpoint = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[Status] = Query(None, alias="status"),
    from_date_str: Optional[str] = Query(None, alias="from"),
    to_date_str: Optional[str] = Query(None, alias="to"),
    page: Optional[int] = Query(1, ge=1),
    page_size: Optional[int] = Query(20, ge=1, le=100),
):
    """
    MongoDB explain output with executionStats verbosity for endpoint's main query.
    Built with the exact same shared query builders as the real endpoints.
    """
    if endpoint == ExplainEndpoint.attendance_list:
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from must be <= date_to")
        st_val = status_filter.value if status_filter else None
        col, q, sort_spec, skip, limit_val = build_attendance_list_query(
            emp_code=emp_code,
            date_from=date_from,
            date_to=date_to,
            status=st_val,
            page=page or 1,
            page_size=page_size or 20,
        )
        explain_cmd = {
            "explain": {
                "find": col,
                "filter": q,
                "sort": dict(sort_spec),
                "skip": skip,
                "limit": limit_val,
            },
            "verbosity": "executionStats",
        }
        res = db.command(explain_cmd)
        return {"endpoint": endpoint.value, "collection": col, "explain": json_safe(res)}

    elif endpoint == ExplainEndpoint.employee_monthly:
        if not emp_code:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="emp_code required")
        if not month:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="month required")
        col, pipeline = build_employee_monthly_pipeline(emp_code, month)
        explain_cmd = {
            "explain": {
                "aggregate": col,
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(explain_cmd)
        return {"endpoint": endpoint.value, "collection": col, "explain": json_safe(res)}

    elif endpoint == ExplainEndpoint.department_summary:
        if not month:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="month required")
        col, pipeline = build_department_summary_pipeline(month, department)
        explain_cmd = {
            "explain": {
                "aggregate": col,
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(explain_cmd)
        return {"endpoint": endpoint.value, "collection": col, "explain": json_safe(res)}

    elif endpoint == ExplainEndpoint.late_leaderboard:
        if not month:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="month required")
        col, pipeline = build_late_leaderboard_pipeline(month, limit or 10, department)
        explain_cmd = {
            "explain": {
                "aggregate": col,
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(explain_cmd)
        return {"endpoint": endpoint.value, "collection": col, "explain": json_safe(res)}

    elif endpoint == ExplainEndpoint.department_trend:
        if not department:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="department required")
        if not from_date_str or not to_date_str:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="from and to dates required")
        try:
            d_from = date.fromisoformat(from_date_str)
            d_to = date.fromisoformat(to_date_str)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid date format")
        if d_to < d_from:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="to must be >= from")
        if (d_to - d_from).days + 1 > 92:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Range exceeds 92 days")

        col, pipeline = build_department_trend_pipeline(department, from_date_str, to_date_str)
        explain_cmd = {
            "explain": {
                "aggregate": col,
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(explain_cmd)
        return {"endpoint": endpoint.value, "collection": col, "explain": json_safe(res)}

    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown endpoint")
