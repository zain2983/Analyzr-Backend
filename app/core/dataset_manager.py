import pandas as pd
import uuid
import time

DATASETS = {}

def create_dataset(df: pd.DataFrame):
    dataset_id = str(uuid.uuid4())
    DATASETS[dataset_id] = {
        "df": df,
        "created_at": time.time()
    }
    return dataset_id

def get_dataset(dataset_id: str):
    return DATASETS.get(dataset_id)

def delete_dataset(dataset_id: str):
    DATASETS.pop(dataset_id, None)
