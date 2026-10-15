# DECISIONS.md

1. **Indexes.**
On `employees`, created unique `emp_code` to enforce employee identity and back primary lookups; compound `(department, 1), (emp_code, 1)` for covered department listings and aggregation filtering; and `joined_on` for headcount filtering. On `attendance_logs`, created unique compound `(emp_code, 1), (date, 1)` to enforce at most one record per employee per day and back lookups by natural key; `(date, -1), (emp_code, 1)` to support default list pagination and range scans without memory sorts; and `(emp_code, 1), (punch_in, -1)` for fast open-shift punch-out lookups. Considered and rejected a broad `(emp_code, date, status, work_hours)` index to avoid excessive write amplification on frequent punch events.

2. **Punch-in race.**
When two identical punch-in requests arrive simultaneously, both validate the employee and compute the same attendance date. Both execute `insert_one()`. MongoDB's unique compound index on `(emp_code, date)` guarantees atomicity at the storage engine level. Exactly one insert succeeds and returns HTTP 201. The concurrent insert violates the unique constraint and raises `DuplicateKeyError`, which is caught and mapped to HTTP 409 ("already punched in for this date"), preventing duplicate records without manual locks.

3. **Ties.**
The leaderboard pipeline calculates standard competition ranking using `$setWindowFields` with `$rank` ordered by `total_late_minutes` descending. Tied employees receive the same rank, and subsequent ranks are skipped (e.g. 1, 2, 2, 4). The `limit` parameter is evaluated after ranking via `$match: {rank: {$lte: limit}}`. If two employees tie at the cutoff rank, both are returned, exceeding `limit` rows as required by competition ranking rules.

4. **Headcount.**
The department summary aggregation pipeline queries the `employees` collection first, matching active employees with `joined_on` on or before month end. A `$lookup` joins corresponding records from `attendance_logs` for that month. For employees with zero logs, `$lookup` produces an empty array. We default missing log statistics to zero present days and minutes while still counting the employee document, guaranteeing accurate headcount before grouping by department.

5. **One thing you would change for 100x the data.**
At 100x volume (10 million+ records), real-time cross-collection joins and `$densify` aggregations would bottleneck memory and I/O. I would maintain pre-aggregated daily department summaries updated asynchronously on punch-out and regularization, and shard `attendance_logs` using a compound shard key `(department, date)` to localize analytical queries to specific shards.
