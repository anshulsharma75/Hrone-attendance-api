# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code`; `(department, emp_code)` for filtered, sorted lists and the trend; `joined_on` for headcount. `attendance_logs`: unique `(emp_code, date)` (one record per day, per-employee reads, lookups); `(date desc, emp_code)` for the list sort and month scans; `(status, date desc, emp_code)` for status filters; `(emp_code, punch_in desc)` for punch-out. Rejected: an index on `late_minutes`, because the month range already narrows the data.
2. **Punch-in race.** Both requests read the employee and compute the same date, then both call `insert_one`. The unique `(emp_code, date)` index lets MongoDB accept one; the other gets `DuplicateKeyError`, which I turn into 409. No check-then-insert.
3. **Ties.** `$rank` gives 1, 2, 2, 4 and `limit` is applied after ranking, so everyone tied at the cutoff is returned and the list can be longer than `limit`.
4. **Headcount.** The summary starts from `employees` (joined on or before month end) and left-joins logs with `$lookup`. An employee with no logs gets an empty array but still adds 1 to headcount.
5. **At 100x.** Pre-compute monthly totals per employee when attendance is written, and use keyset pagination instead of `skip`.
