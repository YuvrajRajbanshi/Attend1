"""Behaviour tests: contract, business rules R1-R5, validation, pagination and concurrency."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

IST = timezone(timedelta(hours=5, minutes=30))


def ist(y, mo, d, h, mi, s=0, ms=0) -> int:
    """An IST wall-clock time as epoch milliseconds."""
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp()) * 1000 + ms


def employee(code="EMP0001", **kw):
    return {"emp_code": code, "name": "Asha", "email": "asha@example.com", "department": "Engineering",
            "joined_on": "2026-01-05", **kw}


@pytest.fixture()
def emp(api):
    assert api.post("/employees", json=employee()).status_code == 201
    return "EMP0001"


@pytest.fixture()
def night(api):
    assert api.post("/employees", json=employee("EMP0002", shift_start="22:00", shift_end="06:00")).status_code == 201
    return "EMP0002"


def punch_in(api, code, ms, **kw):
    return api.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms, **kw})


def punch_out(api, code, ms):
    return api.post("/attendance/punch-out", json={"emp_code": code, "punched_at": ms})


# --------------------------------------------------------------------------- employees
def test_health(api):
    assert api.get("/health").json() == {"status": "ok"}


def test_create_employee_returns_contract_shape(api):
    r = api.post("/employees", json=employee(created_at=1, work="ignored"))
    assert r.status_code == 201
    body = r.json()
    assert set(body) == {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on",
                         "created_at"}
    assert body["shift_start"] == "09:30" and body["shift_end"] == "18:30"
    assert isinstance(body["created_at"], int) and body["created_at"] > 1_700_000_000_000


@pytest.mark.parametrize("override", [
    {"emp_code": "E1"}, {"emp_code": "EMP123"}, {"emp_code": "EMP1234567"}, {"email": "nope"},
    {"shift_start": "24:00"}, {"shift_end": "9:30"}, {"shift_start": "10:00", "shift_end": "10:00"},
    {"joined_on": "2026-02-30"}, {"joined_on": "20260105"}, {"name": ""}, {"department": "x" * 51},
    {"name": 5},
])
def test_create_employee_validation(api, override):
    assert api.post("/employees", json=employee(**override)).status_code == 422


def test_duplicate_employee_409(api, emp):
    assert api.post("/employees", json=employee()).status_code == 409


def test_concurrent_employee_create_exactly_one_201(api):
    with ThreadPoolExecutor(10) as pool:
        codes = list(pool.map(lambda _: api.post("/employees", json=employee("EMP0777")).status_code, range(10)))
    assert sorted(codes) == [201] + [409] * 9


def test_list_employees_filter_sort_pagination(api):
    for i in (5, 3, 1, 4, 2):
        api.post("/employees", json=employee(f"EMP000{i}", department="Sales" if i % 2 else "Ops"))
    r = api.get("/employees", params={"department": "Sales", "page": 1, "page_size": 2}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0001", "EMP0003"]
    assert r["total"] == 3 and r["page"] == 1 and r["page_size"] == 2
    r = api.get("/employees", params={"department": "Sales", "page": 2, "page_size": 2}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0005"]
    assert api.get("/employees", params={"department": "sales"}).json()["total"] == 0  # case-sensitive
    for bad in ({"page": 0}, {"page_size": 0}, {"page_size": 101}, {"page": "x"}):
        assert api.get("/employees", params=bad).status_code == 422


# --------------------------------------------------------------------------- punch-in, R1, R2
@pytest.mark.parametrize("hms,late", [((9, 40, 0), 0), ((9, 40, 1), 10), ((10, 15, 59), 45), ((9, 0, 0), 0)])
def test_late_minutes_r2(api, emp, hms, late):
    r = punch_in(api, emp, ist(2026, 7, 6, *hms))
    assert r.status_code == 201
    assert r.json()["late_minutes"] == late


def test_punch_in_truncates_to_seconds_and_record_shape(api, emp):
    r = punch_in(api, emp, ist(2026, 7, 6, 9, 40, 0, ms=900), status="WFH")
    body = r.json()
    assert body == {"emp_code": emp, "date": "2026-07-06", "status": "WFH", "punch_in": ist(2026, 7, 6, 9, 40),
                    "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0,
                    "half_day": False, "history": []}


def test_attendance_date_is_ist(api, emp):
    # 00:30 IST on the 7th is still the 6th in UTC; the record belongs to the 7th.
    assert punch_in(api, emp, ist(2026, 7, 7, 0, 30)).json()["date"] == "2026-07-07"


def test_overnight_shift_r1(api, night):
    r = punch_in(api, night, ist(2026, 7, 6, 21, 55))
    assert r.json()["date"] == "2026-07-06" and r.json()["late_minutes"] == 0
    # 01:00 on the 8th is before shift_end (06:00): belongs to the shift that started on the 7th.
    r = punch_in(api, night, ist(2026, 7, 8, 1, 0))
    assert r.json()["date"] == "2026-07-07" and r.json()["late_minutes"] == 180
    # 06:00 exactly is not earlier than shift_end: belongs to the 8th.
    assert punch_in(api, night, ist(2026, 7, 8, 6, 0)).json()["date"] == "2026-07-08"


def test_overnight_punch_out_across_midnight(api, night):
    punch_in(api, night, ist(2026, 7, 6, 21, 55))
    r = punch_out(api, night, ist(2026, 7, 7, 6, 40))
    assert r.status_code == 200
    assert r.json()["work_hours"] == 8.75 and r.json()["overtime_minutes"] == 40 and r.json()["date"] == "2026-07-06"


@pytest.mark.parametrize("payload", [
    {"punched_at": 1783312500},            # seconds
    {"punched_at": 1783312500000.0},       # float
    {"punched_at": "1783312500000"},       # string
    {"punched_at": True},
    {"punched_at": 4102444800001},         # out of range
    {"status": "ABSENT"}, {"status": "LEAVE"}, {"status": "present"},
])
def test_punch_in_validation(api, emp, payload):
    assert api.post("/attendance/punch-in", json={"emp_code": emp, **payload}).status_code == 422


def test_punch_in_unknown_employee_404(api):
    assert punch_in(api, "EMP4040", ist(2026, 7, 6, 9, 30)).status_code == 404


def test_punch_in_twice_409(api, emp):
    assert punch_in(api, emp, ist(2026, 7, 6, 9, 30)).status_code == 201
    assert punch_in(api, emp, ist(2026, 7, 6, 13, 0)).status_code == 409


def test_concurrent_punch_in_exactly_one_201(api, emp):
    with ThreadPoolExecutor(20) as pool:
        codes = list(pool.map(lambda _: punch_in(api, emp, ist(2026, 7, 6, 9, 31)).status_code, range(20)))
    assert sorted(codes) == [201] + [409] * 19


def test_punch_in_defaults_to_now(api, emp):
    r = api.post("/attendance/punch-in", json={"emp_code": emp})
    assert r.status_code == 201
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    assert abs(r.json()["punch_in"] - now_ms) < 5000 and r.json()["punch_in"] % 1000 == 0


# --------------------------------------------------------------------------- punch-out, R3-R5
@pytest.mark.parametrize("out,overtime", [((18, 59, 59), 0),((19, 0, 0), 30), ((19, 10, 30), 40), ((18, 0), 0)])
def test_overtime_r3(api, emp, out, overtime):
    punch_in(api, emp, ist(2026, 7, 6, 9, 30))
    assert punch_out(api, emp, ist(2026, 7, 6, *out)).json()["overtime_minutes"] == overtime


@pytest.mark.parametrize("seconds,hours,half", [
    (16182, 4.5, False),    # 4.495 exactly -> half-up 4.50 -> not a half day
    (16181, 4.49, True),    # 4.49472 -> 4.49
    (9 * 3600 + 7 * 60, 9.12, False),
    (3 * 3600 + 30 * 60, 3.5, True),
    (30, 0.01, True),       # 0.008333 -> 0.01
])
def test_work_hours_r4_half_up_and_half_day_r5(api, emp, seconds, hours, half):
    start = ist(2026, 7, 6, 9, 0)
    punch_in(api, emp, start)
    body = punch_out(api, emp, start + seconds * 1000).json()
    assert body["work_hours"] == hours and body["half_day"] is half


def test_punch_out_errors(api, emp):
    assert punch_out(api, emp, ist(2026, 7, 6, 18, 0)).status_code == 404            # no punch-in yet
    assert punch_out(api, "EMP4040", ist(2026, 7, 6, 18, 0)).status_code == 404      # unknown employee
    punch_in(api, emp, ist(2026, 7, 6, 9, 30))
    assert punch_out(api, emp, ist(2026, 7, 6, 9, 30)).status_code == 422            # == punch_in
    assert punch_out(api, emp, ist(2026, 7, 7, 9, 31)).status_code == 422            # > 24 h later
    assert punch_out(api, emp, ist(2026, 7, 6, 18, 30)).status_code == 200
    assert punch_out(api, emp, ist(2026, 7, 6, 18, 40)).status_code == 409            # already out


def test_punch_out_never_writes_history_and_keeps_late(api, emp):
    punch_in(api, emp, ist(2026, 7, 6, 10, 5))
    body = punch_out(api, emp, ist(2026, 7, 6, 19, 10)).json()
    assert body["history"] == [] and body["late_minutes"] == 35 and body["overtime_minutes"] == 40


def test_concurrent_punch_out_exactly_one_200(api, emp):
    punch_in(api, emp, ist(2026, 7, 6, 9, 30))
    with ThreadPoolExecutor(10) as pool:
        codes = list(pool.map(lambda i: punch_out(api, emp, ist(2026, 7, 6, 18, 30 + i)).status_code, range(10)))
    assert sorted(codes) == [200] + [409] * 9


# --------------------------------------------------------------------------- list
def test_list_attendance_sort_filter_paginate(api, db, emp):
    api.post("/employees", json=employee("EMP0000"))
    for code in ("EMP0001", "EMP0000"):
        for day in (6, 7, 8):
            punch_in(api, code, ist(2026, 7, day, 9, 30))
    r = api.get("/attendance", params={"page_size": 4}).json()
    assert [(i["date"], i["emp_code"]) for i in r["items"]] == [
        ("2026-07-08", "EMP0000"), ("2026-07-08", "EMP0001"), ("2026-07-07", "EMP0000"), ("2026-07-07", "EMP0001")]
    assert r["total"] == 6 and all("id" not in i and "_id" not in i for i in r["items"])
    r = api.get("/attendance", params={"emp_code": emp, "date_from": "2026-07-07", "date_to": "2026-07-07"}).json()
    assert r["total"] == 1 and r["items"][0]["date"] == "2026-07-07"
    assert api.get("/attendance", params={"status": "LEAVE"}).json()["total"] == 0
    for bad in ({"date_from": "2026-07-08", "date_to": "2026-07-07"}, {"date_from": "2026-13-01"},
                {"date_to": "yesterday"}, {"status": "HOLIDAY"}, {"page_size": 101}, {"page": 0}):
        assert api.get("/attendance", params=bad).status_code == 422


def test_list_attendance_legacy_record_defaults(api, db):
    db.attendance_logs.insert_one({"emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",
                                   "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=timezone.utc),
                                   "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0})
    item = api.get("/attendance").json()["items"][0]
    assert item["half_day"] is False and item["history"] == []


# --------------------------------------------------------------------------- PATCH
def patch(api, code, day, **body):
    return api.patch(f"/attendance/{code}/{day}", json={"reason": "biometric glitch", "regularized_by": "hr.admin",
                                                         **body})


def test_regularize_punch_in_recomputes_and_appends_history(api, emp):
    punch_in(api, emp, ist(2026, 7, 7, 10, 5))
    punch_out(api, emp, ist(2026, 7, 7, 18, 30))
    r = patch(api, emp, "2026-07-07", punch_in=ist(2026, 7, 7, 9, 28), work_hours=1, late_minutes=99)
    assert r.status_code == 200
    body = r.json()
    assert body["punch_in"] == ist(2026, 7, 7, 9, 28) and body["late_minutes"] == 0 and body["work_hours"] == 9.03
    (entry,) = body["history"]
    assert entry["by"] == "hr.admin" and entry["reason"] == "biometric glitch" and isinstance(entry["at"], int)
    assert entry["changes"] == {
        "punch_in": {"from": ist(2026, 7, 7, 10, 5), "to": ist(2026, 7, 7, 9, 28)},
        "late_minutes": {"from": 35, "to": 0},
        "work_hours": {"from": 8.42, "to": 9.03},
    }
    # A second correction appends, never overwrites.
    r = patch(api, emp, "2026-07-07", status="WFH")
    assert [set(h["changes"]) for h in r.json()["history"]] == [{"punch_in", "late_minutes", "work_hours"},
                                                                {"status"}]


def test_regularize_to_absent_clears_everything(api, emp):
    punch_in(api, emp, ist(2026, 7, 7, 10, 5))
    punch_out(api, emp, ist(2026, 7, 7, 19, 30))
    body = patch(api, emp, "2026-07-07", status="ABSENT").json()
    assert (body["punch_in"], body["punch_out"], body["work_hours"], body["late_minutes"],
            body["overtime_minutes"]) == (None, None, None, 0, 0)
    assert set(body["history"][0]["changes"]) == {"status", "punch_in", "punch_out", "work_hours", "late_minutes",
                                                  "overtime_minutes"}
    # Back to present needs a punch_in.
    assert patch(api, emp, "2026-07-07", status="PRESENT").status_code == 422
    r = patch(api, emp, "2026-07-07", status="PRESENT", punch_in=ist(2026, 7, 7, 9, 30))
    assert r.status_code == 200 and r.json()["punch_out"] is None and len(r.json()["history"]) == 2


def test_regularize_errors(api, emp):
    punch_in(api, emp, ist(2026, 7, 7, 9, 30))
    day = "2026-07-07"
    assert patch(api, emp, "2026-07-08", status="WFH").status_code == 404
    assert patch(api, "EMP4040", day, status="WFH").status_code == 404
    assert patch(api, emp, day, status="PRESENT").status_code == 422                    # changes nothing
    assert patch(api, emp, day).status_code == 422                                      # changes nothing
    assert patch(api, emp, day, status="LEAVE", punch_in=ist(2026, 7, 7, 9, 0)).status_code == 422
    assert patch(api, emp, day, punch_in=ist(2026, 7, 8, 9, 0)).status_code == 422      # other attendance date
    assert patch(api, emp, day, punch_out=ist(2026, 7, 7, 9, 0)).status_code == 422     # before punch_in
    assert patch(api, emp, day, punch_out=ist(2026, 7, 8, 9, 31)).status_code == 422    # > 24 h
    assert patch(api, emp, day, punch_in=1783312500).status_code == 422                 # seconds
    assert patch(api, emp, "2026-7-7", status="WFH").status_code == 422
    assert api.patch(f"/attendance/{emp}/{day}", json={"status": "WFH", "reason": "x",
                                                       "regularized_by": "hr"}).status_code == 422  # reason < 5
    assert api.patch(f"/attendance/{emp}/{day}", json={"status": "WFH"}).status_code == 422


def test_concurrent_regularize_never_loses_history(api, db, emp):
    punch_in(api, emp, ist(2026, 7, 7, 9, 30))
    with ThreadPoolExecutor(10) as pool:
        codes = list(pool.map(lambda i: patch(api, emp, "2026-07-07", punch_out=ist(2026, 7, 7, 17, i)).status_code,
                              range(10)))
    assert set(codes) <= {200, 409} and 200 in codes
    stored = db.attendance_logs.find_one({"emp_code": emp, "date": "2026-07-07"})
    assert len(stored["history"]) == codes.count(200)
