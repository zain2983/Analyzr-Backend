"""Input validation shared across the API layer."""

from __future__ import annotations

import re
import uuid

from fastapi import HTTPException

# Dataset ids are always uuid4 strings minted by create_dataset. Validating
# the shape before the value reaches a lookup, a log line, or a response
# header means nothing else downstream has to wonder what might be in it.
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def validate_dataset_id(dataset_id: str) -> str:
    if not isinstance(dataset_id, str) or not _UUID_RE.match(dataset_id):
        # Deliberately the same 404 an unknown-but-well-formed id gets, so the
        # endpoint doesn't distinguish "malformed" from "not yours".
        raise HTTPException(status_code=404, detail="Dataset not found")
    return dataset_id


def is_dataset_id(value) -> bool:
    """Non-raising variant, for filtering a caller-supplied batch of ids."""
    return isinstance(value, str) and bool(_UUID_RE.match(value))


def safe_download_filename(name: str, fallback: str = "dataset.csv") -> str:
    """Reduces a user-supplied name to something safe for Content-Disposition.

    A filename reaches an HTTP header, so a CR or LF in it splits the response
    and lets the caller inject headers of their own; a quote breaks out of the
    quoted-string form; a slash or `..` steers where the browser writes. Rather
    than escaping each case, keep a conservative character set.
    """
    if not isinstance(name, str):
        return fallback

    base = name.replace("\\", "/").split("/")[-1]
    cleaned = re.sub(r'[^A-Za-z0-9._ -]', "_", base).strip(" .")

    if not cleaned:
        return fallback
    if not cleaned.lower().endswith(".csv"):
        cleaned = f"{cleaned}.csv"

    return cleaned[:120]


def new_dataset_id() -> str:
    return str(uuid.uuid4())
