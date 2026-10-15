# Code Review: Employee Attendance and Analytics API

## Scope

This review records issues identified in the starter implementation of
`app/main.py`, the observable impact of each issue, and the
corresponding change in the submitted implementation. The review focuses
on API contract compliance, time calculations, input validation,
concurrent writes, database access, and error handling.

The statements below describe the changes made in the implementation.
They should be treated as verified only to the extent that the
corresponding code and tests have been checked.

## Findings

### 1. Shift-start calculations used inconsistent time zones

**Location:** `compute_late_minutes`

The starter implementation derived the shift start by replacing the hour
and minute on `punch_in`. This retained the timestamp's existing time
zone, while shift times were interpreted as IST. As a result, the two
values could represent different instants.

For example, for a 09:30 IST shift and a 10:05 IST punch-in,
interpreting the shift start as 09:30 UTC can produce a negative
duration and report no lateness.

**Change:** Build the shift window from the attendance date in IST and
compare timezone-aware instants.

### 2. The grace-period check truncated elapsed time too early

**Location:** `compute_late_minutes`

The starter implementation converted elapsed time to whole minutes
before checking the grace period. A punch-in at 09:40:01 for a 09:30
shift could be truncated to ten minutes and incorrectly treated as
within the grace period.

**Change:** Compare elapsed seconds against the 600-second threshold
first, then calculate whole late minutes according to the contract.

### 3. Overnight shifts used the wrong start date

**Location:** `compute_late_minutes`

The starter implementation assumed that the shift began on the
punch-in's calendar date. For a 22:00--06:00 shift, a punch-in at 01:00
the following morning could be compared with a shift start later that
same day.

**Change:** Determine the attendance date first, then construct the
shift window from that date. Apply the contract's timestamp precision
before calculating lateness.

### 4. Work-hour rounding did not match the contract

**Location:** `compute_work_hours`

Python's built-in `round()` uses ties-to-even rounding. The contract
requires half-up rounding. For example, 8.125 hours can round to 8.12
with ties-to-even, whereas the expected half-up result is 8.13.

**Change:** Use integer arithmetic to calculate hundredths of an hour
with the required half-up behavior.

### 5. Overtime was counted below the required minimum

**Location:** `compute_overtime`

The starter implementation counted every minute after the scheduled
shift end. A five-minute overrun therefore produced five overtime
minutes even though the contract sets a 30-minute minimum.

**Change:** Return overtime only when the calculated duration reaches
the 30-minute threshold.

### 6. Overnight shift end was placed on the wrong date

**Location:** `compute_overtime`

The starter implementation placed the shift end on the attendance
record's date even when the shift crossed midnight. For a 22:00--06:00
shift, this placed the end before the start and could produce an
excessively large overtime duration.

**Change:** Construct the shift window so the end falls on the following
day when the configured shift crosses midnight.

### 7. Naive and timezone-aware datetimes could be mixed

**Location:** Punch-in and overtime calculations

The starter implementation used naive datetimes from timestamp
conversion alongside timezone-aware IST values. Subtracting these values
raises a `TypeError` and can cause a request to fail with a server
error.

**Change:** Configure the MongoDB client to return timezone-aware UTC
datetimes and keep datetime arithmetic consistent.

### 8. Employee input was not validated

**Location:** `EmployeeIn`

The starter model did not validate key employee fields. Malformed
employee codes, invalid email addresses, impossible dates, invalid shift
times, or identical shift start and end times could be accepted.

**Change:** Add field constraints and validators for expected formats,
valid dates, and shift-time rules. Invalid requests should return 422.

### 9. Employee creation was vulnerable to duplicate requests

**Location:** `create_employee`

A separate existence check followed by an insert is not atomic.
Concurrent requests with the same employee code could both pass the
check and create duplicate records if uniqueness was not enforced by the
database.

**Change:** Enforce uniqueness with a database index and translate
duplicate-key errors into a 409 response. Do not rely on a
check-then-insert sequence for correctness.

### 10. Employee creation timestamps were inconsistent

**Location:** `create_employee`

The starter implementation stored a naive local timestamp and returned
it as an ISO-formatted string, while the API contract expects epoch
milliseconds. The stored instant could also vary with the server's time
zone.

**Change:** Store the timestamp as a UTC instant and return it in the
contract's epoch-millisecond format.

### 11. Employee pagination used an incorrect offset

**Location:** `list_employees`

The starter implementation calculated the offset as `page * page_size`.
With one-based page numbering, this skips the first page when requesting
page 1.

**Change:** Calculate the offset as `(page - 1) * page_size`.

### 12. Employee totals did not use the active filter

**Location:** `list_employees`

The returned `total` counted all employees even when the request
filtered by department. The number of returned items and the total could
therefore describe different sets of records.

**Change:** Apply the same filter to both the item query and the count
query.

### 13. Employee pagination lacked stable ordering and bounds

**Location:** `list_employees`

