import io
import json
import zipfile
from pathlib import Path, PurePosixPath
from typing import Optional, Tuple

from fastapi import APIRouter, UploadFile, File, HTTPException
import pandas as pd

from app.core.csv_export import sanitize_frame_in_place
from app.core.dataset_manager import create_dataset
from app.core.csv_repair import CSVRepairError, RepairReport, repair_csv

router = APIRouter()

MAX_FILE_SIZE_MB = 20
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
# Parsed size guard, independent of the on-disk upload size — a compressed
# XLSX can expand into a far larger frame in memory than its file size implies.
MAX_CELLS = 5_000_000
# Zip-bomb guard for XLSX: a few hundred KB of archive can declare gigabytes
# of XML, and openpyxl will happily try to parse all of it. Checked against
# the central directory before a single byte is decompressed.
MAX_XLSX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024

MAX_FILENAME_LENGTH = 255

ALLOWED_SUFFIXES = {".csv", ".json", ".xlsx"}


def _read_upload_bounded(file) -> bytes:
    """Reads the upload in chunks, refusing anything past the size cap.

    Reading `.read()` in one go and measuring afterwards means the process has
    already committed the memory an attacker asked it to — the cap has to be
    enforced while reading, not after.
    """
    chunks = []
    total = 0

    while True:
        chunk = file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_FILE_SIZE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Max allowed size is {MAX_FILE_SIZE_MB} MB",
            )
        chunks.append(chunk)

    return b"".join(chunks)


def _validate_filename(raw_name: Optional[str]) -> str:
    """Extracts a usable suffix from the client-supplied filename.

    The filename comes from the multipart body, so it is fully attacker
    controlled. Nothing here ever touches the filesystem, but stripping the
    path and bounding the length keeps a traversal-shaped or absurdly long
    name from reaching a log line or an error message.
    """
    if not raw_name:
        raise HTTPException(status_code=400, detail="Uploaded file has no filename")

    # Handle both separators — a Windows client sends backslashes.
    base = PurePosixPath(raw_name.replace("\\", "/")).name
    if not base or len(base) > MAX_FILENAME_LENGTH:
        raise HTTPException(status_code=400, detail="Invalid filename")

    suffix = Path(base).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail="Only CSV, JSON, and XLSX files are allowed")

    return suffix


def _read_csv(raw: bytes) -> Tuple[pd.DataFrame, RepairReport]:
    try:
        return repair_csv(raw)
    except CSVRepairError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _read_json(raw: bytes) -> pd.DataFrame:
    try:
        data = json.loads(raw)
    except Exception:
        # The parser echoes a slice of the offending document; keep the reason
        # without replaying attacker-supplied bytes back to the client.
        raise HTTPException(status_code=400, detail="Invalid JSON file: could not be parsed")

    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        raise HTTPException(
            status_code=400,
            detail="JSON file must be an array of objects (records) — nested or envelope-wrapped documents aren't supported yet",
        )

    try:
        return pd.json_normalize(data)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON file: could not be flattened into a table")


def _reject_xlsx_zip_bomb(raw: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            declared = sum(info.file_size for info in zf.infolist())
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid XLSX file: not a readable workbook")

    if declared > MAX_XLSX_UNCOMPRESSED_BYTES:
        raise HTTPException(
            status_code=400,
            detail="XLSX file expands to more data than this service will parse",
        )


def _read_xlsx(raw: bytes) -> Tuple[pd.DataFrame, str]:
    _reject_xlsx_zip_bomb(raw)

    try:
        # openpyxl needs a real seekable stream — SpooledTemporaryFile doesn't
        # implement enough of io.IOBase for it, so read the bytes into BytesIO first.
        excel = pd.ExcelFile(io.BytesIO(raw), engine="openpyxl")
        sheet_name = excel.sheet_names[0]
        df = excel.parse(sheet_name)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid XLSX file: could not be parsed")

    return df, sheet_name


@router.post("/upload")
async def upload_csv(file: UploadFile = File(...)):
    suffix = _validate_filename(file.filename)

    raw = _read_upload_bounded(file.file)
    if not raw:
        raise HTTPException(status_code=400, detail="Uploaded file is empty (0 bytes)")

    sheet_name = None
    repair_report = None
    if suffix == ".csv":
        df, repair_report = _read_csv(raw)
    elif suffix == ".json":
        df = _read_json(raw)
    else:
        df, sheet_name = _read_xlsx(raw)

    if df.empty:
        raise HTTPException(status_code=400, detail="Uploaded file has no rows")

    cell_count = df.shape[0] * df.shape[1]
    if cell_count > MAX_CELLS:
        raise HTTPException(
            status_code=400,
            detail=f"Parsed dataset has {cell_count:,} cells, exceeding the {MAX_CELLS:,} cell limit",
        )

    # The CSV path sanitizes formula triggers inside the repair pipeline; JSON
    # and XLSX have no such pipeline, so they get the same treatment here.
    # Export sanitizes again — this keeps what's *stored* clean too, so the
    # SQL tab and previews never surface a live formula string either.
    if suffix != ".csv":
        sanitize_frame_in_place(df)

    dataset_id = create_dataset(df, repair_preview=repair_report.preview if repair_report else None)

    response = {
        "dataset_id": dataset_id,
        "rows": len(df),
        "columns": [str(c) for c in df.columns],
        "column_types": {str(k): v for k, v in df.dtypes.astype(str).to_dict().items()},
    }
    if sheet_name is not None:
        response["sheet_name"] = sheet_name
    if repair_report is not None:
        response["repair_report"] = repair_report.to_dict()

    return response
