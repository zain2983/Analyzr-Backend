from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from app.core.csv_export import to_safe_csv
from app.core.dataset_manager import get_dataset
from app.core.validation import safe_download_filename, validate_dataset_id

router = APIRouter()


@router.get("/dataset/{dataset_id}/download")
def download_dataset(dataset_id: str, filename: Optional[str] = Query(default=None, max_length=200)):
    validate_dataset_id(dataset_id)
    ds_entry = get_dataset(dataset_id)

    if not ds_entry:
        raise HTTPException(status_code=404, detail="Dataset not found")

    df = ds_entry["df"]
    csv_text = to_safe_csv(df)

    download_name = safe_download_filename(filename or f"{dataset_id}.csv", fallback=f"{dataset_id}.csv")

    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{download_name}"',
            # Belt and braces: the browser must not sniff this into something
            # renderable, and it must never be displayed inline in our origin.
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )
