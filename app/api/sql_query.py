from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
import duckdb
import numpy as np

from app.core.dataset_manager import get_dataset
from app.core.safe_sql import (
    MAX_RESULT_ROWS,
    QueryTimeoutError,
    UnsafeQueryError,
    run_readonly_query,
)
from app.core.validation import validate_dataset_id

router = APIRouter()

MAX_QUERY_LENGTH = 20_000
RESPONSE_ROW_LIMIT = 100


class SQLQueryRequest(BaseModel):
    dataset_id: str = Field(min_length=1, max_length=64)
    query: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)


@router.post("/query")
def run_sql_query(request: SQLQueryRequest):
    validate_dataset_id(request.dataset_id)
    ds_entry = get_dataset(request.dataset_id)

    if not ds_entry:
        raise HTTPException(status_code=404, detail="Dataset not found")

    df = ds_entry["df"]

    try:
        result_df = run_readonly_query(df, request.query)
    except UnsafeQueryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except QueryTimeoutError as e:
        raise HTTPException(status_code=408, detail=str(e))
    except duckdb.Error as e:
        # DuckDB's message is the useful part of a SQL error, so it's worth
        # returning — truncated, since a bad query can echo a lot of text back.
        raise HTTPException(status_code=400, detail=f"SQL error: {str(e)[:500]}")

    truncated = len(result_df) > MAX_RESULT_ROWS
    if truncated:
        result_df = result_df.head(MAX_RESULT_ROWS)

    result_df = result_df.replace({np.nan: None})

    return {
        "rows": len(result_df),
        "columns": [str(c) for c in result_df.columns],
        "data": result_df.head(RESPONSE_ROW_LIMIT).to_dict(orient="records"),
        "truncated": truncated,
    }
