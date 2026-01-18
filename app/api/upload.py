from fastapi import APIRouter, UploadFile, File, HTTPException
import pandas as pd
from app.core.dataset_manager import create_dataset

router = APIRouter()

MAX_FILE_SIZE_MB = 20

@router.post("/upload")
async def upload_csv(file: UploadFile = File(...)):
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files are allowed")

    # Optional: file size check
    file.file.seek(0, 2)
    size_mb = file.file.tell() / (1024 * 1024)
    file.file.seek(0)

    if size_mb > MAX_FILE_SIZE_MB:
        raise HTTPException(
            status_code=400,
            detail=f"File too large. Max allowed size is {MAX_FILE_SIZE_MB} MB"
        )

    try:
        df = pd.read_csv(file.file)
        print("Filename : ", file.filename)
        print("Columns :", ", ".join(df.columns))
        print("Number of rows :", len(df))
        # print("[INFO] -------------------- READ THE CSV SUCCESSFULLY")
        # print(df.head())
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid CSV file: {str(e)}")

    if df.empty:
        raise HTTPException(status_code=400, detail="Uploaded CSV is empty")

    dataset_id = create_dataset(df)
    print(f"Dataset_id : {dataset_id}")

    return {
        "dataset_id": dataset_id,
        "rows": len(df),
        "columns": list(df.columns)
    }
