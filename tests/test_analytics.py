"""Analytics checked against an independent, deliberately naive Python implementation of the rules, on
random data containing ties, no-log employees, weekend records, joiners, overnight shifts, legacy records and
logs of unknown employees. Plus hand-computed cases for ties and the trend window."""
import random
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest

from tests.datagen import make_employees, make_logs

PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
FIRST = date(2026, 6, 1)


def r(value, places):
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def month_range(month):
    first = date.fromisoformat(month + "-01")
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, nxt - timedelta(days=1)


def present_value(log):
    return 0.5 if log.get("half_day") else 1


def weekday(d: str):
    return date.fromisoformat(d).weekday() < 5


# ---------------------------------------------------------------- reference implementations
def ref_monthly(emp, logs, month):
    first, last = month_range(month)
    mine = [l for l in logs if l["emp_code"] == emp["emp_code"] and first.isoformat() <= l["date"] <= last.isoformat()]
    start = max(first, date.fromisoformat(emp["joined_on"]))
    working = sum(1 for i in range((last - start).days + 1) if (start + timedelta(i)).weekday() < 5) if start <= last else 0
    present = sum(present_value(l) for l in mine if l["status"] in PRESENCE and weekday(l["date"]))
    return {
        "emp_code": emp["emp_code"], "month": month, "working_days": working, "present_days": present,
        "leave_days": sum(l["status"] == "LEAVE" for l in mine),
        "late_count": sum((l.get("late_minutes") or 0) > 0 for l in mine),
        "total_late_minutes": sum(l.get("late_minutes") or 0 for l in mine),
        "total_overtime_minutes": sum(l.get("overtime_minutes") or 0 for l in mine),
        "attendance_pct": r(Decimal(str(present)) * 100 / working, 2) if working else None,
    }


def ref_summary(emps, logs, month, department=None):
    first, last = month_range(month)
    out = {}
    for e in emps:
        if e["joined_on"] > last.isoformat() or (department is not None and e["department"] != department):
            continue
        d = out.setdefault(e["department"], {"department": e["department"], "headcount": 0, "present_days": 0,
                                             "wh": [], "late_count": 0, "total_late_minutes": 0,
                                             "leave_count": 0, "on_duty_count": 0})
        d["headcount"] += 1
        for l in logs:
            if l["emp_code"] != e["emp_code"] or not (first.isoformat() <= l["date"] <= last.isoformat()):
                continue
            if l["status"] in PRESENCE and weekday(l["date"]):
                d["present_days"] += present_value(l)
            if l["status"] in PRESENCE and l.get("work_hours") is not None:
                d["wh"].append(Decimal(str(l["work_hours"])))
            if (l.get("late_minutes") or 0) > 0:
                d["late_count"] += 1
                d["total_late_minutes"] += l["late_minutes"]
            d["leave_count"] += l["status"] == "LEAVE"
            d["on_duty_count"] += l["status"] == "ON_DUTY"
    items = []
    for name in sorted(out):
        d = out[name]
        wh = d.pop("wh")
        d["avg_work_hours"] = r(sum(wh) / len(wh), 2) if wh else None
        items.append(d)
    return {"month": month, "items": items}


def ref_leaderboard(emps, logs, month, limit=10, department=None):
    first, last = month_range(month)
    by_code = {e["emp_code"]: e for e in emps}
    totals = {}
    for l in logs:
        e = by_code.get(l["emp_code"])
        if e is None or not (first.isoformat() <= l["date"] <= last.isoformat()) or not (l.get("late_minutes") or 0):
            continue
        if department is not None and e["department"] != department:
            continue
        t = totals.setdefault(l["emp_code"], [0, 0])
        t[0] += l["late_minutes"]
        t[1] += 1
    rows = []
    for code, (total, count) in totals.items():
        rank = 1 + sum(1 for other, _ in totals.values() if other > total)
        if rank <= limit:
            e = by_code[code]
            rows.append({"rank": rank, "emp_code": code, "name": e["name"], "department": e["department"],
                         "total_late_minutes": total, "late_count": count})
    rows.sort(key=lambda x: (-x["total_late_minutes"], x["emp_code"]))
    return {"month": month, "items": rows}


