from fastapi import APIRouter, HTTPException

from app.core.dataset_manager import get_dataset
from app.core.validation import validate_dataset_id

router = APIRouter()


@router.get("/dataset/{dataset_id}/repair-preview")
def get_repair_preview(dataset_id: str):
    """A handful of {before, after} row snapshots from the CSV repair
    pipeline, for a dataset that needed repairs on upload.

    Fetched on demand rather than included in the upload response, so every
    upload doesn't carry sample rows through sessionStorage whether or not
    the user ever opens the preview — same reasoning as Compare Tab's
    hover-preview reusing /api/query instead of eagerly fetching everything.
    """
    validate_dataset_id(dataset_id)
    entry = get_dataset(dataset_id)

    if not entry:
        raise HTTPException(status_code=404, detail="Dataset not found")

    return {"dataset_id": dataset_id, "rows": entry.get("repair_preview") or []}
