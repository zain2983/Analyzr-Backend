"""Checks for POST /api/transform/rename — the endpoint the Compare Tab's
inline column merge is a thin caller of."""

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


def _rename(dataset_id: str, column: str, new_name: str):
    return client.post(
        "/api/transform/rename",
        json={"dataset_id": dataset_id, "column": column, "new_name": new_name},
    )


def test_rename_column_updates_the_stored_frame():
    upload_res = _upload("a.csv", b"Customer Name,email\nAlice,alice@example.com\n")
    dataset_id = upload_res.json()["dataset_id"]

    res = _rename(dataset_id, "Customer Name", "customer_name")
    assert res.status_code == 200
    body = res.json()
    assert body["columns"] == ["customer_name", "email"]

    # The rename must actually be visible to every other endpoint, not just
    # echoed back — that's the whole point of doing this server-side.
    query_res = client.post(
        "/api/query",
        json={"query": "SELECT customer_name FROM dataset LIMIT 1", "dataset_id": dataset_id},
    )
    assert query_res.status_code == 200
    assert query_res.json()["data"] == [{"customer_name": "Alice"}]


def test_rename_unknown_column_rejected():
    upload_res = _upload("a.csv", b"name,age\nAlice,30\n")
    dataset_id = upload_res.json()["dataset_id"]

    res = _rename(dataset_id, "does_not_exist", "new_name")
    assert res.status_code == 400
    assert "not found" in res.json()["detail"].lower()


def test_rename_to_existing_column_rejected():
    upload_res = _upload("a.csv", b"name,age\nAlice,30\n")
    dataset_id = upload_res.json()["dataset_id"]

    res = _rename(dataset_id, "name", "age")
    assert res.status_code == 400
    assert "already exists" in res.json()["detail"].lower()


def test_rename_to_same_name_is_a_noop_success():
    upload_res = _upload("a.csv", b"name,age\nAlice,30\n")
    dataset_id = upload_res.json()["dataset_id"]

    res = _rename(dataset_id, "name", "name")
    assert res.status_code == 200
    assert res.json()["columns"] == ["name", "age"]


def test_rename_empty_new_name_rejected():
    upload_res = _upload("a.csv", b"name,age\nAlice,30\n")
    dataset_id = upload_res.json()["dataset_id"]

    res = _rename(dataset_id, "name", "   ")
    assert res.status_code == 400
    assert "empty" in res.json()["detail"].lower()


def test_rename_on_unknown_dataset_404s():
    res = _rename("9f3c1b2a-1234-4567-8901-1234567890ab", "name", "new_name")
    assert res.status_code == 404


def test_rename_on_malformed_dataset_id_404s():
    res = _rename("not-a-uuid", "name", "new_name")
    assert res.status_code == 404
