# Employee Attendance and Analytics API

FastAPI backend for tracking employee attendance, punch-in/out lifecycles, record regularizations with audit trails, and reporting departmental metrics using MongoDB aggregation pipelines.

## Prerequisites

- Python 3.10 or newer (tested with Python 3.10 and 3.11).
- MongoDB 6.0 or newer (MongoDB 7.0 supported; local Docker or MongoDB Atlas).
- Git.

## Setup and Installation

### 1. Clone the repository
```powershell
git clone https://github.com/MunishUpadhyay/Employee-Attendance-and-Analytics-API.git
cd Employee-Attendance-and-Analytics-API
```

### 2. Create and activate a virtual environment
On Windows (PowerShell):
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

On Linux or macOS:
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies
```powershell
pip install -r requirements.txt
```

Verify that all dependencies installed cleanly:
```powershell
python -m pip check
```

### 4. Configure environment variables
The service reads connection settings from system environment variables or a local `.env` file. System environment variables take precedence over `.env`.

- `MONGO_URI`: MongoDB connection string (default: `mongodb://localhost:27017`).
- `MONGO_DB`: Database name (default: `attendance_db`).

In PowerShell:
```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
```

## Seeding Sample Data

The repository includes nine sample attendance records and six employee records in `sample_data/`. To load them into your database:

```powershell
python sample_seed.py
```

*Note: Running `sample_seed.py` clears existing documents in `employees` and `attendance_logs` before inserting the starter sample.*

## Running the API

Start the service with Uvicorn from the project root:

```powershell
uvicorn app.main:app --port 8000 --reload
```

The application creates all required MongoDB indexes idempotently during startup via the FastAPI lifespan handler.

Once running, interactive documentation is available at:
- Swagger UI: [http://localhost:8000/docs](http://localhost:8000/docs)
- ReDoc: [http://localhost:8000/redoc](http://localhost:8000/redoc)

## Running Tests

Run the full automated test suite using Python's built-in `unittest` runner:

```powershell
python -m unittest discover -s tests -v
```

All test cases generate dynamic, isolated employee identifiers and clean up their own test data, leaving the sample database intact.

## Implemented API Endpoints

### System
- `GET /health`: Readiness probe returning HTTP 200 when MongoDB responds to a ping, or HTTP 503 if unavailable.

### Employee Management
- `POST /employees`: Create employee records with unique `emp_code` (pattern `^EMP\d{4,6}$`), validated shift times, and automatic server timestamping.
- `GET /employees`: Paginated employee list, sorted by `emp_code` ascending, with optional case-sensitive `department` filter.

### Attendance Lifecycle
- `POST /attendance/punch-in`: Records punch-in for the day. Computes attendance date and initial lateness (R1, R2). Enforces uniqueness per `(emp_code, date)` to handle concurrent punch-in attempts safely.
- `POST /attendance/punch-out`: Closes the open shift. Computes total work hours (R4), overtime (R3), and half-day status (R5). Uses atomic updates to reject duplicate or concurrent punch-outs.
- `GET /attendance`: Paginated attendance history, sorted by `date` descending then `emp_code` ascending. Supports filtering by `emp_code`, `status`, and date ranges (`date_from`, `date_to`).
- `PATCH /attendance/{emp_code}/{date}`: Regularizes a day's record. Recomputes derived fields and appends an audit history entry with exact field-level diffs (`{from, to}`). Uses optimistic concurrency to prevent losing history entries.

### Analytics and Administration
- `GET /analytics/employees/{emp_code}/monthly`: Aggregates monthly working days, present days, leave days, late count, total late minutes, overtime, and rounded attendance percentage for an employee.
- `GET /analytics/departments/summary`: Department-level summary for a given month. Includes active employees with no attendance logs in the headcount.
- `GET /analytics/leaderboard/late`: Ranks late employees using standard competition ranking (`$rank`), properly handling ties at the `limit` cutoff.
- `GET /analytics/departments/{department}/trend`: Daily attendance series across a date range (up to 92 days) using `$densify` and `$fill`, computing 7-day trailing moving averages.
- `GET /admin/explain/{endpoint}`: Returns MongoDB `explain("executionStats")` diagnostics for the main query or pipeline of the target endpoint, confirming index usage (`IXSCAN`) and absence of collection scans (`COLLSCAN`).

## Business Rules and Boundaries

- **Timezone and Dates (R1):** Shift schedules and attendance dates operate in Indian Standard Time (IST, UTC+05:30). Instants are normalized to whole seconds. For overnight shifts (`shift_end <= shift_start`), punch-ins before `shift_end` belong to the previous calendar day's shift.
- **Lateness (R2):** 10-minute grace period. Punch-ins strictly exceeding 10 minutes past shift start are marked late, with lateness equal to the floor of total elapsed minutes since shift start.
- **Overtime (R3):** Counted only when post-shift work is at least 30 minutes; otherwise 0.
- **Work Hours and Rounding (R4, R8):** Duration in seconds divided by 3600, rounded to 2 decimal places using true `ROUND_HALF_UP`. Rates are rounded to 4 decimal places half-up.
- **Half Day (R5):** Rounded work hours strictly under 4.50 hours count as a half day (0.5 present days).
- **Presence Statuses (R6):** `PRESENT`, `WFH`, and `ON_DUTY` count toward attendance; `ABSENT` and `LEAVE` do not.
- **Working Days (R7):** Monday through Friday. Mid-month joiners count working days starting from their `joined_on` date. Weekend logs contribute to overtime and late totals, but are excluded from weekday present days.
- **Pagination (R10):** Pages are 1-indexed. Default page size is 20, with a maximum of 100.

## Known Limitations and Considerations

- **Single-Node Operations:** Aggregation pipelines currently run on a single MongoDB replica set or standalone node. For datasets with tens of millions of documents, sharding on `(department, date)` and asynchronous pre-aggregation of daily summary metrics would be advisable.
- **No In-Band Authentication:** Per the assignment specification, authentication and role checks are omitted; user identity in regularizations is provided via the `regularized_by` body parameter.
