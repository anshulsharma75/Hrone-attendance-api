# Employee Attendance & Analytics API

FastAPI + MongoDB. All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # or export MONGO_URI / MONGO_DB (real env vars win over .env)
uvicorn app.main:app --port 8000
```
MongoDB 6.0+ is needed (`$setWindowFields`, `$lookup` with `localField` + `pipeline`). Indexes are created at startup, nothing to do by hand. Interactive docs: `http://localhost:8000/docs`.

## Status
Everything in `openapi.yaml` is implemented: punch-in/out, regularization with history, the four analytics endpoints (aggregation pipelines) and `/admin/explain/{endpoint}`. Nothing is missing.

## Notes and assumptions
- A trend range of up to 92 days (counting both ends) is accepted; 93 or more gives 422.
- `moving_avg_7d` averages the reported (4-decimal) daily rates, as the field description says.
- A correction that changes no stored value (including derived ones) is rejected with 422; a concurrent correction that loses the race gets 409.
- Sums that feed rounding (work hours, rates) are kept as whole hundredths / ten-thousandths so half-up rounding is exact.
- Python 3.11+.
