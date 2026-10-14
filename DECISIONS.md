# DECISIONS.md

1. **Indexes.**
   - Unique `employees.emp_code` and unique `attendance_logs(emp_code, date)` turn the "exactly one" rules into database guarantees. The second also serves every per-employee date range and the analytics joins.
   - `(date desc, emp_code asc)` matches the list sort and the month scans. `(status, date desc, emp_code)` covers status filters, `(emp_code, punch_in desc)` finds the record to punch out, and `employees(department, emp_code)` and `employees(joined_on)` serve the department queries.
   - I rejected a separate `emp_code` index on logs, because the compound index's prefix already covers it.

2. **Punch-in race.** Both requests compute the same `(emp_code, date)` and call `insert_one`. The unique index lets exactly one insert succeed. The other gets `DuplicateKeyError`, which I return as 409. There is no check-then-insert.

3. **Ties.** `$setWindowFields` with `$rank` gives 1, 2, 2, 4, and I filter on `rank <= limit`. Everyone tied at the cutoff is returned, so the list can be longer than `limit`.

4. **Headcount.** The pipeline starts from `employees` with `joined_on <= month end`. Every employee is a row with headcount 1, and log statistics are joined onto it. Someone with no logs still counts.

5. **100x data.** The analytics collect one month's emp_codes into a single document, which would approach the 16 MB limit. I would precompute daily per-employee summaries when records are written, then aggregate those.
