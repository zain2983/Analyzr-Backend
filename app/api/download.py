from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from io import StringIO
from app.core.dataset_manager import get_dataset

router = APIRouter()

@router.get("/dataset/{dataset_id}/download")
def download_dataset(dataset_id: str):
    ds_entry = get_dataset(dataset_id)

    if not ds_entry:
        raise HTTPException(status_code=404, detail="Dataset not found")

    df = ds_entry["df"]

    buffer = StringIO()
    df.to_csv(buffer, index=False)
    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{dataset_id}.csv"'
        },
    )
