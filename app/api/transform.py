from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.dataset_manager import get_dataset, rename_column
from app.core.validation import validate_dataset_id

router = APIRouter()

# Generous enough for any real column name; bounds an unauthenticated
# endpoint against someone posting megabytes of "new_name".
MAX_COLUMN_NAME_LENGTH = 200


class RenameColumnRequest(BaseModel):
    dataset_id: str
    column: str = Field(..., min_length=1, max_length=MAX_COLUMN_NAME_LENGTH)
    new_name: str = Field(..., min_length=1, max_length=MAX_COLUMN_NAME_LENGTH)


@router.post("/transform/rename")
def rename_dataset_column(request: RenameColumnRequest):
    """Renames one column on the server-held frame.

    This is the endpoint the Compare Tab's "inline merge" feature was blocked
    on: accepting a fuzzy-matched pair (e.g. "Customer Name" / "customer_name")
    and actually unifying them means mutating the frame the backend holds, not
    just what the frontend displays — `Dataset.columnNames` is never sent on
    any request, so a client-only rename would show one dataset's tab
    disagreeing with what every other tab and query still sees.
    """
    validate_dataset_id(request.dataset_id)

    entry = get_dataset(request.dataset_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Dataset not found")

    df = entry["df"]
    column = request.column
    new_name = request.new_name.strip()

    if column not in df.columns:
        raise HTTPException(status_code=400, detail=f"Column '{column}' not found")
    if not new_name:
        raise HTTPException(status_code=400, detail="New column name cannot be empty")
    if new_name != column and new_name in df.columns:
        raise HTTPException(status_code=400, detail=f"Column '{new_name}' already exists")

    updated = rename_column(request.dataset_id, column, new_name)
    if updated is None:
        # Expired between get_dataset() and rename_column() acquiring the
        # lock — same end state as never having found it.
        raise HTTPException(status_code=404, detail="Dataset not found")

    return {
        "dataset_id": request.dataset_id,
        "columns": [str(c) for c in updated.columns],
        "column_types": {str(k): v for k, v in updated.dtypes.astype(str).to_dict().items()},
    }
