"""Checks for POST /api/query/export — a query-scoped export so a "download
results" button doesn't silently truncate at /api/query's 100-row display cap."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core import dataset_manager
from app.api.query_export import EXPORT_ROW_CAP

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clear_datasets():
    dataset_manager.DATASETS.clear()
    yield
    dataset_manager.DATASETS.clear()


def _upload(name: str, content: bytes):
    return client.post("/api/upload", files={"file": (name, content, "text/csv")})


def _export(dataset_id: str, query: str):
    return client.post("/api/query/export", json={"dataset_id": dataset_id, "query": query})


def test_export_returns_full_csv_past_the_100_row_display_cap():
    rows = "\n".join(f"{i},val{i}" for i in range(1, 251))
    upload_res = _upload("big.csv", f"id,name\n{rows}\n".encode())
    dataset_id = upload_res.json()["dataset_id"]

    res = _export(dataset_id, "SELECT * FROM dataset ORDER BY id")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    assert res.headers["x-result-truncated"] == "false"

    lines = res.text.strip().split("\n")
    assert lines[0] == "id,name"
    # header + 250 data rows — well past the 100-row cap /api/query applies to `data`.
    assert len(lines) == 251


def test_export_content_disposition_is_an_attachment():
    upload_res = _upload("a.csv", b"name\nAlice\n")
    dataset_id = upload_res.json()["dataset_id"]

    res = _export(dataset_id, "SELECT * FROM dataset")
    assert "attachment" in res.headers["content-disposition"]
    assert "query_results.csv" in res.headers["content-disposition"]


def test_export_truncates_past_the_export_cap_and_flags_it():
    upload_res = _upload("a.csv", b"name\nAlice\n")
    dataset_id = upload_res.json()["dataset_id"]

    # A cross join is cheap to write and easily exceeds EXPORT_ROW_CAP —
    # exercises the export cap without needing a real large dataset.
    query = (
        "SELECT a.name FROM dataset a, "
        f"(SELECT unnest(range({EXPORT_ROW_CAP + 50}))) AS t"
    )
    res = _export(dataset_id, query)
    assert res.status_code == 200
    assert res.headers["x-result-truncated"] == "true"


def test_export_rejects_write_query():
    upload_res = _upload("a.csv", b"name\nAlice\n")
    dataset_id = upload_res.json()["dataset_id"]

    res = _export(dataset_id, "CREATE TABLE t (a INT)")
    assert res.status_code == 400


def test_export_surfaces_sql_error():
    upload_res = _upload("a.csv", b"name\nAlice\n")
    dataset_id = upload_res.json()["dataset_id"]

    res = _export(dataset_id, "SELECT nonexistent_column FROM dataset")
    assert res.status_code == 400
    assert "sql error" in res.json()["detail"].lower()


def test_export_on_unknown_dataset_404s():
    res = _export("9f3c1b2a-1234-4567-8901-1234567890ab", "SELECT * FROM dataset")
    assert res.status_code == 404


def test_export_sanitizes_formula_injection():
    upload_res = _upload("a.csv", b"name\nAlice\n")
    dataset_id = upload_res.json()["dataset_id"]

    res = _export(dataset_id, "SELECT '=cmd|\"/c calc\"!A0' AS name")
    assert res.status_code == 200
    assert res.text.strip().split("\n")[1].startswith("\"'=cmd")
