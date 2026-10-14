# DECISIONS.md

1. **Indexes.**
   - `employees`: Unique `{"emp_code": 1}` enforces ID uniqueness. `{"department": 1, "joined_on": 1}` and `{"joined_on": 1}` serve department filters and period headcount filtering.
   - `attendance_logs`: Unique `{"emp_code": 1, "date": 1}` guarantees one record per employee-day and powers monthly queries. `{"date": -1, "emp_code": 1}` powers listing. `{"date": 1, "late_minutes": 1}` powers the leaderboard. `{"emp_code": 1, "punch_in": -1}` powers punch-out lookups.
   - *Rejected*: A standalone index on `status` was rejected because status queries always filter by date or employee first; low-cardinality status indexing yields negligible selectivity and increases write overhead.

2. **Punch-in race.**
   Both concurrent requests locate the employee and compute the same IST attendance date. Both attempt `insert_one`. MongoDB's unique index on `(emp_code, date)` guarantees that one write succeeds and the second triggers a `DuplicateKeyError`. Our code catches this error and returns HTTP 409, yielding exactly one 201 without check-then-insert race conditions.

3. **Ties.**
   Using standard competition ranking (1, 1, 3), ties share the same rank and subsequent ranks are skipped. Because `limit` filters on `rank <= limit`, if two employees tie at rank 1 with `limit=1`, both are returned with `rank: 1`, ordered by `emp_code` ascending.

4. **Headcount.**
   The aggregation pipeline starts from `employees` matching `joined_on <= month_end`, grouping by department with `headcount: {"$sum": 1}`. Logs are joined via `$lookup`. Any employee with zero logs is fully retained and counted toward headcount.

5. **One thing you would change for 100x data.**
   Pre-aggregate daily and monthly metrics into dedicated summary collections on record closure or nightly rollup jobs, eliminating real-time multi-stage `$lookup` joins over millions of raw punch records.
