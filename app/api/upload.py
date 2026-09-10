import io
import json
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, HTTPException
import pandas as pd
from app.core.dataset_manager import create_dataset
from app.core.csv_repair import CSVRepairError, RepairReport, repair_csv

router = APIRouter()

MAX_FILE_SIZE_MB = 20
# Parsed size guard, independent of the on-disk upload size — a compressed
# XLSX can expand into a far larger frame in memory than its file size implies.
MAX_CELLS = 5_000_000

ALLOWED_SUFFIXES = {".csv", ".json", ".xlsx"}


def _read_csv(file) -> tuple[pd.DataFrame, RepairReport]:
    raw = file.read()
    try:
        return repair_csv(raw)
    except CSVRepairError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _read_json(file) -> pd.DataFrame:
    try:
        data = json.loads(file.read())
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON file: {str(e)}")

    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        raise HTTPException(
            status_code=400,
            detail="JSON file must be an array of objects (records) — nested or envelope-wrapped documents aren't supported yet",
        )

    try:
        return pd.json_normalize(data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON file: {str(e)}")


def _read_xlsx(file) -> tuple[pd.DataFrame, str]:
    try:
        # openpyxl needs a real seekable stream — SpooledTemporaryFile doesn't
        # implement enough of io.IOBase for it, so read the bytes into BytesIO first.
        excel = pd.ExcelFile(io.BytesIO(file.read()), engine="openpyxl")
        sheet_name = excel.sheet_names[0]
        df = excel.parse(sheet_name)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid XLSX file: {str(e)}")
    return df, sheet_name


@router.post("/upload")
async def upload_csv(file: UploadFile = File(...)):
    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail="Only CSV, JSON, and XLSX files are allowed")

    file.file.seek(0, 2)
    size_mb = file.file.tell() / (1024 * 1024)
    file.file.seek(0)

    if size_mb > MAX_FILE_SIZE_MB:
        raise HTTPException(
            status_code=400,
            detail=f"File too large. Max allowed size is {MAX_FILE_SIZE_MB} MB"
        )

    sheet_name = None
    repair_report = None
    if suffix == ".csv":
        df, repair_report = _read_csv(file.file)
    elif suffix == ".json":
        df = _read_json(file.file)
    else:
        df, sheet_name = _read_xlsx(file.file)

    if df.empty:
        raise HTTPException(status_code=400, detail="Uploaded file has no rows")

    cell_count = df.shape[0] * df.shape[1]
    if cell_count > MAX_CELLS:
        raise HTTPException(
            status_code=400,
            detail=f"Parsed dataset has {cell_count:,} cells, exceeding the {MAX_CELLS:,} cell limit",
        )

    dataset_id = create_dataset(df)

    response = {
        "dataset_id": dataset_id,
        "rows": len(df),
        "columns": list(df.columns),
        "column_types": df.dtypes.astype(str).to_dict(),
    }
    if sheet_name is not None:
        response["sheet_name"] = sheet_name
    if repair_report is not None:
        response["repair_report"] = repair_report.to_dict()

    return response
