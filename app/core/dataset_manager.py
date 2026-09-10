"""In-memory dataset store.

Deliberately not a database — the service is stateless by design and loses
everything on restart. That does mean this dict is the whole memory budget
of the process, so it needs its own limits: an unbounded store on an
unauthenticated upload endpoint is a one-command denial of service.

Two bounds, both enforced on insert:
  * a TTL, so an abandoned upload doesn't occupy RAM until the next deploy;
  * a hard count, so a burst of concurrent uploads can't outrun the TTL.
"""

from __future__ import annotations

import threading
import time

import pandas as pd

from app.core.validation import new_dataset_id

DATASETS: dict[str, dict] = {}

# Uploads are session-scoped in the UI; an hour is generous for a working
# session and short enough that garbage doesn't accumulate.
DATASET_TTL_SECONDS = 60 * 60
MAX_DATASETS = 50

_lock = threading.Lock()


def _evict_expired_locked(now: float) -> None:
    expired = [
        ds_id
        for ds_id, entry in DATASETS.items()
        if now - entry["created_at"] > DATASET_TTL_SECONDS
    ]
    for ds_id in expired:
        DATASETS.pop(ds_id, None)


def create_dataset(df: pd.DataFrame, repair_preview: list[dict] | None = None) -> str:
    """`repair_preview`, when given, is a handful of {before, after} row
    snapshots from the CSV repair pipeline — held here rather than shipped in
    the upload response, and fetched on demand via
    GET /api/dataset/{id}/repair-preview."""
    now = time.time()

    with _lock:
        _evict_expired_locked(now)

        # Still full after expiry sweep: drop the oldest to make room, so a
        # live user is never refused because of someone else's stale upload.
        while len(DATASETS) >= MAX_DATASETS:
            oldest = min(DATASETS, key=lambda k: DATASETS[k]["created_at"])
            DATASETS.pop(oldest, None)

        dataset_id = new_dataset_id()
        DATASETS[dataset_id] = {"df": df, "created_at": now, "repair_preview": repair_preview or []}

    return dataset_id


def get_dataset(dataset_id: str):
    entry = DATASETS.get(dataset_id)
    if entry is None:
        return None

    if time.time() - entry["created_at"] > DATASET_TTL_SECONDS:
        with _lock:
            DATASETS.pop(dataset_id, None)
        return None

    return entry


def rename_column(dataset_id: str, column: str, new_name: str) -> pd.DataFrame | None:
    """Renames a column on the stored frame in place, under the same lock as
    every other mutation of DATASETS. Returns the updated frame, or None if
    the dataset doesn't exist (or expired between the caller's own check and
    this call — TTL eviction runs on its own clock)."""
    with _lock:
        entry = DATASETS.get(dataset_id)
        if entry is None:
            return None
        if time.time() - entry["created_at"] > DATASET_TTL_SECONDS:
            DATASETS.pop(dataset_id, None)
            return None

        entry["df"].rename(columns={column: new_name}, inplace=True)
        return entry["df"]


def delete_dataset(dataset_id: str) -> None:
    with _lock:
        DATASETS.pop(dataset_id, None)


def count_datasets() -> int:
    return len(DATASETS)