def ref_trend(emps, logs, department, first, last):
    members = {e["emp_code"]: e for e in emps if e["department"] == department}
    rows, rates = [], []
    day = first
    while day <= last:
        iso = day.isoformat()
        headcount = sum(1 for e in members.values() if e["joined_on"] <= iso)
        todays = [l for l in logs if l["emp_code"] in members and l["date"] == iso]
        present = sum(present_value(l) for l in todays if l["status"] in PRESENCE)
        working = day.weekday() < 5
        rate = r(Decimal(str(present)) / headcount, 4) if working and headcount else None
        rates.append(rate)
        window = [x for x in rates[-7:] if x is not None]
        rows.append({"date": iso, "is_working_day": working, "headcount": headcount, "present_count": present,
                     "late_count": sum((l.get("late_minutes") or 0) > 0 for l in todays), "attendance_rate": rate,
                     "moving_avg_7d": r(sum(Decimal(str(x)) for x in window) / len(window), 4) if window else None})
        day += timedelta(days=1)
    return {"department": department, "items": rows}


# ---------------------------------------------------------------- random dataset comparison
@pytest.fixture()
def dataset(db):
    rng = random.Random(20260711)
    emps = make_employees(60, FIRST, rng)
    logs = make_logs(emps, FIRST, 75, rng)
    db.employees.insert_many([dict(e) for e in emps])
    db.attendance_logs.insert_many([dict(l) for l in logs])
    return emps, logs


def test_employee_monthly_matches_reference(api, dataset):
    emps, logs = dataset
    for e in emps:
        for month in ("2026-06", "2026-07", "2026-08", "2025-12"):
            got = api.get(f"/analytics/employees/{e['emp_code']}/monthly", params={"month": month}).json()
            assert got == ref_monthly(e, logs, month), (e["emp_code"], month)


def test_department_summary_matches_reference(api, dataset):
    emps, logs = dataset
    for month in ("2026-06", "2026-07", "2026-08"):
        assert api.get("/analytics/departments/summary", params={"month": month}).json() == ref_summary(emps, logs, month)
        for dept in ("Sales", "engineering", "Nope"):
            got = api.get("/analytics/departments/summary", params={"month": month, "department": dept}).json()
            assert got == ref_summary(emps, logs, month, dept)


def test_leaderboard_matches_reference(api, dataset):
    emps, logs = dataset
    for month in ("2026-06", "2026-07"):
        for limit in (1, 2, 3, 5, 10, 50):
            for dept in (None, "Support"):
                params = {"month": month, "limit": limit, **({"department": dept} if dept else {})}
                got = api.get("/analytics/leaderboard/late", params=params).json()
                assert got == ref_leaderboard(emps, logs, month, limit, dept), params


def test_trend_matches_reference(api, dataset):
    emps, logs = dataset
    for dept, first, last in (("Sales", date(2026, 6, 1), date(2026, 8, 14)),
                              ("Finance", date(2026, 6, 20), date(2026, 6, 20)),
                              ("Engineering", date(2026, 5, 25), date(2026, 6, 9)),
                              ("engineering", date(2026, 6, 1), date(2026, 8, 31))):
        got = api.get(f"/analytics/departments/{dept}/trend",
                      params={"from": first.isoformat(), "to": last.isoformat()}).json()
        assert got == ref_trend(emps, logs, dept, first, last), dept


# ---------------------------------------------------------------- hand-computed edge cases
def seed(db, emps, logs):
    base = {"name": "N", "email": "n@x.io", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"}
    db.employees.insert_many([{**base, **e} for e in emps])
    if logs:
        db.attendance_logs.insert_many([{"status": "PRESENT", "late_minutes": 0, **l} for l in logs])


def test_leaderboard_ties_share_rank_and_limit_applies_to_rank(api, db):
    seed(db, [{"emp_code": f"EMP000{i}", "department": "D"} for i in range(1, 6)], [
        {"emp_code": "EMP0005", "date": "2026-07-01", "late_minutes": 50},
        {"emp_code": "EMP0002", "date": "2026-07-01", "late_minutes": 30},
        {"emp_code": "EMP0004", "date": "2026-07-01", "late_minutes": 30},
        {"emp_code": "EMP0001", "date": "2026-07-01", "late_minutes": 20},
        {"emp_code": "EMP0003", "date": "2026-07-01", "late_minutes": 0},
    ])
    items = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2}).json()["items"]
    assert [(i["rank"], i["emp_code"]) for i in items] == [(1, "EMP0005"), (2, "EMP0002"), (2, "EMP0004")]
    items = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 3}).json()["items"]
    assert [(i["rank"], i["emp_code"]) for i in items] == [(1, "EMP0005"), (2, "EMP0002"), (2, "EMP0004"),
                                                           (4, "EMP0001")][:3]
    items = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 4}).json()["items"]
    assert [i["rank"] for i in items] == [1, 2, 2, 4]


