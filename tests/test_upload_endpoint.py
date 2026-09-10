"""End-to-end checks that /api/upload actually wires up the repair pipeline
and surfaces its report in the response contract the frontend depends on."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core import dataset_manager

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clear_datasets():
    dataset_manager.DATASETS.clear()
    yield
    dataset_manager.DATASETS.clear()


def _upload(name: str, content: bytes):
    return client.post("/api/upload", files={"file": (name, content, "text/csv")})


def test_clean_csv_upload_has_no_repair_warnings():
    res = _upload("clean.csv", b"name,age\nAlice,30\nBob,25\n")
    assert res.status_code == 200
    body = res.json()
    assert body["rows"] == 2
    assert body["columns"] == ["name", "age"]
    assert body["repair_report"]["clean"] is True
    assert body["repair_report"]["warnings"] == []


def test_messy_csv_upload_surfaces_repair_report():
    messy = b"NAME,AGE\nAlice, 30 \nBob,NULL\n"
    res = _upload("messy.csv", messy)
    assert res.status_code == 200
    body = res.json()
    assert body["rows"] == 2
    report = body["repair_report"]
    assert report["clean"] is False
    assert any("missing-value marker" in w for w in report["warnings"])


def test_empty_file_rejected_with_clear_message():
    res = _upload("empty.csv", b"")
    assert res.status_code == 400
    assert "empty" in res.json()["detail"].lower()


def test_header_only_file_rejected():
    res = _upload("header_only.csv", b"a,b,c\n")
    assert res.status_code == 400
    assert "no rows" in res.json()["detail"].lower()


def test_xlsx_binary_renamed_to_csv_rejected():
    fake_xlsx = b"PK\x03\x04" + b"\x00" * 40
    res = _upload("fake.csv", fake_xlsx)
    assert res.status_code == 400
    assert "zip-based binary" in res.json()["detail"]


def test_semicolon_delimited_file_parses_correctly():
    res = _upload("euro.csv", b"name;age\nAlice;30\nBob;25\n")
    assert res.status_code == 200
    body = res.json()
    assert body["columns"] == ["name", "age"]
    assert body["repair_report"]["delimiter"] == ";"


def test_column_types_present_for_downstream_sql_autocomplete():
    res = _upload("typed.csv", b"id,amount\n1,10.5\n2,20.5\n")
    assert res.status_code == 200
    body = res.json()
    assert body["column_types"]["amount"] in ("float64", "int64")
