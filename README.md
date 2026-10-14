# Employee Attendance & Analytics API

Implementation of the HROne Engineering Assignment. The service provides employee management, attendance lifecycle tracking (punch-in, punch-out, regularization with audit history), and MongoDB aggregation-powered analytics.

All code lives in `app/main.py` and adheres strictly to the `openapi.yaml` contract and business rules (R1–R10).

---

## Features Implemented

1. **System & Health:**
   - `GET /health`: Readiness probe checking MongoDB connectivity via administrative ping (returns 200 on success, 503 if unreachable).

2. **Employee Management:**
   - `POST /employees`: Unique `emp_code` enforcement via unique database index, regex format validation, shift validations (`shift_start != shift_end`), and integer epoch ms serialization.
   - `GET /employees`: Efficient sorted (`emp_code` asc) pagination and department filtering.

3. **Attendance Lifecycle:**
   - `POST /attendance/punch-in`: Automated IST attendance date determination with overnight shift support (R1), late minutes calculation with 10-minute grace period (R2), and atomic concurrency protection via unique `(emp_code, date)` index.
   - `POST /attendance/punch-out`: Closes recent punch-in, verifies <= 24h duration, atomic conditional update preventing race conditions, half-up work hours rounding (R4), overtime calculation with 30m threshold (R3), and half-day detection (R5).
   - `GET /attendance`: Memory-efficient paginated stream sorted by `date` desc and `emp_code` asc.
   - `PATCH /attendance/{emp_code}/{date}`: Manual regularization recomputing all derived metrics, appending immutable audit history entries (`history[].changes`), and preventing lost updates.

4. **Analytics & Aggregations:**
   - `GET /analytics/employees/{emp_code}/monthly`: Monthly aggregation calculating working days (Mon–Fri, mid-month joins), present days (half-day = 0.5), leave days, late count, total overtime, and attendance percentage.
   - `GET /analytics/departments/summary`: Multi-collection pipeline aggregating department headcount (including employees with zero logs, per R9), average work hours, total late minutes, and leave/on-duty counts.
   - `GET /analytics/leaderboard/late`: MongoDB `$setWindowFields` standard competition ranking (1, 1, 3) where limit applies post-ranking.
   - `GET /analytics/departments/{department}/trend`: Gap-free daily trend utilizing MongoDB `$densify` with exact calendar bounds, daily headcount calculation, attendance rate, and a 7-day moving average.

5. **Performance & Query Optimization:**
   - `GET /admin/explain/{endpoint}`: MongoDB execution stats verification ensuring all 5 key query and aggregation endpoints utilize `IXSCAN` with zero `COLLSCAN`.

---

## Local Setup & Running

### Prerequisites
- Python 3.10+
- MongoDB 6.0 or newer (tested with MongoDB 8.0 / 7.0; Atlas M0 also supported)

### Steps

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure environment:**
   Create `.env` or set environment variables:
   ```bash
   MONGO_URI=mongodb://localhost:27017
   MONGO_DB=attendance_db
   ```

3. **Seed database:**
   ```bash
   python sample_seed.py
   ```

4. **Run the API server:**
   ```bash
   uvicorn app.main:app --port 8000 --reload
   ```

5. **Run test suite:**
   ```bash
   python tests_run.py
   ```

---

## Key Files

- `app/main.py`: Complete application code and database index initialization.
- `requirements.txt`: Minimal required Python packages.
- `REVIEW.md`: Detailed defect analysis of starter code with reproductions and fixes.
- `DECISIONS.md`: Concise rationale for indexes, race condition handling, tie-breaking, and headcount tracking.
