from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.dataset_manager import get_dataset
from app.core.validation import validate_dataset_id

router = APIRouter()

# The response embeds the offending line's text. Bounding both the count and
# each line's length keeps a pathological dataset from turning this endpoint
# into a multi-megabyte JSON amplifier.
MAX_REPORTED_ISSUES = 500
MAX_LINE_PREVIEW_CHARS = 500


class ScriptRunRequest(BaseModel):
    dataset_id: str = Field(min_length=1, max_length=64)


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
                "content": line.strip()[:MAX_LINE_PREVIEW_CHARS]
            })
            if len(issues) >= MAX_REPORTED_ISSUES:
                break
    return issues


@router.post("/check-commas")
def run_script_on_dataset(request: ScriptRunRequest):
    validate_dataset_id(request.dataset_id)
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
            "truncated": len(issues) >= MAX_REPORTED_ISSUES,
            "issues": issues
        }
    except Exception:
        # Don't echo the raw exception — pandas messages can carry file paths
        # and internal detail that don't help the caller.
        raise HTTPException(status_code=500, detail="Failed to scan the dataset for quote issues")