Without an explicit sort, records could move between pages. A page size
of zero could also cause PyMongo to return all matching records, while
negative values could cause errors.

**Change:** Sort by employee code and validate page and page-size
parameters, including an upper bound for page size.

### 14. Punch-in did not handle unknown employees

**Location:** `punch_in`

The starter implementation accessed shift details without first
confirming that the employee existed. An unknown employee could
therefore trigger an exception instead of a meaningful API response.

**Change:** Retrieve and validate the employee before using its shift
configuration. Return 404 when the employee does not exist.

### 15. Punch-in attendance dates were derived using server-local time

**Location:** `punch_in`

The starter implementation converted epoch timestamps using server-local
time and then used that date. On a server configured for UTC, a punch
shortly after midnight in IST could be stored under the previous date. A
truthiness check on `punched_at` also treated zero as if the field were
absent.

**Change:** Convert timestamps consistently, derive the attendance date
in IST using the shift rules, and distinguish an omitted value from a
supplied value.

### 16. Punch-in was vulnerable to duplicate concurrent requests

**Location:** `punch_in`

The starter implementation checked for an existing attendance record and
then inserted a new one. Two requests for the same employee and
attendance date could both pass the check before either insert
completed.

**Change:** Enforce uniqueness with a compound index on
`(emp_code, date)` and translate duplicate-key errors into a 409
response. The database constraint, rather than the preliminary read, is
the final protection against duplicate records.

### 18. Punch-in allowed unsupported status and timestamp values

**Location:** `PunchInIn`

The starter model accepted arbitrary status strings and did not strictly
enforce epoch-millisecond timestamps. An unsupported status, a
seconds-based timestamp, a float, or a numeric string could pass
validation.

**Change:** Restrict status to supported values and validate timestamps
as integers within the contract's accepted range.

### 17. Punch-in responses exposed internal database fields

**Location:** `punch_in`

The starter implementation returned the MongoDB document directly,
exposing its generated identifier and datetime values instead of the
response shape required by the API contract.

**Change:** Construct responses explicitly using a serializer that
returns the documented fields and represents timestamps as epoch
milliseconds.

### 19. Attendance listing loaded and paginated records in Python

**Location:** `list_attendance`

The starter implementation loaded all matching documents into
application memory, sorted them, and sliced the list to implement
pagination. This increases memory use and response time as the
collection grows.

**Change:** Apply filtering, sorting, skipping, and limiting in MongoDB,
supported by indexes that match the query patterns.

### 20. Attendance pagination and filtering had correctness gaps

**Location:** `list_attendance`

The starter implementation did not use a deterministic tie-breaker for
records with the same date. It also did not consistently validate page
numbers, date ranges, dates, or status values. Responses could expose
internal identifiers or omit expected fields for older documents.

**Change:** Use a stable sort by date and employee code, validate query
parameters, reject invalid date ranges with 422, and normalize response
fields through the response serializer.

### 21. The health endpoint did not check database availability

**Location:** `health`

The starter endpoint always returned success, even when MongoDB was
unavailable. This could make the service appear healthy when it could
not process requests.

**Change:** Ping MongoDB and return a service-unavailable response when
the database check fails.

### 22. Required database indexes were not created

**Location:** Application startup

The starter implementation did not create indexes for its main query
patterns. This could lead to collection scans and slower queries as the
data set grows, and it left uniqueness constraints unenforced.

**Change:** Create the required indexes during application startup using
an idempotent index-creation routine.

### 23. Database errors were not mapped to service-level responses

**Location:** Application-wide error handling

Database failures could surface as unhandled exceptions and generic 500
responses. A database outage is an availability problem rather than
necessarily an application defect.

**Change:** Handle relevant PyMongo errors and return a 503 response
when the service cannot complete a request because MongoDB is
unavailable.

## Additional implementation checks

The following areas were reviewed separately from the defects above:

-   **Environment configuration:** `load_dotenv()` does not overwrite
    environment variables that are already set, so explicitly configured
    deployment values take precedence over local defaults.
-   **Synchronous endpoint handlers:** FastAPI runs ordinary synchronous
    path operations in a thread pool. This is compatible with blocking
    PyMongo calls, provided the database client is managed
    appropriately.
-   **MongoDB document identifiers:** Inserts add an `_id` field to the
    inserted document. Explicit response serializers avoid exposing that
    internal field in API responses.
-   **Duplicate-write responses:** A 409 response is appropriate when a
    unique constraint rejects a duplicate employee code or attendance
    record.
-   **Local MongoDB defaults:** A localhost URI is useful for local
    development, but deployed environments should supply their own
    connection settings.

## Verification status

This document describes the issues and the intended corrections. A code
review is not a substitute for executing the tests. Before claiming full
compliance, run the relevant API tests against MongoDB, including
validation failures, date and time boundaries, concurrent writes,
pagination, analytics, and database-unavailable behavior. Record only
results that have actually been observed.
