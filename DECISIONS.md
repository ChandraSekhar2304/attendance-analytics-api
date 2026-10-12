# DECISIONS.md

1. **Indexes.**
On `employees`: unique `emp_code` enforces business key uniqueness; `(department, emp_code)` and `(joined_on, department)` optimize department lookups and headcount filters. On `attendance_logs`: unique `(emp_code, date)` guarantees one record per day; compound `(date: -1, emp_code: 1)` and `(status: 1, date: -1, emp_code: 1)` back index-only pagination; `(emp_code: 1, punch_in: -1)` powers punch-out lookups; `(date: 1, late_minutes: 1)` speeds up monthly late queries. We rejected indexing `history` because it is an audit trail appended to documents, never queried independently.

2. **Punch-in race.**
Both concurrent requests pass validation, compute the date, and issue an `insert_one`. The database's unique compound index on `(emp_code, date)` atomically accepts the first write and rejects the second with `DuplicateKeyError`. Our code catches this exception and returns HTTP 409, completely eliminating check-then-insert race conditions.

3. **Ties.**
We use `$setWindowFields` with `$rank` (standard competition ranking: 1, 2, 2, 4), and filter `rank <= limit` afterwards. Tied employees at the boundary share the same rank and are all returned, ordered by `total_late_minutes` descending, then `emp_code` ascending.

4. **Headcount.**
The pipeline starts on `employees` (filtered by `joined_on <= month_end`), then uses `$lookup` to fetch the month's attendance logs. Employees with no logs yield an empty logs array, ensuring each employee contributes 1 to the department headcount during `$group`.

5. **One thing to change for 100x data.**
At 100x scale (~10M logs), we would maintain pre-aggregated monthly departmental summaries via change streams or daily rollup jobs instead of computing cross-collection `$lookup` pipelines on demand.
