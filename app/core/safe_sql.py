"""Sandboxed execution of user-supplied SQL.

The /api/query endpoint hands raw, unauthenticated SQL straight to DuckDB.
DuckDB is not a pure query engine — out of the box it can read and write
arbitrary paths on the host (`read_csv_auto('/etc/passwd')`,
`COPY (...) TO '/app/main.py'`), list directories via `glob()`, `ATTACH`
other databases, and install/load extensions such as httpfs that turn a
SELECT into an outbound HTTP request. On a cloud host that last one reaches
the instance metadata endpoint.

So the query text alone can never be the security boundary. Three layers,
outermost first:

1. Connection config. `enable_external_access=false` disables every
   filesystem and network operation at the engine level, and DuckDB refuses
   to let a running database change that setting back.
2. Statement gate. Exactly one statement, and it must be read-only
   (SELECT/EXPLAIN — DuckDB classifies DESCRIBE, SHOW and SUMMARIZE as
   SELECT). This is defense in depth: layer 1 already blocks the dangerous
   verbs, but it keeps the endpoint's contract honest and rejects
   multi-statement payloads, which `execute()` otherwise runs in sequence.
3. Wall-clock budget. A syntactically innocent query can still pin a core
   forever (`SELECT count(*) FROM range(1e12)`), so a watchdog interrupts
   anything past the deadline.
"""

from __future__ import annotations

import threading

import duckdb
import pandas as pd

# Engine-level lockdown. Passed at connect time because DuckDB refuses to
# change these while a database is running — which is exactly what makes
# them a boundary a query can't talk its way out of.
_SANDBOX_CONFIG = {
    # Kills all file and network I/O: read_csv, COPY TO, glob, ATTACH, httpfs.
    "enable_external_access": False,
    "allow_unsigned_extensions": False,
    "autoinstall_known_extensions": False,
    "autoload_known_extensions": False,
    # A single query has no business spawning a thread pool per request.
    "threads": 2,
    "memory_limit": "512MB",
}

# DuckDB folds DESCRIBE / SHOW / SUMMARIZE / PRAGMA-style introspection into
# SELECT, so this pair covers every read-only shape the SQL tab offers.
_ALLOWED_STATEMENT_TYPES = {
    duckdb.StatementType.SELECT,
    duckdb.StatementType.EXPLAIN,
}

QUERY_TIMEOUT_SECONDS = 15.0

# Cap on rows materialized into Python objects for the response. The frontend
# only renders the first 100; fetching a 50M-row cross join into a dict list
# would exhaust the process regardless of what it displays.
MAX_RESULT_ROWS = 5_000


class UnsafeQueryError(ValueError):
    """The query was rejected before execution — a policy refusal, not a SQL error."""


class QueryTimeoutError(RuntimeError):
    """The query exceeded its wall-clock budget and was interrupted."""


def _reject_unsafe(query: str, con: duckdb.DuckDBPyConnection) -> None:
    if not query.strip():
        raise UnsafeQueryError("Query is empty")

    try:
        statements = con.extract_statements(query)
    except duckdb.Error as e:
        # A parse failure here is an ordinary SQL syntax error; surface it as
        # one rather than as a policy refusal.
        raise duckdb.ParserException(str(e)) from e

    if len(statements) == 0:
        raise UnsafeQueryError("Query is empty")
    if len(statements) > 1:
        raise UnsafeQueryError(
            "Only one statement per query is allowed — remove the ';'-separated extra statements"
        )

    statement_type = statements[0].type
    if statement_type not in _ALLOWED_STATEMENT_TYPES:
        raise UnsafeQueryError(
            f"Only read-only queries are allowed here ({statement_type.name} is not permitted). "
            "Use SELECT, DESCRIBE, SUMMARIZE or EXPLAIN."
        )


def run_readonly_query(df: pd.DataFrame, query: str) -> pd.DataFrame:
    """Runs `query` against `df` (exposed as `dataset` and `data`) in a sandbox.

    Raises UnsafeQueryError for policy refusals, QueryTimeoutError when the
    budget is blown, and duckdb.Error for ordinary SQL problems.
    """
    con = duckdb.connect(database=":memory:", config=_SANDBOX_CONFIG)
    try:
        con.register("data", df)
        con.register("dataset", df)  # the table name the SQL tab's starters use

        _reject_unsafe(query, con)

        timed_out = threading.Event()

        def _interrupt():
            timed_out.set()
            con.interrupt()

        watchdog = threading.Timer(QUERY_TIMEOUT_SECONDS, _interrupt)
        watchdog.start()
        try:
            relation = con.sql(query)
            # Fetch through a LIMIT so an enormous result set is bounded in
            # the engine rather than after it lands in Python.
            result = relation.limit(MAX_RESULT_ROWS + 1).df() if relation is not None else pd.DataFrame()
        except duckdb.InterruptException as e:
            raise QueryTimeoutError(
                f"Query exceeded the {QUERY_TIMEOUT_SECONDS:.0f}s time limit and was cancelled"
            ) from e
        except duckdb.Error:
            if timed_out.is_set():
                raise QueryTimeoutError(
                    f"Query exceeded the {QUERY_TIMEOUT_SECONDS:.0f}s time limit and was cancelled"
                )
            raise
        finally:
            watchdog.cancel()

        return result
    finally:
        con.close()
