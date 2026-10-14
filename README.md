# Employee Attendance & Analytics API

FastAPI + MongoDB service implementing employee attendance tracking and analytics (OpenAPI 3.0.3, rules R1–R10).

## How to Run & Environment
```bash
python -m venv .venv
# On Windows: .\.venv\Scripts\Activate.ps1 | On Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
# Env vars: MONGO_URI (default: mongodb://localhost:27017), MONGO_DB (default: attendance_db)
uvicorn app.main:app --port 8000
```
`/health` is ready immediately and returns HTTP 200 when MongoDB is connected (503 if unreachable).

## Assumptions
- **Punch-Out Check Order:** 404 if employee missing, 404 if no record with `punch_in <= punched_at`, 409 if already punched out, 422 if `punched_at <= punch_in` or `duration > 24h`, then atomic `find_one_and_update` on `{"punch_out": null}` returning 409 on conflict.
- **Date Range Limit:** In department trend, 92 days is inclusive (`(to - from).days + 1 <= 92`). 92 days returns 200; 93 days returns 422.
- **Moving Average:** `moving_avg_7d` is calculated over the reported rounded daily attendance rates across window `[-6, 0]`, ignoring nulls.
- **Weekend Records:** Monthly employee analytics (`R6`) counts `present_days` on Mon–Fri only; `late_minutes` and `overtime_minutes` sum across all records. Department trend includes weekends with `is_working_day: false` and `attendance_rate: null`.

## Verification
- **Verified with live tests:** Late boundaries (09:40:00 -> 0, 09:40:01 -> 10, 10:15:59 -> 45); overnight shift (22:00-06:00, 40 min overtime, start-day date); 92-day (200) vs 93-day (422) trend; competition rank ties (1,2,2); 20-thread concurrency for punch-in (1x 201, 19x 409), punch-out (1x 200, 19x 409), employee creation (1x 201, 19x 409), and PATCH updates (no history lost); legacy doc PATCH without history field; exact Decimal rounding (8.98 + 9.03 -> 9.01, 0.5/16 -> 3.13%, 1/32 -> 0.0313); all 5 admin explain endpoints verified using `IXSCAN` and zero `COLLSCAN`; 56 bad-input calls with 0 500 errors.
- **Not verified:** Multi-node MongoDB replica sets, sharding, and authentication/RBAC (not specified in contract).