def test_summary_counts_employees_without_logs_and_skips_future_joiners(api, db):
    seed(db, [{"emp_code": "EMP0001", "department": "D"}, {"emp_code": "EMP0002", "department": "D"},
              {"emp_code": "EMP0003", "department": "D", "joined_on": "2026-08-01"},
              {"emp_code": "EMP0004", "department": "Later", "joined_on": "2026-08-01"}],
         [{"emp_code": "EMP0001", "date": "2026-07-04", "work_hours": 9.0},          # Saturday
          {"emp_code": "EMP0001", "date": "2026-07-06", "work_hours": 4.0, "half_day": True}])
    (item,) = api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"]
    assert item["department"] == "D" and item["headcount"] == 2
    assert item["present_days"] == 0.5 and item["avg_work_hours"] == 6.5   # weekend work still in the average


def test_trend_window_and_404(api, db):
    seed(db, [{"emp_code": "EMP0001", "department": "D"}, {"emp_code": "EMP0002", "department": "D",
                                                             "joined_on": "2026-07-08"}],
         [{"emp_code": "EMP0001", "date": "2026-07-06"}, {"emp_code": "EMP0001", "date": "2026-07-11"}])
    items = api.get("/analytics/departments/D/trend", params={"from": "2026-07-06", "to": "2026-07-13"}).json()["items"]
    assert [i["headcount"] for i in items] == [1, 1, 2, 2, 2, 2, 2, 2]
    assert [i["attendance_rate"] for i in items] == [1.0, 0.0, 0.0, 0.0, 0.0, None, None, 0.0]
    assert items[5]["present_count"] == 1 and items[5]["is_working_day"] is False
    assert items[1]["moving_avg_7d"] == 0.5 and items[7]["moving_avg_7d"] == 0.0
    assert api.get("/analytics/departments/Nope/trend", params={"from": "2026-07-06", "to": "2026-07-07"}).status_code == 404
    for params in ({"from": "2026-07-07", "to": "2026-07-06"}, {"from": "2026-07-01", "to": "2026-10-01"},
                   {"from": "2026-07-01"}, {"from": "bad", "to": "2026-07-02"}):
        assert api.get("/analytics/departments/D/trend", params=params).status_code == 422
    assert api.get("/analytics/departments/D/trend", params={"from": "2026-07-01", "to": "2026-09-30"}).status_code == 200


def test_analytics_validation(api, db):
    seed(db, [{"emp_code": "EMP0001", "department": "D"}], [])
    assert api.get("/analytics/employees/EMP0404/monthly", params={"month": "2026-07"}).status_code == 404
    for month in ("2026-13", "2026-7", "July", None):
        params = {"month": month} if month else {}
        assert api.get("/analytics/employees/EMP0001/monthly", params=params).status_code == 422
        assert api.get("/analytics/departments/summary", params=params).status_code == 422
        assert api.get("/analytics/leaderboard/late", params=params).status_code == 422
    for limit in (0, 51):
        assert api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": limit}).status_code == 422
    monthly = api.get("/analytics/employees/EMP0001/monthly", params={"month": "2025-12"}).json()
    assert monthly["working_days"] == 0 and monthly["attendance_pct"] is None


# ---------------------------------------------------------------- explain
@pytest.mark.parametrize("endpoint,params", [
    ("attendance_list", {}),
    ("attendance_list", {"emp_code": "EMP0003", "date_from": "2026-06-01"}),
    ("attendance_list", {"status": "LEAVE", "page": 2}),
    ("employee_monthly", {"emp_code": "EMP0003", "month": "2026-07"}),
    ("department_summary", {"month": "2026-07"}),
    ("department_summary", {"month": "2026-07", "department": "Sales"}),
    ("late_leaderboard", {"month": "2026-07"}),
    ("late_leaderboard", {"month": "2026-07", "department": "Sales", "limit": 3}),
    ("department_trend", {"department": "Sales", "from": "2026-06-01", "to": "2026-07-31"}),
])
def test_explain_uses_indexes(api, dataset, endpoint, params):
    r = api.get(f"/admin/explain/{endpoint}", params=params)
    assert r.status_code == 200
    body = r.json()
    assert body["endpoint"] == endpoint and body["collection"] in ("employees", "attendance_logs")
    text = str(body["explain"])
    assert "IXSCAN" in text and "COLLSCAN" not in text


def test_explain_validation(api, dataset):
    assert api.get("/admin/explain/everything").status_code == 422
    assert api.get("/admin/explain/employee_monthly", params={"month": "2026-07"}).status_code == 422
    assert api.get("/admin/explain/department_trend", params={"department": "Sales"}).status_code == 422
    assert api.get("/admin/explain/late_leaderboard").status_code == 422
