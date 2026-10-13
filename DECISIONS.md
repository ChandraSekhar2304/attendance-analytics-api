# DECISIONS.md

1. **Indexes.**
On `employees`, I created unique `emp_code` for identity, and `(department, emp_code)` plus `(joined_on, department)` for headcount scans. On `attendance_logs`, I created unique `(emp_code, date)`, `(date: -1, emp_code: 1)` and `(status: 1, date: -1, emp_code: 1)` to avoid in-memory sorting, `(emp_code: 1, punch_in: -1)` for punch-outs, and `(date: 1, late_minutes: 1)` for the leaderboard. I considered and rejected an index on `late_minutes` alone because queries filter by date first and group by employee.

2. **Punch-in race.**
Both requests compute the date and issue `insert_one`. The unique index on `(emp_code, date)` accepts the first and rejects the second with `DuplicateKeyError`. I catch this and return 409, eliminating check-then-insert races.

3. **Ties.**
I rank with `$setWindowFields` using `$rank` (1, 2, 2, 4). Filtering `rank <= limit` runs afterwards. Boundary ties share a rank and are all returned, even when exceeding `limit`.

4. **Headcount.**
The pipeline starts on `employees` (`joined_on <= month_end`) and embeds logs via `$lookup` without `$unwind`. Because `$group` aggregates employee records directly, zero-log employees still count as 1 towards headcount.

5. **One thing for 100x data.**
I would pre-aggregate monthly department stats via change streams instead of executing on-demand `$lookup` pipelines over 10M raw logs.
