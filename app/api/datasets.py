from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.dataset_manager import count_datasets, delete_dataset, get_dataset
from app.core.validation import is_dataset_id, validate_dataset_id

router = APIRouter()

MAX_RECONCILE_IDS = 100


class ReconcileRequest(BaseModel):
    dataset_ids: list[str] = Field(default_factory=list, max_length=MAX_RECONCILE_IDS)


@router.post("/datasets/reconcile")
def reconcile_datasets(request: ReconcileRequest):
    """Reports which of the caller's own dataset ids still exist on this server.

    This replaces a `GET /datasets` that returned every id in the process.
    With no accounts and no per-session ownership, a dataset id *is* the
    capability to read and delete that dataset — so publishing the full list
    handed every visitor the contents of every other visitor's upload. Asking
    the client which ids it already holds answers the same question (which of
    my cached files survived a backend restart?) without ever revealing an id
    the caller didn't already have.
    """
    known = [ds_id for ds_id in request.dataset_ids if is_dataset_id(ds_id)]
    live = [ds_id for ds_id in known if get_dataset(ds_id) is not None]

    return {"dataset_ids": live}


@router.get("/datasets/count")
def datasets_count():
    """Non-identifying health/capacity signal — a count, never the ids."""
    return {"count": count_datasets()}


@router.delete("/dataset/{dataset_id}")
def remove_dataset(dataset_id: str):
    validate_dataset_id(dataset_id)

    if not get_dataset(dataset_id):
        raise HTTPException(status_code=404, detail="Dataset not found")

    delete_dataset(dataset_id)
    return {"dataset_id": dataset_id, "deleted": True}
