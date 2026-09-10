"""Checks for GET /api/dataset/{id}/repair-preview — the follow-up to
repair_report that lets the frontend show a handful of original-vs-fixed
rows, fetched on demand rather than stuffed into the upload response."""

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


def _preview(dataset_id: str):
    return client.get(f"/api/dataset/{dataset_id}/repair-preview")


def test_clean_upload_has_no_preview_rows():
    upload_res = _upload("clean.csv", b"name,age\nAlice,30\nBob,25\n")
    dataset_id = upload_res.json()["dataset_id"]
    assert upload_res.json()["repair_report"]["clean"] is True

    res = _preview(dataset_id)
    assert res.status_code == 200
    assert res.json() == {"dataset_id": dataset_id, "rows": []}


def test_messy_upload_returns_before_after_rows():
    messy = b"NAME,AGE\nAlice, 30 \nBob,NULL\n"
    upload_res = _upload("messy.csv", messy)
    dataset_id = upload_res.json()["dataset_id"]
    assert upload_res.json()["repair_report"]["clean"] is False

    res = _preview(dataset_id)
    assert res.status_code == 200
    body = res.json()
    assert body["dataset_id"] == dataset_id
    assert len(body["rows"]) >= 1

    row = body["rows"][0]
    assert set(row.keys()) == {"before", "after"}
    assert set(row["before"].keys()) == {"NAME", "AGE"}
    # At least one cell must actually differ, or it wouldn't be in the preview.
    assert any(row["before"][k] != row["after"].get(k) for k in row["before"])


def test_preview_never_stuffed_into_upload_response():
    messy = b"NAME,AGE\nAlice, 30 \nBob,NULL\n"
    upload_res = _upload("messy.csv", messy)
    assert "preview" not in upload_res.json()["repair_report"]


def test_preview_capped_at_five_rows():
    rows = "\n".join(f"val{i}, {i} " for i in range(1, 21))  # every row has extra whitespace to repair
    upload_res = _upload("messy_big.csv", f"name,n\n{rows}\n".encode())
    dataset_id = upload_res.json()["dataset_id"]

    res = _preview(dataset_id)
    assert len(res.json()["rows"]) <= 5


def test_json_upload_has_no_preview():
    upload_res = client.post(
        "/api/upload",
        files={"file": ("data.json", b'[{"name": "Alice"}]', "application/json")},
    )
    dataset_id = upload_res.json()["dataset_id"]

    res = _preview(dataset_id)
    assert res.status_code == 200
    assert res.json()["rows"] == []


def test_preview_on_unknown_dataset_404s():
    res = _preview("9f3c1b2a-1234-4567-8901-1234567890ab")
    assert res.status_code == 404


def test_preview_on_malformed_dataset_id_404s():
    res = _preview("not-a-uuid")
    assert res.status_code == 404
