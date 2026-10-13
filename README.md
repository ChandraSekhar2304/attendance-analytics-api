# Employee Attendance & Analytics API

FastAPI + MongoDB service implementing employee attendance tracking and analytics (OpenAPI 3.0.3, rules R1–R10).

## How to Run
```bash
python -m venv .venv
# On Windows: .\.venv\Scripts\Activate.ps1 | On Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
# Environment: MONGO_URI (default: mongodb://localhost:27017), MONGO_DB (default: attendance_db)
uvicorn app.main:app --port 8000
```
`/health` is ready immediately and returns HTTP 200 when MongoDB is connected (503 if unreachable).

## Assumptions for Ambiguous Rules
- **Punch-Out Check Order:** Evaluates candidate record in order: 404 if employee missing, 404 if no record with `punch_in <= punched_at`, 409 if already punched out, 422 if `punched_at <= punch_in` or `duration > 24h`, then atomic `find_one_and_update` on `{"punch_out": null}` returning 409 on conflict.
- **Date Range Limit:** In `GET /analytics/departments/{department}/trend`, the 92-day limit is counted inclusively (`(to - from).days + 1 <= 92`). 92 days returns 200; 93 days returns 422.
- **Moving Average:** The 7-day moving average (`moving_avg_7d`) is calculated over the reported rounded daily attendance rates across window `[-6, 0]`, ignoring nulls.
- **Weekend Records:** Monthly employee analytics (`R6`) counts `present_days` on Monday–Friday only; `total_late_minutes` and `total_overtime_minutes` sum across all month records including weekends. Department trend (`R8`) includes weekend days with `is_working_day: false` and `attendance_rate: null`.

## Verification Status
- **Tested & Verified:** All 12 endpoints verified with live HTTP tests on a ~100k attendance dataset. All 5 explain endpoints verified programmatically to use `IXSCAN` with zero `COLLSCAN`. 20-thread concurrency tested for employee creation, punch-in, punch-out, and PATCH regularization.
- **Not Implemented / Scope Limits:** No user authentication or role-based access control was required by the specification or contract.
