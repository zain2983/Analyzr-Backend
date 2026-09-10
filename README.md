# Analyzr Backend

This repository contains the backend service for **Analyzr**.

The backend is built solely to support the frontend by handling CPU-intensive operations such as data processing and analysis.

## Overview

- The service operates entirely in memory
- No database is used
- No data is persisted
- All data exists only for the lifetime of the process

If the server restarts, all data is intentionally lost.

## Design Purpose

This backend is intentionally lightweight and stateless. Its primary goals are:

- Offloading heavy computation from the frontend
- Keeping the architecture simple
- Avoiding unnecessary persistence and infrastructure overhead

The backend is not intended to be used as a standalone service.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `ALLOWED_ORIGINS` | production origin only | Comma-separated CORS allowlist. Overrides `ANALYZR_ENV`. |
| `ANALYZR_ENV` | `production` | Set to `development` to also allow `http://localhost:3000`. |

The CORS allowlist defaults to the production origin alone, so running the
frontend locally against a locally-run backend needs one of these set.
Without it the browser reports "Backend unreachable"; the server logs the
active origins on startup to make that obvious.

```bash
ANALYZR_ENV=development uvicorn app.main:app --reload
```

## Security model

There are no accounts, so **a dataset id is the capability** to read or delete
that dataset. Two consequences shape the API:

- Dataset ids are never enumerable. `POST /api/datasets/reconcile` confirms
  only the ids a caller already holds; there is no endpoint that lists them.
- Anything that leaves the service as a file is sanitized against
  spreadsheet formula injection (`app/core/csv_export.py`).

The SQL endpoint runs user-supplied queries against DuckDB, which by default
can read and write the host filesystem and open network connections. It is
sandboxed in `app/core/safe_sql.py`: external access is disabled at connect
time, only single read-only statements are accepted, and queries are bounded
by a wall-clock timeout and a row cap. `tests/test_security.py` asserts each
of these, so run it before changing that module.

```bash
pytest tests/
```

## Frontend

The frontend application is available at:

https://analyzr-z1.vercel.app
