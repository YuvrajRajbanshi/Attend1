"""Synthetic data with the awkward cases the business rules describe.

Used by the tests (small, random) and by scripts/seed_large.py (~100k records for explain / timing checks).
Documents are written straight into MongoDB, exactly like the grader seeds them: the derived fields are
stored values, and analytics must read them rather than recompute them.
"""
import random
from datetime import date, datetime, time, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
STATUS_WEIGHTS = [("PRESENT", 60), ("WFH", 15), ("ON_DUTY", 6), ("ABSENT", 9), ("LEAVE", 10)]


def make_employees(n: int, first_day: date, rng: random.Random) -> list[dict]:
    departments = ["Engineering", "Sales", "Support", "Finance", "engineering"]  # case-sensitive on purpose
    out = []
    for i in range(1, n + 1):
        overnight = i % 9 == 0
        if i % 13 == 0:
            joined = first_day + timedelta(days=rng.randint(5, 40))      # mid-period joiner
        elif i % 17 == 0:
            joined = first_day + timedelta(days=400)                     # joins after the data ends
        else:
            joined = first_day - timedelta(days=rng.randint(1, 600))
        out.append({
            "emp_code": f"EMP{i:04d}",
            "name": f"Employee {i}",
            "email": f"e{i}@example.com",
            "department": departments[i % len(departments)],
            "shift_start": "22:00" if overnight else rng.choice(["09:00", "09:30", "10:00"]),
            "shift_end": "06:00" if overnight else rng.choice(["18:00", "18:30", "19:00"]),
            "joined_on": joined.isoformat(),
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        })
    return out


def make_logs(emps: list[dict], first_day: date, days: int, rng: random.Random, density: float = 0.8) -> list[dict]:
    statuses = [s for s, w in STATUS_WEIGHTS for _ in range(w)]
    out = []
    for e in emps:
        if int(e["emp_code"][3:]) % 11 == 0:
            continue                                                     # employees with no logs at all
        for d in range(days):
            day = first_day + timedelta(days=d)
            weekend = day.weekday() >= 5
            if rng.random() > (0.08 if weekend else density):           # missing days, some weekend work
                continue
            status = rng.choice(statuses)
            doc = {"emp_code": e["emp_code"], "date": day.isoformat(), "status": status}
            if status in ("ABSENT", "LEAVE"):
                doc.update(punch_in=None, punch_out=None, work_hours=None, late_minutes=0, overtime_minutes=0,
                           half_day=False)
            else:
                start = datetime.combine(day, time.fromisoformat(e["shift_start"]), IST)
                pin = start + timedelta(minutes=rng.choice([-15, -5, 0, 5, 12, 20, 20, 35, 60]))
                late = max(0, int((pin - start).total_seconds() // 60))
                late = late if late > 10 else 0
                if rng.random() < 0.05:
                    doc.update(punch_in=pin, punch_out=None, work_hours=None, late_minutes=late,
                               overtime_minutes=0, half_day=False)
                else:
                    hours = rng.choice([3.5, 4.25, 4.5, 7.75, 8.5, 9.0, 9.33, 10.17])
                    pout = pin + timedelta(hours=hours)
                    doc.update(punch_in=pin, punch_out=pout, work_hours=hours, late_minutes=late,
                               overtime_minutes=rng.choice([0, 0, 0, 30, 45, 61]), half_day=hours < 4.5)
            if rng.random() < 0.1:              # legacy shape: no history, and no half_day when it is false
                if not doc["half_day"]:
                    del doc["half_day"]
            else:
                doc["history"] = []
            out.append(doc)
    # Logs of an employee that does not exist: must be ignored by the analytics that join employees.
    for d in range(0, days, 3):
        day = first_day + timedelta(days=d)
        out.append({"emp_code": "EMP9999", "date": day.isoformat(), "status": "PRESENT",
                    "punch_in": datetime.combine(day, time(10, 30), IST), "punch_out": None, "work_hours": None,
                    "late_minutes": 60, "overtime_minutes": 0, "half_day": False, "history": []})
    return out
