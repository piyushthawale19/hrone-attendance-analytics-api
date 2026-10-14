import os
import sys
from datetime import datetime, timezone
from fastapi.testclient import TestClient

from app.main import app, init_db, db

init_db()
client = TestClient(app)

def test_all():
    print("=== 1. Health Probe ===")
    r = client.get("/health")
    assert r.status_code == 200, f"Health check failed: {r.text}"
    assert r.json() == {"status": "ok"}
    print("Health check PASSED")

    print("\n=== 2. Employee Endpoints ===")
    # List employees
    r = client.get("/employees")
    assert r.status_code == 200, r.text
    data = r.json()
    assert "items" in data and "total" in data
    assert data["page"] == 1
    assert data["page_size"] == 20
    emp_codes = [e["emp_code"] for e in data["items"]]
    assert emp_codes == sorted(emp_codes), "Employees must be sorted by emp_code ascending"
    print(f"List employees passed, found {data['total']} employees")

    # Filter by department
    r = client.get("/employees?department=Engineering")
    assert r.status_code == 200
    eng_items = r.json()["items"]
    assert all(e["department"] == "Engineering" for e in eng_items)
    assert r.json()["total"] == len(eng_items)
    print("List employees department filter passed")

    # Create employee valid
    new_emp = {
        "emp_code": "EMP9999",
        "name": "Test User",
        "email": "test@example.com",
        "department": "QA",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-06-01"
    }
    # Clean up if existed
    db.employees.delete_one({"emp_code": "EMP9999"})
    db.attendance_logs.delete_many({"emp_code": "EMP9999"})

    r = client.post("/employees", json=new_emp)
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["emp_code"] == "EMP9999"
    assert isinstance(created["created_at"], int)
    assert "_id" not in created
    print("Create employee passed")

    # Create duplicate employee -> 409
    r = client.post("/employees", json=new_emp)
    assert r.status_code == 409, f"Expected 409 for duplicate, got {r.status_code}"
    print("Duplicate employee 409 passed")

    # Create with shift_start == shift_end -> 422
    bad_emp = dict(new_emp, emp_code="EMP9998", shift_end="09:30")
    r = client.post("/employees", json=bad_emp)
    assert r.status_code == 422, f"Expected 422, got {r.status_code}"
    print("Shift start == shift end 422 passed")

    print("\n=== 3. Attendance Punch-In and Punch-Out ===")
    # Punch in with invalid emp_code -> 404
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0000"})
    assert r.status_code == 404

    # Punch in on 2026-07-20 09:40:01 IST -> 10 late minutes
    # 2026-07-20 09:40:01 IST is 04:10:01 UTC = 1784520601000
    # Let's compute epoch ms:
    # 2026-07-20 04:10:01 UTC:
    dt_utc = datetime(2026, 7, 20, 4, 10, 1, tzinfo=timezone.utc)
    ms_in = int(dt_utc.timestamp() * 1000)
    r = client.post("/attendance/punch-in", json={
        "emp_code": "EMP9999",
        "punched_at": ms_in,
        "status": "PRESENT"
    })
    assert r.status_code == 201, r.text
    punch_doc = r.json()
    assert punch_doc["date"] == "2026-07-20"
    assert punch_doc["late_minutes"] == 10
    assert punch_doc["punch_out"] is None
    assert punch_doc["work_hours"] is None
    print("Punch-in with 10 late minutes passed")

    # Duplicate punch-in for same date -> 409
    r = client.post("/attendance/punch-in", json={
        "emp_code": "EMP9999",
        "punched_at": ms_in + 1000,
        "status": "PRESENT"
    })
    assert r.status_code == 409, f"Expected 409, got {r.status_code}"
    print("Duplicate punch-in 409 passed")

    # Punch-out before punch-in -> 422
    r = client.post("/attendance/punch-out", json={
        "emp_code": "EMP9999",
        "punched_at": ms_in - 5000
    })
    assert r.status_code == 422, f"Expected 422, got {r.status_code}"

    # Punch-out > 24 hours -> 422
    r = client.post("/attendance/punch-out", json={
        "emp_code": "EMP9999",
        "punched_at": ms_in + 25 * 3600 * 1000
    })
    assert r.status_code == 422, f"Expected 422, got {r.status_code}"

    # Valid Punch-out at 19:15:00 IST (13:45:00 UTC) -> 45 min overtime (>= 30 min)
    dt_out = datetime(2026, 7, 20, 13, 45, 0, tzinfo=timezone.utc)
    ms_out = int(dt_out.timestamp() * 1000)
    r = client.post("/attendance/punch-out", json={
        "emp_code": "EMP9999",
        "punched_at": ms_out
    })
    assert r.status_code == 200, r.text
    out_doc = r.json()
    assert out_doc["punch_out"] == ms_out
    assert out_doc["overtime_minutes"] == 45
    assert out_doc["half_day"] is False
    assert out_doc["work_hours"] == 9.58
    print("Punch-out passed with overtime 45m and work_hours 9.58")

    # Second punch-out -> 409
    r = client.post("/attendance/punch-out", json={
        "emp_code": "EMP9999",
        "punched_at": ms_out
    })
    assert r.status_code == 409, f"Expected 409 for second punch-out, got {r.status_code}"
    print("Second punch-out 409 passed")

    print("\n=== 4. Attendance List Endpoints ===")
    r = client.get("/attendance?date_from=2026-07-21&date_to=2026-07-20")
    assert r.status_code == 422, "date_from > date_to must be 422"

    r = client.get("/attendance?emp_code=EMP9999")
    assert r.status_code == 200
    assert len(r.json()["items"]) == 1
    print("Attendance list passed")

    print("\n=== 5. Regularize (PATCH) ===")
    # Patch with no change -> 422
    r = client.patch("/attendance/EMP9999/2026-07-20", json={
        "reason": "testing nothing changed",
        "regularized_by": "admin"
    })
    assert r.status_code == 422, f"Expected 422 for no change, got {r.status_code}"

    # Patch punch_in to 09:30:00 IST (04:00:00 UTC) -> late_minutes should become 0
    dt_in_new = datetime(2026, 7, 20, 4, 0, 0, tzinfo=timezone.utc)
    ms_in_new = int(dt_in_new.timestamp() * 1000)
    r = client.patch("/attendance/EMP9999/2026-07-20", json={
        "punch_in": ms_in_new,
        "reason": "corrected punch in time",
        "regularized_by": "admin"
    })
    assert r.status_code == 200, r.text
    patched = r.json()
    assert patched["late_minutes"] == 0
    assert len(patched["history"]) == 1
    h = patched["history"][0]
    assert h["by"] == "admin"
    assert "late_minutes" in h["changes"]
    assert h["changes"]["late_minutes"]["from"] == 10
    assert h["changes"]["late_minutes"]["to"] == 0
    print("Patch regularization and audit history passed")

    print("\n=== 6. Monthly Employee Analytics ===")
    r = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert r.status_code == 200, r.text
    monthly = r.json()
    assert monthly["emp_code"] == "EMP0001"
    assert monthly["month"] == "2026-07"
    assert monthly["working_days"] == 23  # July 2026 has 23 weekdays
    assert monthly["present_days"] >= 1.0
    print(f"Monthly summary passed: {monthly}")

    print("\n=== 7. Department Summary Analytics ===")
    r = client.get("/analytics/departments/summary?month=2026-07")
    assert r.status_code == 200, r.text
    dept_summary = r.json()
    assert dept_summary["month"] == "2026-07"
    depts = [d["department"] for d in dept_summary["items"]]
    assert depts == sorted(depts)
    print(f"Department summary passed: {len(dept_summary['items'])} departments")

    print("\n=== 8. Late Leaderboard Analytics ===")
    r = client.get("/analytics/leaderboard/late?month=2026-07&limit=5")
    assert r.status_code == 200, r.text
    lb = r.json()
    assert "items" in lb
    ranks = [i["rank"] for i in lb["items"]]
    print(f"Leaderboard items: {lb['items']}")
    print("Leaderboard passed")

    print("\n=== 9. Department Trend Analytics ===")
    r = client.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-12")
    assert r.status_code == 200, r.text
    trend = r.json()
    assert trend["department"] == "Engineering"
    assert len(trend["items"]) == 7  # 7 calendar days
    for day in trend["items"]:
        assert "date" in day
        assert "is_working_day" in day
        assert "attendance_rate" in day
        assert "moving_avg_7d" in day
    print("Department trend passed with exactly 7 densified rows")

    print("\n=== 10. Admin Explain Endpoints ===")
    endpoints_to_test = [
        ("attendance_list", "?date_from=2026-07-01&date_to=2026-07-31"),
        ("employee_monthly", "?emp_code=EMP0001&month=2026-07"),
        ("department_summary", "?month=2026-07"),
        ("late_leaderboard", "?month=2026-07&limit=10"),
        ("department_trend", "?department=Engineering&from=2026-07-06&to=2026-07-12"),
    ]

    for ep_name, qs in endpoints_to_test:
        r = client.get(f"/admin/explain/{ep_name}{qs}")
        assert r.status_code == 200, f"Explain failed for {ep_name}: {r.text}"
        data = r.json()
        assert data["endpoint"] == ep_name
        assert "collection" in data
        assert "explain" in data
        exp_str = str(data["explain"])
        assert "IXSCAN" in exp_str, f"Explain for {ep_name} does not contain IXSCAN!"
        assert "COLLSCAN" not in exp_str, f"Explain for {ep_name} contains COLLSCAN!"
        print(f"Explain {ep_name} PASSED: uses IXSCAN and NO COLLSCAN on {data['collection']}")

    print("\nALL TESTS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    test_all()
