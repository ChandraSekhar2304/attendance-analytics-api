# Employee Attendance & Analytics API

A FastAPI and MongoDB service providing employee attendance tracking and analytics following the OpenAPI 3.0.3 contract and business rules R1–R10.

## How to Run

1. **Set up virtual environment & install dependencies:**
   ```bash
   python -m venv .venv
   source .venv/bin/activate    # On Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Configure environment:**
   Create a `.env` file (or set system environment variables):
   ```env
   MONGO_URI=mongodb://localhost:27017
   MONGO_DB=attendance_db
   ```
   *Note: Real environment variables take precedence over `.env`.*

3. **Seed sample data:**
   ```bash
   python sample_seed.py
   ```

4. **Start the server:**
   ```bash
   uvicorn app.main:app --port 8000
   ```
   The `/health` endpoint is available immediately and reports 200 OK once MongoDB is responsive.

---

## Design Decisions & Assumptions

### 1. `POST /attendance/punch-out` Check Order
The validation and update sequence strictly follows this order:
1. **Employee Verification:** Confirm `emp_code` exists in `employees` (returns 404 if not found).
2. **Find Candidate Record:** Query `attendance_logs` for the employee's most recent record with `punch_in <= punched_at` sorted by `punch_in` descending (returns 404 if no record exists).
3. **Already Punched Out Check:** If the candidate record already has a non-null `punch_out`, return 409 Conflict.
4. **Instant Bounds Validation:**
   - If `punched_at <= punch_in` (i.e. identical timestamp or earlier), return 422 Unprocessable Entity.
   - If `(punched_at - punch_in) > 24 hours` (86,400 seconds), return 422 Unprocessable Entity.
5. **Atomic Update:** Atomically update the record using `find_one_and_update` with query `{"_id": cand["_id"], "punch_out": null}`. If another concurrent request already punched out the record, it returns 409 Conflict. Two concurrent punch-outs yield exactly one 200 and one 409.

### 2. Department Trend Moving Average
- In `GET /analytics/departments/{department}/trend`, daily attendance rates are rounded to 4 decimals half-up on working days where headcount > 0 (null otherwise).
- Per the specification note, the 7-day moving average (`moving_avg_7d`) is calculated over the **reported, rounded** `attendance_rate` values across the document window `[-6, 0]`, ignoring nulls, and rounded to 4 decimals half-up.

### 3. Half-Up Rounding & Exact Math
- Python's standard `round()` uses Banker's Rounding (round-half-to-even). To strictly adhere to R4 and R8, all reporting calculations (work hours, percentages, rates) use exact Decimal half-up arithmetic (`Decimal(ROUND_HALF_UP)`).

### 4. Index-Backed Pipelines & Explain
- All list queries and aggregation pipelines are backed by compound indexes created idempotently at startup:
  - `employees`: unique `emp_code`, `(department, emp_code)`, `(joined_on, department)`, `(department, joined_on)`.
  - `attendance_logs`: unique `(emp_code, date)`, `(date: -1, emp_code: 1)`, `(status: 1, date: -1, emp_code: 1)`, `(emp_code: 1, punch_in: -1)`, `(date: 1, late_minutes: 1)`.
- All explained operations in `GET /admin/explain/{endpoint}` run using index scans (`IXSCAN`) with zero collection scans (`COLLSCAN`).

---

## Testing & Verification
A standalone verification script `test_and_seed_100k.py` is included to generate ~100k attendance records and test awkward cases (overnight shifts across midnight, ties at leaderboard limits, weekend records, mid-month joiners, and concurrency races).
