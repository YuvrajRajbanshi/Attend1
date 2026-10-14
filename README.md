# Employee Attendance & Analytics API

FastAPI + MongoDB backend for punch-in/out, manual corrections with an audit trail, and analytics built on
MongoDB aggregation pipelines. All application code is in `app/main.py`.

## Run

Requires Python 3.11+ and MongoDB 6.0+ (tested on 7.0).

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                     # or export MONGO_URI / MONGO_DB
uvicorn app.main:app --port 8000
```

- `MONGO_URI` and `MONGO_DB` are read from the environment. A local `.env` is loaded, but real environment variables take priority. If either is missing, startup fails with a clear message.
- Indexes are created at startup, idempotently. If MongoDB is unreachable at that moment, `/health` returns 503 and retries.
- To load the nine sample documents, run `sample_seed.py` from the candidate kit.

## Tests

The tests start the app with uvicorn against a separate database (`attendance_test`) and need a running MongoDB.

```bash
pip install pytest httpx
python -m pytest -q
```

- `tests/test_api.py` covers the contract: status codes, R1-R5 boundary values, validation, pagination, and races (parallel punch-ins, punch-outs, creates and corrections).
- `tests/test_analytics.py` compares every analytics endpoint with a deliberately naive Python implementation of the rules. The random data includes ties, employees with no logs, weekend records, mid-month and future joiners, overnight shifts, legacy records and logs of unknown employees. It also checks that `/admin/explain` uses `IXSCAN` and never `COLLSCAN`.
- `python -m scripts.seed_large` seeds about 100k records into `attendance_large` for timing and explain checks. On that data, list endpoints answer in under 15 ms (except deep pages) and analytics in roughly 70-200 ms.

## Notes on interpretation

- **PATCH:** a field sent as `null` is a 422 (the contract types are not nullable). An omitted field is unchanged. "Changes nothing" means none of the sent `status`/`punch_in`/`punch_out` values differs from the stored record.
- **Concurrent corrections:** these use optimistic concurrency. The update only applies if status, punch times and history length are unchanged since the record was read. The loser gets 409 and no history entry is lost.
- **Trend range:** the trend accepts at most 92 days, counting both ends (e.g. 1 Jul to 30 Sep).
- **Rounding:** R8 rounding is done with `Decimal` (Decimal128 inside MongoDB for the trend), never with `round()` or `$round`, because both of those round half to even.
