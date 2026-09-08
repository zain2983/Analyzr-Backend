from fastapi import APIRouter, HTTPException
from app.core.dataset_manager import DATASETS, get_dataset, delete_dataset

router = APIRouter()

@router.get("/datasets")
def list_datasets():
    return {"dataset_ids": list(DATASETS.keys())}

@router.delete("/dataset/{dataset_id}")
def remove_dataset(dataset_id: str):
    if not get_dataset(dataset_id):
        raise HTTPException(status_code=404, detail="Dataset not found")

    delete_dataset(dataset_id)
    return {"dataset_id": dataset_id, "deleted": True}
