from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import duckdb
from app.core.dataset_manager import get_dataset

router = APIRouter()

class SQLQueryRequest(BaseModel):
    dataset_id: str
    query: str

@router.post("/query")
def run_sql_query(request: SQLQueryRequest):
    print(request)
    ds_entry = get_dataset(request.dataset_id)
    # print(ds_entry)

    if not ds_entry:
        raise HTTPException(status_code=404, detail="Dataset not found")

    df = ds_entry["df"]

    con = duckdb.connect(database=":memory:")
    con.register("data", df)
    con.register("dataset", df)  # register under the name clients might use in SQL

    try:
        result_df = con.execute(request.query).fetchdf()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"SQL error: {str(e)}")

    return {
        "rows": len(result_df),
        "columns": list(result_df.columns),
        "data": result_df.head(100).to_dict(orient="records")
    }
