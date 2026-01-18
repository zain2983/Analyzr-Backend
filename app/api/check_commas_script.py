from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.core.dataset_manager import get_dataset
import io

router = APIRouter()

class ScriptRunRequest(BaseModel):
    dataset_id: str

def detect_unenclosed_quotes(csv_text):
    """
    Detects lines with unenclosed quotes in raw CSV text
    Returns a list of problematic lines
    """
    issues = []
    for i, line in enumerate(csv_text.split('\n'), 1):
        if not line.strip():  # Skip empty lines
            continue
        quote_count = line.count('"')
        if quote_count % 2 != 0:
            issues.append({
                "line": i,
                "content": line.strip()
            })
    return issues

@router.post("/check-commas")
def run_script_on_dataset(request: ScriptRunRequest):
    ds_entry = get_dataset(request.dataset_id)
    
    if not ds_entry:
        raise HTTPException(status_code=404, detail="Dataset not found")
    
    df = ds_entry["df"]
    
    try:
        # Convert DataFrame back to raw CSV text
        csv_text = df.to_csv(index=False)
        
        issues = detect_unenclosed_quotes(csv_text)
        return {
            "success": True,
            "total_issues": len(issues),
            "issues": issues
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")