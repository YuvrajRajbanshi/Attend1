"""Seed ~100,000 attendance records (plus ~560 employees) for explain and timing checks.

Usage:  python -m scripts.seed_large [database]     (default database: attendance_large; MONGO_URI from env / .env)
Then:   MONGO_DB=attendance_large uvicorn app.main:app --port 8000
"""
import os
import random
import sys
from datetime import date

from dotenv import load_dotenv
from pymongo import MongoClient

from tests.datagen import make_employees, make_logs

load_dotenv()
name = sys.argv[1] if len(sys.argv) > 1 else "attendance_large"
db = MongoClient(os.environ.get("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)[name]
rng = random.Random(7)
first = date(2026, 1, 1)
emps = make_employees(560, first, rng)
logs = make_logs(emps, first, 330, rng)
db.employees.drop()
db.attendance_logs.drop()  # also drops indexes: the app must recreate them at startup
db.employees.insert_many(emps)
db.attendance_logs.insert_many(logs)
print(f"{name}: {len(emps)} employees, {len(logs)} attendance records")
