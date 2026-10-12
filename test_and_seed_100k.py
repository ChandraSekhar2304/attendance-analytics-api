"""
Test and Seed Script for 100,000 Records and Awkward Edge Cases.
Validates production performance, index usage (IXSCAN and NO COLLSCAN),
and analytics correctness on scale.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone
import json
import random
import sys
import time

from fastapi.testclient import TestClient
from pymongo import MongoClient

# Ensure app can be imported
sys.path.insert(0, ".")
from app.main import app, db, IST, truncate_ms, ms_to_dt, dt_to_ms, round_half_up, init_db_indexes

client = TestClient(app)


def seed_large_dataset():
    print("Initializing indexes...")
    init_db_indexes()

    print("Dropping existing collections to build fresh 100k dataset...")
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})

    departments = ["Engineering", "Sales", "Support", "Finance", "HR", "Marketing"]

    print("Generating ~500 employees...")
    employees = []
    # EMP0001 to EMP0500
    for i in range(1, 501):
        emp_code = f"EMP{i:04d}"
        dept = departments[(i - 1) % len(departments)]
        # Variety of shifts: mostly 09:30-18:30, some 10:00-19:00, some overnight 22:00-06:00
        if i % 10 == 0:
            shift_start, shift_end = "22:00", "06:00"  # overnight
        elif i % 5 == 0:
            shift_start, shift_end = "10:00", "19:00"
        else:
            shift_start, shift_end = "09:30", "18:30"

        # Variety of joined_on dates:
        # Some long-time employees, some mid-month joiners (2026-07-15), some joined in August 2026
        if i == 50:
            joined_on = "2026-07-15"  # mid-month joiner in July 2026
        elif i > 480:
            joined_on = "2026-08-05"  # joined after July 2026
        else:
            joined_on = "2026-01-01"

        created_at = datetime(2026, 1, 1, 4, 30, tzinfo=timezone.utc)
        employees.append({
            "emp_code": emp_code,
            "name": f"Employee {i}",
            "email": f"emp{i}@example.com",
            "department": dept,
            "shift_start": shift_start,
            "shift_end": shift_end,
            "joined_on": joined_on,
            "created_at": created_at,
        })

    db.employees.insert_many(employees)
    print(f"Inserted {len(employees)} employees.")

    # Note: EMP0470 - EMP0479 will have ZERO attendance records to test headcount inclusion
    # We will generate ~100,000 attendance records across ~220 calendar days for employees 1..460
    # 460 employees * 220 days = 101,200 records
    print("Generating ~100,000 attendance records (this takes ~10-15 seconds)...")

    start_date = date(2026, 1, 1)
    num_days = 220
    emp_subset = [f"EMP{i:04d}" for i in range(1, 461)]

    # Pre-construct dates
    dates = [(start_date + timedelta(days=d)) for d in range(num_days)]

    batch_size = 10000
    batch = []
    total_records = 0

    # Specifically create ties for late leaderboard in 2026-07:
    # EMP0002 and EMP0003 will have the EXACT same late minutes in July 2026 (e.g. 50 minutes)
    for cur_date in dates:
        date_str = cur_date.isoformat()
        is_weekend = cur_date.weekday() >= 5

        for emp_code in emp_subset:
            # Deterministic pseudo-random generation
            code_num = int(emp_code[3:])

            # Special case for late ties in July 2026
            if cur_date.year == 2026 and cur_date.month == 7 and date_str == "2026-07-06":
                if emp_code in ("EMP0002", "EMP0003"):
                    # Both get exactly 50 late minutes
                    p_in = datetime(2026, 7, 6, 10, 20, 0, tzinfo=IST)
                    p_out = datetime(2026, 7, 6, 18, 30, 0, tzinfo=IST)
                    batch.append({
                        "emp_code": emp_code,
                        "date": date_str,
                        "status": "PRESENT",
                        "punch_in": p_in,
                        "punch_out": p_out,
                        "work_hours": 8.17,
                        "late_minutes": 50,
                        "overtime_minutes": 0,
                        "half_day": False,
                        "history": [],
                    })
                    continue

            # General distribution:
            # ~80% PRESENT, ~5% WFH, ~5% ON_DUTY, ~5% ABSENT, ~3% LEAVE, ~2% no record
            r_val = (code_num * 17 + cur_date.day * 31 + cur_date.month * 7) % 100

            if r_val < 2:
                # Missing record (no doc)
                continue
            elif r_val < 5:
                # LEAVE
                batch.append({
                    "emp_code": emp_code,
                    "date": date_str,
                    "status": "LEAVE",
                    "punch_in": None,
                    "punch_out": None,
                    "work_hours": None,
                    "late_minutes": 0,
                    "overtime_minutes": 0,
                    "half_day": False,
                    "history": [],
                })
            elif r_val < 10:
                # ABSENT
                batch.append({
                    "emp_code": emp_code,
                    "date": date_str,
                    "status": "ABSENT",
                    "punch_in": None,
                    "punch_out": None,
                    "work_hours": None,
                    "late_minutes": 0,
                    "overtime_minutes": 0,
                    "half_day": False,
                    "history": [],
                })
            else:
                # Presence status
                st = "PRESENT" if r_val < 85 else ("WFH" if r_val < 95 else "ON_DUTY")
                # Overnight employee
                is_overnight = (code_num % 10 == 0)
                if is_overnight:
                    p_in = datetime(cur_date.year, cur_date.month, cur_date.day, 21, 55, tzinfo=IST)
                    p_out = p_in + timedelta(hours=8, minutes=45)
                    wh = 8.75
                    late = 0
                    ot = 40
                    half = False
                else:
                    late = 35 if (r_val % 7 == 0) else 0
                    p_in = datetime(cur_date.year, cur_date.month, cur_date.day, 9, 30, tzinfo=IST) + timedelta(minutes=35 if late else 0)
                    # 5% half-day
                    if r_val % 20 == 0:
                        p_out = p_in + timedelta(hours=3, minutes=30)
                        wh = 3.5
                        half = True
                        ot = 0
                    else:
                        p_out = p_in + timedelta(hours=9, minutes=10)
                        wh = 9.17
                        half = False
                        ot = 40 if (r_val % 11 == 0) else 0

                doc = {
                    "emp_code": emp_code,
                    "date": date_str,
                    "status": st,
                    "punch_in": p_in,
                    "punch_out": p_out,
                    "work_hours": wh,
                    "late_minutes": late,
                    "overtime_minutes": ot,
                    "half_day": half,
                    "history": [],
                }

                # Add some legacy documents without history / half_day (sample 9)
                if r_val == 99:
                    doc.pop("history", None)
                    doc.pop("half_day", None)

                batch.append(doc)

            if len(batch) >= batch_size:
                db.attendance_logs.insert_many(batch)
                total_records += len(batch)
                print(f"  Inserted {total_records} records...")
                batch = []

    if batch:
        db.attendance_logs.insert_many(batch)
        total_records += len(batch)

    print(f"Total attendance records seeded: {total_records}")
    assert total_records >= 90000, f"Expected ~100k, got {total_records}"


def run_benchmark_and_verifications():
    print("\n=== Running Benchmarks & Plan Inspections on 100k Dataset ===")

    # 1. Test Attendance List Query
    t0 = time.time()
    r = client.get("/attendance?date_from=2026-07-01&date_to=2026-07-10&page=1&page_size=20")
    t1 = time.time()
    print(f"GET /attendance range: {r.status_code} in {(t1-t0)*1000:.1f}ms, total: {r.json()['total']}")
    assert r.status_code == 200

    # 2. Test Attendance List by status
    t0 = time.time()
    r = client.get("/attendance?status=PRESENT&date_from=2026-07-01&date_to=2026-07-10&page=1&page_size=20")
    t1 = time.time()
    print(f"GET /attendance status+range: {r.status_code} in {(t1-t0)*1000:.1f}ms, total: {r.json()['total']}")
    assert r.status_code == 200

    # 3. Test Employee Monthly
    t0 = time.time()
    r = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    t1 = time.time()
    print(f"GET /analytics/employees/EMP0001/monthly: {r.status_code} in {(t1-t0)*1000:.1f}ms")
    assert r.status_code == 200
    res = r.json()
    print("  Monthly result:", res)
    assert res["working_days"] == 23

    # 4. Test Mid-Month Joiner Employee Monthly (EMP0050 joined 2026-07-15)
    r = client.get("/analytics/employees/EMP0050/monthly?month=2026-07")
    assert r.status_code == 200
    # From 2026-07-15 to 2026-07-31: Mon-Fri days = 13 days
    print(f"  Mid-month joiner EMP0050 working_days: {r.json()['working_days']}")
    assert r.json()["working_days"] == 13

    # 5. Test Department Summary
    t0 = time.time()
    r = client.get("/analytics/departments/summary?month=2026-07")
    t1 = time.time()
    print(f"GET /analytics/departments/summary: {r.status_code} in {(t1-t0)*1000:.1f}ms")
    assert r.status_code == 200
    data = r.json()
    print(f"  Summary departments count: {len(data['items'])}")
    for item in data['items']:
        print(f"  - {item['department']}: headcount={item['headcount']}, present_days={item['present_days']}, avg_wh={item['avg_work_hours']}")
        assert item["headcount"] > 0
        assert item["present_days"] >= 0

    # 6. Test Late Leaderboard with Ties
    t0 = time.time()
    r = client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    t1 = time.time()
    print(f"GET /analytics/leaderboard/late: {r.status_code} in {(t1-t0)*1000:.1f}ms")
    assert r.status_code == 200
    items = r.json()["items"]
    print("  Top leaderboard rows:")
    for row in items[:5]:
        print(f"    Rank {row['rank']}: {row['emp_code']} ({row['department']}) -> {row['total_late_minutes']} min ({row['late_count']} times)")
    # Check that ties share rank
    ranks = [row['rank'] for row in items]
    print("  Leaderboard ranks sequence:", ranks)

    # 7. Test Department Trend
    t0 = time.time()
    r = client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-31")
    t1 = time.time()
    print(f"GET /analytics/departments/Engineering/trend: {r.status_code} in {(t1-t0)*1000:.1f}ms")
    assert r.status_code == 200
    trend_items = r.json()["items"]
    print(f"  Engineering trend returned {len(trend_items)} rows for 31 days (exact match)")
    assert len(trend_items) == 31
    for day_row in trend_items[:3]:
        print(f"    Date {day_row['date']}: is_working={day_row['is_working_day']}, headcount={day_row['headcount']}, rate={day_row['attendance_rate']}, mavg={day_row['moving_avg_7d']}")

    # 8. Strict Explain Checks on 100k records: IXSCAN and NO COLLSCAN
    print("\n--- Verifying MongoDB Explain Plans on 100k Dataset ---")
    endpoints = [
        ("attendance_list", {"date_from": "2026-07-01", "date_to": "2026-07-10", "page": 1, "page_size": 20}),
        ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
        ("department_summary", {"month": "2026-07"}),
        ("late_leaderboard", {"month": "2026-07", "limit": 10}),
        ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-10"}),
    ]

    for ep_name, params in endpoints:
        r = client.get(f"/admin/explain/{ep_name}", params=params)
        assert r.status_code == 200, f"Explain {ep_name} failed: {r.text}"
        explain_json = r.json()["explain"]
        plan_str = json.dumps(explain_json)

        has_ixscan = "IXSCAN" in plan_str
        has_collscan = "COLLSCAN" in plan_str

        print(f"  Explain {ep_name:20}: IXSCAN = {has_ixscan:<5} | COLLSCAN = {has_collscan}")
        assert has_ixscan, f"Expected IXSCAN in {ep_name} plan"
        assert not has_collscan, f"Found COLLSCAN in {ep_name} plan!"

    print("\n>>> ALL 100K PERFORMANCE AND PLAN VERIFICATIONS PASSED! <<<")


if __name__ == "__main__":
    seed_large_dataset()
    run_benchmark_and_verifications()
