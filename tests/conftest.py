"""Starts the real app with uvicorn against a throw-away database (MONGO_DB=<db>_test) and gives tests an
HTTP client plus direct database access. Requires a running MongoDB (MONGO_URI, default localhost)."""
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from dotenv import load_dotenv
from pymongo import MongoClient

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
TEST_DB = os.environ.get("TEST_MONGO_DB", "attendance_test")
PORT = int(os.environ.get("TEST_PORT", "8765"))
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture(scope="session")
def server():
    env = {**os.environ, "MONGO_URI": MONGO_URI, "MONGO_DB": TEST_DB}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)], cwd=ROOT,
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            if httpx.get(f"{BASE}/health").status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("app did not become healthy within 20 s")
    yield BASE
    proc.terminate()
    proc.wait(10)


@pytest.fixture()
def db(server):
    database = MongoClient(MONGO_URI, tz_aware=True)[TEST_DB]
    database.employees.delete_many({})
    database.attendance_logs.delete_many({})
    return database


@pytest.fixture()
def api(server, db):
    with httpx.Client(base_url=server, timeout=30) as client:
        yield client
