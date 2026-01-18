from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import tempfile
import subprocess
import os
from app.core.dataset_manager import get_dataset

router = APIRouter()

class ScriptRunRequest(BaseModel):
    dataset_id: str

@router.post("/run-script")
def run_script_on_dataset(request: ScriptRunRequest):
    ds_entry = get_dataset(request.dataset_id)
    
    if not ds_entry:
        raise HTTPException(status_code=404, detail="Dataset not found")
    
    df = ds_entry["df"]
    csv_path = None
    
    try:
        # Create a temporary CSV file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as tmp_file:
            csv_path = tmp_file.name
            df.to_csv(csv_path, index=False)
        
        # Load and execute your Python script
        script_path = "/home/da3m0n/Desktop/Clients' Websites/Analyzr/backend/app/scripts/your_script.py"
        
        # Run the script with the CSV path as an argument
        result = subprocess.run(
            ['python', script_path, csv_path],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode != 0:
            raise HTTPException(status_code=400, detail=f"Script error: {result.stderr}")
        
        return {
            "success": True,
            "output": result.stdout
        }
    
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail="Script execution timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")
    finally:
        # Cleanup temporary CSV file
        if csv_path and os.path.exists(csv_path):
            os.remove(csv_path)