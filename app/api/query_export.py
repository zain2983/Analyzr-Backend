import duckdb
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.core.csv_export import to_safe_csv
from app.core.dataset_manager import get_dataset
from app.core.safe_sql import QueryTimeoutError, UnsafeQueryError, run_readonly_query
from app.core.validation import validate_dataset_id

router = APIRouter()

MAX_QUERY_LENGTH = 20_000

# /api/query caps its JSON payload at 100 rows for the results table, which
# is a real regression from "the whole dataset" for anything bigger. This
# endpoint exists specifically so a "download results" button isn't shipped
# without a full(er) export behind it. Still bounded, not unbounded — a
# result-materializing endpoint with no ceiling is a denial-of-service seam
# regardless of who's allowed to call it, and 200k rows is already far more
# than the SQL tab's table can usefully render.
EXPORT_ROW_CAP = 200_000


class QueryExportRequest(BaseModel):
    dataset_id: str = Field(min_length=1, max_length=64)
    query: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)


@router.post("/query/export")
def export_query_results(request: QueryExportRequest):
    validate_dataset_id(request.dataset_id)
    ds_entry = get_dataset(request.dataset_id)

    if not ds_entry:
        raise HTTPException(status_code=404, detail="Dataset not found")

    df = ds_entry["df"]

    try:
        result_df = run_readonly_query(df, request.query, row_limit=EXPORT_ROW_CAP)
    except UnsafeQueryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except QueryTimeoutError as e:
        raise HTTPException(status_code=408, detail=str(e))
    except duckdb.Error as e:
        raise HTTPException(status_code=400, detail=f"SQL error: {str(e)[:500]}")

    truncated = len(result_df) > EXPORT_ROW_CAP
    if truncated:
        result_df = result_df.head(EXPORT_ROW_CAP)

    csv_text = to_safe_csv(result_df)

    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="query_results.csv"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
            # Read client-side to tell the user the export was capped —
            # exposed via CORS below, same as Content-Disposition.
            "X-Result-Truncated": "true" if truncated else "false",
        },
    )
