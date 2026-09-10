"""Fault-tolerant CSV ingestion pipeline.

Turns arbitrary, possibly-malformed CSV bytes into a clean pandas DataFrame
plus a small, serializable report of what was detected and what was fixed.

Pipeline: bytes -> detect encoding -> decode -> normalize line endings ->
detect delimiter -> quote-aware tokenize -> reconcile header/rows ->
normalize values (missing tokens, whitespace, numbers, dates) -> sanitize
formula-injection risk -> DataFrame + RepairReport.

Design principle carried through every step: repair what's unambiguous,
flag what isn't. Never silently guess on something like decimal convention
or date format when the data itself doesn't resolve the ambiguity.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

try:
    import chardet
except ImportError:  # pragma: no cover - exercised only if dependency missing
    chardet = None


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

@dataclass
class RepairReport:
    encoding: str = "utf-8"
    encoding_confidence: Optional[float] = None
    delimiter: str = ","
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def bump(self, key: str, by: int = 1) -> None:
        if by:
            self.stats[key] = self.stats.get(key, 0) + by

    @property
    def clean(self) -> bool:
        return not self.warnings and not self.stats

    def to_dict(self) -> dict:
        return {
            "clean": self.clean,
            "encoding": self.encoding,
            "encoding_confidence": self.encoding_confidence,
            "delimiter": self.delimiter,
            "warnings": self.warnings,
            "stats": self.stats,
        }


class CSVRepairError(ValueError):
    """Raised for structural problems repair can't recover from — a genuine reject, not a fixable warning."""


# ---------------------------------------------------------------------------
# 0. File-type sniffing — catch a wrong-extension file before we try to
#    decode it as text at all.
# ---------------------------------------------------------------------------

def _reject_if_not_csv_shaped(raw: bytes) -> None:
    if len(raw) == 0:
        raise CSVRepairError("Uploaded file is empty (0 bytes)")

    head = raw[:8]
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06") or head.startswith(b"PK\x07\x08"):
        raise CSVRepairError(
            "This looks like a zip-based binary file (e.g. .xlsx, .docx) renamed to .csv — "
            "upload it with its real extension instead"
        )
    if head.startswith(b"%PDF"):
        raise CSVRepairError("This looks like a PDF file renamed to .csv")
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        raise CSVRepairError("This looks like a legacy Excel (.xls) binary file renamed to .csv")

    # Text-level sniffs (JSON/XML) need a rough decode first; latin-1 never
    # raises, and this is a pre-check only, not the pipeline's real decode.
    probe = raw[:512].decode("latin-1").strip()
    if probe[:1] in "{[":
        try:
            import json

            json.loads(raw.decode("latin-1"))
            raise CSVRepairError(
                "This looks like a JSON file renamed to .csv — upload it as .json instead"
            )
        except CSVRepairError:
            raise
        except Exception:
            pass  # started with { or [ but isn't valid JSON — could be legitimate CSV content
    if probe[:1] == "<" and (probe[:5].lower() in ("<?xml", "<html") or probe[:4].lower() == "<svg"):
        raise CSVRepairError("This looks like an XML/HTML file, not CSV")


# ---------------------------------------------------------------------------
# 1. Encoding
# ---------------------------------------------------------------------------

_BOM_UTF8 = b"\xef\xbb\xbf"
_BOM_UTF16_LE = b"\xff\xfe"
_BOM_UTF16_BE = b"\xfe\xff"


def _detect_and_decode(raw: bytes, report: RepairReport) -> str:
    if raw.startswith(_BOM_UTF8):
        report.encoding = "utf-8-sig"
        return raw.decode("utf-8-sig", errors="replace")

    if raw.startswith(_BOM_UTF16_LE) or raw.startswith(_BOM_UTF16_BE):
        report.encoding = "utf-16"
        report.warn("File is UTF-16 encoded — converted to UTF-8")
        return raw.decode("utf-16", errors="replace")

    try:
        text = raw.decode("utf-8")
        report.encoding = "utf-8"
        return text
    except UnicodeDecodeError:
        pass

    detected_encoding = None
    confidence = None
    if chardet is not None:
        guess = chardet.detect(raw)
        detected_encoding = guess.get("encoding")
        confidence = guess.get("confidence")

    if detected_encoding and (confidence or 0) >= 0.5:
        normalized = detected_encoding.lower().replace("_", "-")
        if normalized in ("utf-16", "utf-16le", "utf-16be"):
            report.warn("File is UTF-16 encoded — converted to UTF-8")
        elif normalized not in ("ascii", "utf-8"):
            report.warn(
                f"File was detected as {detected_encoding} (not UTF-8) — converted to UTF-8; "
                "accented characters were re-decoded, double check them"
            )
        report.encoding = detected_encoding
        report.encoding_confidence = round(confidence, 2) if confidence is not None else None
        try:
            return raw.decode(detected_encoding, errors="replace")
        except LookupError:
            pass

    # Last resort: latin-1 never raises (every byte maps to a codepoint), so
    # this always succeeds — but it's a guess, not a detection, when we get here.
    report.encoding = "latin-1"
    report.warn(
        "Could not confidently detect the file's encoding — fell back to Latin-1. "
        "Accented characters or special symbols may be mangled (mojibake)"
    )
    text = raw.decode("latin-1", errors="replace")
    if "�" in text:
        report.bump("invalid_byte_sequences_replaced", text.count("�"))
    return text


def _strip_bom_char(text: str, report: RepairReport) -> str:
    if text.startswith("﻿"):
        report.warn("Removed a leading byte-order-mark (BOM) character that was stuck to the first header name")
        return text[1:]
    return text


# ---------------------------------------------------------------------------
# 2. Line endings
# ---------------------------------------------------------------------------

def _normalize_line_endings(text: str, report: RepairReport) -> str:
    has_crlf = "\r\n" in text
    has_bare_cr = bool(re.search(r"\r(?!\n)", text))
    has_bare_lf = bool(re.search(r"(?<!\r)\n", text))

    if has_bare_cr and not has_crlf:
        report.warn("File used old classic-Mac-style (\\r only) line endings — normalized to \\n")
    elif (has_crlf and has_bare_lf) or (has_crlf and has_bare_cr):
        report.warn("File mixed Windows (\\r\\n) and Unix/old-Mac line endings in the same file — normalized to \\n")
    # Plain \r\n-only or \n-only is the expected case; nothing to flag.

    return text.replace("\r\n", "\n").replace("\r", "\n")


# ---------------------------------------------------------------------------
# 3. Delimiter detection
# ---------------------------------------------------------------------------

_DELIMITER_CANDIDATES = [",", ";", "\t", "|"]


def _delimiter_consistency(lines: list[str], delim: str) -> tuple[int, int]:
    """Returns (mode_count, rows_agreeing_with_mode) for how many times `delim` appears per line."""
    counts = [line.count(delim) for line in lines if line.strip()]
    if not counts or max(counts) == 0:
        return (0, 0)
    mode = max(set(counts), key=counts.count)
    agree = sum(1 for c in counts if c == mode)
    return (mode, agree)


def _detect_delimiter(text: str, report: RepairReport) -> str:
    sample_lines = text.split("\n")[:200]

    scored = {d: _delimiter_consistency(sample_lines, d) for d in _DELIMITER_CANDIDATES}
    # Prefer the delimiter with the most rows agreeing on a non-zero mode count.
    best = max(_DELIMITER_CANDIDATES, key=lambda d: (scored[d][0] > 0, scored[d][1]))
    best_mode, best_agree = scored[best]

    if best_mode == 0:
        report.warn(
            "No consistent delimiter (comma/semicolon/tab/pipe) was detected — this may be a "
            "single-column file, or not actually delimited data (e.g. fixed-width)"
        )
        return ","

    non_blank = sum(1 for l in sample_lines if l.strip())
    if non_blank and best_agree < non_blank:
        # Some lines disagree on the delimiter count entirely — check whether a
        # *different* candidate is a strong match specifically on those rows.
        runner_up = max(
            (d for d in _DELIMITER_CANDIDATES if d != best),
            key=lambda d: scored[d][1],
        )
        if scored[runner_up][1] > 0 and scored[runner_up][0] > 0:
            report.warn(
                f"Delimiter usage looks inconsistent within the file — mostly '{best}' "
                f"but some rows look like they use '{runner_up}'; parsed with '{best}' throughout"
            )

    if best == ";":
        report.warn("Detected ';' as the delimiter (common in locale exports where ',' is the decimal separator)")
    elif best == "\t":
        report.warn("Detected the file is tab-separated, not comma-separated, despite the .csv extension")
    elif best == "|":
        report.warn("Detected the file is pipe-separated ('|'), not comma-separated, despite the .csv extension")

    return best


# ---------------------------------------------------------------------------
# 4. Quote-aware tokenizer
# ---------------------------------------------------------------------------

def _tokenize(text: str, delimiter: str, report: RepairReport) -> list[list[str]]:
    """Turns normalized text into rows of raw string fields.

    Lenient, best-effort quote handling: doubled quotes are the standard
    escape; an unescaped quote that isn't immediately followed by a delimiter
    or newline is treated as literal content rather than ending the field
    (recovers cases like `"He said "hi" to me"`); a quote that never finds a
    plausible close is force-closed at EOF rather than swallowing the rest of
    the file. Embedded real newlines inside an open quote are preserved as
    field content, not treated as row breaks.
    """
    rows: list[list[str]] = []
    row: list[str] = []
    field_chars: list[str] = []
    in_quotes = False
    quotes_repaired = 0

    n = len(text)
    i = 0
    while i < n:
        c = text[i]

        if in_quotes:
            if c == '"':
                nxt = text[i + 1] if i + 1 < n else None
                if nxt == '"':
                    field_chars.append('"')
                    i += 2
                    continue
                # Tolerate trailing whitespace between the closing quote and the
                # delimiter/newline/EOF (e.g. `"value" ,`), rather than requiring
                # them to be adjacent.
                j = i + 1
                while j < n and text[j] in " \t":
                    j += 1
                following = text[j] if j < n else None
                if following is None or following == delimiter or following == "\n":
                    in_quotes = False
                    i = j
                    continue
                # Stray/unescaped quote inside a quoted field — keep it literal.
                field_chars.append('"')
                quotes_repaired += 1
                i += 1
                continue
            field_chars.append(c)
            i += 1
            continue

        if c == '"' and (not field_chars or all(ch in " \t" for ch in field_chars)):
            in_quotes = True
            field_chars = []  # discard leading whitespace before the opening quote
            i += 1
            continue
        if c == '"':
            # Quote shows up mid-field (not at a plausible opening position) — literal.
            field_chars.append(c)
            i += 1
            continue
        if c == delimiter:
            row.append("".join(field_chars))
            field_chars = []
            i += 1
            continue
        if c == "\n":
            field_val = "".join(field_chars)
            if not row and field_val == "":
                report.bump("blank_lines_skipped")
            else:
                row.append(field_val)
                rows.append(row)
            row = []
            field_chars = []
            i += 1
            continue

        field_chars.append(c)
        i += 1

    if in_quotes:
        quotes_repaired += 1

    field_val = "".join(field_chars)
    if row or field_val != "":
        row.append(field_val)
        rows.append(row)

    if quotes_repaired:
        report.bump("quotes_repaired", quotes_repaired)
        report.warn(
            f"Repaired {quotes_repaired} unescaped/unterminated quote character(s) "
            "(e.g. a doubled quote that wasn't escaped, or a quote left open until end of file)"
        )

    return rows


# ---------------------------------------------------------------------------
# 5. Header detection + ragged-row reconciliation
# ---------------------------------------------------------------------------

_NUMERIC_RE = re.compile(r"^[+-]?[\d,.]+%?$")


def _looks_numeric(value: str) -> bool:
    v = value.strip()
    if not v:
        return False
    return bool(_NUMERIC_RE.match(v))


def _mode_row_length(rows: list[list[str]]) -> int:
    lengths = [len(r) for r in rows]
    if not lengths:
        return 0
    return max(set(lengths), key=lengths.count)


def _split_header_and_rows(rows: list[list[str]], report: RepairReport) -> tuple[list[str], list[list[str]]]:
    if not rows:
        raise CSVRepairError("Uploaded file has no rows")

    # Skip leading metadata/title rows: a lone short row (usually a single
    # cell) sitting above a block that's clearly the real tabular data.
    body = rows
    data_mode_len = _mode_row_length(rows[1:]) if len(rows) > 1 else len(rows[0])
    skipped_titles = 0
    while len(body) > 1 and data_mode_len > 1 and len(body[0]) == 1 and skipped_titles < 3:
        title = ",".join(body[0]).strip()
        report.warn(f"Skipped a metadata/title row before the header: \"{title[:80]}\"")
        body = body[1:]
        skipped_titles += 1
        data_mode_len = _mode_row_length(body[1:]) if len(body) > 1 else len(body[0])

    header_row = body[0]
    data_rows = body[1:]

    # No-header heuristic: does row 0 look like a label row, or just more data?
    # Only positive evidence counts — a header row where every cell already
    # looks like a number is the clear "this is data, not labels" case. Absence
    # of a numeric contrast (e.g. a "name"/"note" text column) is not evidence
    # either way, so it must not trigger this.
    if data_rows and header_row:
        header_numeric_fraction = sum(_looks_numeric(h) for h in header_row) / len(header_row)
        if header_numeric_fraction >= 0.8:
            report.warn(
                "No header row detected — the first row looks like data, not column names. "
                "Generated placeholder column names (col_1, col_2, ...)"
            )
            data_rows = [header_row] + data_rows
            header_row = [f"col_{i + 1}" for i in range(len(header_row))]

    # Trim stray whitespace on header names (case is left alone — it's meaningful/user-visible).
    trimmed_header = []
    any_trimmed = False
    for h in header_row:
        t = h.strip()
        if t != h:
            any_trimmed = True
        trimmed_header.append(t if t else "")
    if any_trimmed:
        report.warn("Trimmed stray leading/trailing whitespace from header names")
    header_row = trimmed_header

    # Fill blank header cells (e.g. trailing-comma phantom column).
    header_row = [h if h else f"column_{i + 1}" for i, h in enumerate(header_row)]

    # Case/whitespace-insensitive collisions worth flagging (not auto-merging —
    # merging column identity is a decision for the user, same stance as Compare Tab fuzzy matching).
    seen_normalized: dict[str, list[str]] = {}
    for h in header_row:
        key = h.strip().lower()
        seen_normalized.setdefault(key, []).append(h)
    near_dupes = [names for names in seen_normalized.values() if len(set(names)) > 1]
    for names in near_dupes:
        report.warn(f"Columns differ only by case/whitespace and may be the same field: {', '.join(sorted(set(names)))}")

    # Exact duplicate column names — disambiguate rather than silently drop data.
    seen_exact: dict[str, int] = {}
    deduped_header = []
    dupes_found = []
    for h in header_row:
        if h in seen_exact:
            seen_exact[h] += 1
            new_name = f"{h}_{seen_exact[h]}"
            dupes_found.append(h)
            deduped_header.append(new_name)
        else:
            seen_exact[h] = 1
            deduped_header.append(h)
    if dupes_found:
        report.warn(f"Duplicate column name(s) renamed to stay unique: {', '.join(sorted(set(dupes_found)))}")

    header_data_mode = _mode_row_length(data_rows) if data_rows else len(deduped_header)
    if header_data_mode and header_data_mode != len(deduped_header):
        report.warn(
            f"Header has {len(deduped_header)} column(s) but data rows mostly have {header_data_mode} — "
            "trailing columns may be unused or data may be missing a column"
        )

    return deduped_header, data_rows


def _reconcile_rows(header: list[str], rows: list[list[str]], delimiter: str, report: RepairReport) -> list[list[str]]:
    target_len = len(header)
    reconciled = []
    header_normalized = [h.strip().lower() for h in header]
    padded = 0
    merged = 0
    repeated_headers_dropped = 0

    for row in rows:
        row_normalized = [v.strip().lower() for v in row]
        if row_normalized == header_normalized:
            repeated_headers_dropped += 1
            continue

        if len(row) == target_len:
            reconciled.append(row)
        elif len(row) < target_len:
            reconciled.append(row + [None] * (target_len - len(row)))
            padded += 1
        else:
            # Extra fields almost always mean an unescaped delimiter inside the
            # last intended value — best-effort recombine rather than truncate.
            merged_row = row[: target_len - 1] + [delimiter.join(row[target_len - 1:])]
            reconciled.append(merged_row)
            merged += 1

    if padded:
        report.bump("rows_padded", padded)
        report.warn(f"Padded {padded} row(s) that had fewer columns than the header (missing trailing values)")
    if merged:
        report.bump("rows_overflow_merged", merged)
        report.warn(
            f"Merged extra column(s) back together in {merged} row(s) that had more fields than the header "
            "(likely an unescaped delimiter inside a value)"
        )
    if repeated_headers_dropped:
        report.bump("repeated_header_rows_dropped", repeated_headers_dropped)
        report.warn(
            f"Dropped {repeated_headers_dropped} repeated header row(s) found partway through the file "
            "(looks like multiple exports were concatenated together)"
        )

    if reconciled and reconciled[-1] and any(v in (None, "") for v in reconciled[-1]) and len(rows) > 0:
        # Heuristic-only nudge; real truncation is already captured by the
        # padded-row warning above when it applies to the final row.
        pass

    return reconciled


# ---------------------------------------------------------------------------
# 6. Value-level normalization
# ---------------------------------------------------------------------------

_MISSING_TOKENS = {
    "null", "na", "n/a", "-", "--", "none", "#n/a", "undefined", "nan",
}

_ZERO_WIDTH_RE = re.compile("[​‌‍﻿]")
_NBSP = " "


def _clean_whitespace(value: str) -> str:
    value = value.replace(_NBSP, " ")
    value = _ZERO_WIDTH_RE.sub("", value)
    value = value.replace("\t", " ")
    return value.strip()


def _normalize_cell(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    cleaned = _clean_whitespace(raw)
    if cleaned == "" or cleaned.lower() in _MISSING_TOKENS:
        return None
    return cleaned


def _build_raw_dataframe(header: list[str], rows: list[list[str]], report: RepairReport) -> pd.DataFrame:
    missing_tokens_seen = 0
    normalized_rows = []
    for row in rows:
        normalized_row = []
        for value in row:
            normalized = _normalize_cell(value)
            if value is not None and normalized is None and value.strip() != "":
                missing_tokens_seen += 1
            normalized_row.append(normalized)
        normalized_rows.append(normalized_row)

    if missing_tokens_seen:
        report.bump("missing_value_tokens_normalized", missing_tokens_seen)
        report.warn(
            f"Normalized {missing_tokens_seen} differently-spelled missing-value marker(s) "
            '(e.g. NULL, N/A, "-", "None", "#N/A") to a single consistent empty value'
        )

    return pd.DataFrame(normalized_rows, columns=header, dtype=object)


# ---------------------------------------------------------------------------
# 7. Number normalization
# ---------------------------------------------------------------------------

_CURRENCY_CHARS = "$€£¥"
_ACCOUNTING_NEGATIVE_RE = re.compile(r"^\(\s*[" + re.escape(_CURRENCY_CHARS) + r"]?\s*[\d,.]+\s*\)$")
_NUMERIC_WITH_SYMBOLS_RE = re.compile(
    r"^[+-]?[" + re.escape(_CURRENCY_CHARS) + r"]?\s*\(?[" + re.escape(_CURRENCY_CHARS) + r"]?\s*[\d,.]+\s*\)?%?$"
)
_EU_DECIMAL_RE = re.compile(r"^-?\d{1,3}(\.\d{3})+,\d+$")
_US_DECIMAL_RE = re.compile(r"^-?\d{1,3}(,\d{3})+\.\d+$")
_LEADING_ZERO_CODE_RE = re.compile(r"^0\d+$")
_SCI_NOTATION_RE = re.compile(r"^\d(\.\d+)?e\+?\d+$", re.IGNORECASE)


def _clean_numeric_token(token: str, eu_decimal: bool = False) -> Optional[float]:
    t = token.strip()
    negative = False
    if _ACCOUNTING_NEGATIVE_RE.match(t):
        negative = True
        t = t.strip("()").strip()
    for ch in _CURRENCY_CHARS:
        t = t.replace(ch, "")
    t = t.strip().rstrip("%")
    if eu_decimal:
        t = t.replace(".", "").replace(",", ".")
    else:
        t = t.replace(",", "")
    try:
        val = float(t)
    except ValueError:
        return None
    return -val if negative else val


def _normalize_numeric_columns(df: pd.DataFrame, report: RepairReport) -> None:
    for col in df.columns:
        series = df[col]
        non_null = series.dropna().astype(str)
        if non_null.empty:
            continue

        # Gate everything below on the column actually being numeric-shaped —
        # otherwise an ordinary text column that happens to contain a comma
        # (e.g. a merged-overflow artifact, or free text) gets misread as an
        # "ambiguous number".
        loose_matches = sum(
            bool(_NUMERIC_WITH_SYMBOLS_RE.match(v.strip())) or bool(_SCI_NOTATION_RE.match(v.strip()))
            for v in non_null
        )
        if loose_matches / len(non_null) < 0.9:
            continue

        if any(_LEADING_ZERO_CODE_RE.match(v.strip()) for v in non_null):
            report.warn(
                f"Column '{col}' has values with leading zeros (e.g. ZIP/account-style codes) — "
                "kept as text so the zeros aren't stripped"
            )
            continue

        sci_matches = sum(bool(_SCI_NOTATION_RE.match(v.strip())) for v in non_null)
        if sci_matches:
            report.warn(
                f"Column '{col}' has {sci_matches} value(s) in scientific notation (likely mangled by Excel, "
                "e.g. '1.23E+14') — parsed back to a number, but any lost digits can't be recovered"
            )

        # A thousands-grouped EU value (e.g. "1.234,56") or a thousands-grouped
        # US value (e.g. "1,234.56" — 4+ digits before the decimal) is
        # unambiguous evidence of the whole column's convention; a bare
        # single-comma value like "1,234" alone is not, since it reads
        # equally validly as either "one thousand two hundred thirty-four"
        # or "one point two three four".
        has_eu_evidence = any(_EU_DECIMAL_RE.match(v.strip()) for v in non_null)
        has_us_evidence = any(_US_DECIMAL_RE.match(v.strip()) for v in non_null)

        percent_count = sum(1 for v in non_null if v.strip().endswith("%"))
        accounting_count = sum(1 for v in non_null if _ACCOUNTING_NEGATIVE_RE.match(v.strip()))

        if has_eu_evidence and not has_us_evidence:
            cleaned = series.map(lambda v: _clean_numeric_token(v, eu_decimal=True) if isinstance(v, str) else v)
            df[col] = pd.to_numeric(cleaned, errors="coerce")
            report.warn(f"Column '{col}' uses European-style decimal commas (e.g. '1.234,56') — converted to standard decimal notation")
            continue

        # Ambiguous: comma-only values with no other signal in the column
        # (no thousands-grouped example either way) can't be told apart from
        # a European decimal comma.
        if not has_us_evidence:
            comma_only = [v.strip() for v in non_null if "," in v and "." not in v]
            single_comma_values = [v for v in comma_only if v.count(",") == 1]
            if single_comma_values:
                report.warn(
                    f"Column '{col}' has comma-containing numbers with no other signal to tell whether ',' is a "
                    "thousands separator or a decimal point (e.g. '1,234') — left as text rather than guessing"
                )
                continue

        cleaned_values = series.map(lambda v: _clean_numeric_token(v) if isinstance(v, str) else v)
        numeric = pd.to_numeric(cleaned_values, errors="coerce")
        df[col] = numeric

        symbol_fixes = sum(1 for v in non_null if any(c in v for c in _CURRENCY_CHARS) or "," in v)
        if symbol_fixes:
            report.bump("numeric_symbols_stripped", symbol_fixes)
        if percent_count:
            report.warn(f"Column '{col}' has {percent_count} value(s) with a '%' suffix — stripped, value kept as-is (not divided by 100)")
        if accounting_count:
            report.bump("accounting_negatives_converted", accounting_count)
            report.warn(f"Column '{col}' has {accounting_count} value(s) in accounting negative format (e.g. '(1,234.56)') — converted to -1234.56 style")
        if symbol_fixes and not percent_count and not accounting_count and not sci_matches:
            report.warn(f"Column '{col}' had currency symbols or thousands separators unquoted — stripped and parsed as numbers")


# ---------------------------------------------------------------------------
# 8. Date normalization
# ---------------------------------------------------------------------------

_TWO_DIGIT_YEAR_RE = re.compile(r"^\d{1,2}[/-]\d{1,2}[/-]\d{2}$")
_DATE_NAME_HINT_RE = re.compile(r"date|dob|created|updated|time", re.IGNORECASE)


def _normalize_date_columns(df: pd.DataFrame, report: RepairReport) -> None:
    for col in df.columns:
        if pd.api.types.is_numeric_dtype(df[col]):
            series = df[col]
            non_null = series.dropna()
            if (
                _DATE_NAME_HINT_RE.search(str(col))
                and not non_null.empty
                and non_null.between(1, 60000).mean() > 0.9
            ):
                report.warn(
                    f"Column '{col}' looks like it may contain Excel serial date numbers (e.g. 45292) based on "
                    "its name — left as a number since this is a guess; verify before converting"
                )
            continue

        series = df[col]
        non_null = series.dropna().astype(str)
        if non_null.empty or len(non_null) < 3:
            continue

        # Quick pre-filter: does this even look date-shaped?
        date_like_fraction = non_null.map(lambda v: bool(re.match(r"^\d{1,4}[/\-.]\d{1,2}[/\-.]\d{1,4}", v.strip()) or bool(re.match(r"^[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}", v.strip())))).mean()
        if date_like_fraction < 0.6:
            continue

        two_digit_year_count = sum(bool(_TWO_DIGIT_YEAR_RE.match(v.strip())) for v in non_null)

        parsed_day_first = pd.to_datetime(series, errors="coerce", dayfirst=True, format="mixed")
        parsed_month_first = pd.to_datetime(series, errors="coerce", dayfirst=False, format="mixed")

        success_day_first = parsed_day_first.notna().sum()
        success_month_first = parsed_month_first.notna().sum()
        total = len(series)

        if success_day_first == 0 and success_month_first == 0:
            continue

        # Rows where both interpretations succeed but disagree are the
        # genuinely ambiguous ones (e.g. 03/04/2024).
        both_valid = parsed_day_first.notna() & parsed_month_first.notna()
        disagree = (parsed_day_first[both_valid] != parsed_month_first[both_valid]).sum()

        if disagree > 0 and abs(success_day_first - success_month_first) <= max(1, int(0.05 * total)):
            report.warn(
                f"Column '{col}' has {disagree} date(s) that are ambiguous between day-first and month-first "
                "formats (e.g. 03/04/2024) — left as original text rather than guessing; consider re-exporting with ISO 8601 dates"
            )
            continue

        use_day_first = success_day_first >= success_month_first
        chosen = parsed_day_first if use_day_first else parsed_month_first
        chosen_success = success_day_first if use_day_first else success_month_first

        if chosen_success / total < 0.6:
            continue  # not confidently a date column after all

        formats_seen = non_null.map(lambda v: re.sub(r"\d", "#", v.strip())).nunique()
        if formats_seen > 1:
            report.warn(f"Column '{col}' mixed multiple date formats — normalized to ISO 8601 (YYYY-MM-DD) where parseable")
        else:
            report.warn(
                f"Column '{col}' dates normalized to ISO 8601 (YYYY-MM-DD), assuming "
                f"{'day-first' if use_day_first else 'month-first'} format"
            )

        if two_digit_year_count:
            report.warn(
                f"Column '{col}' has {two_digit_year_count} date(s) with a 2-digit year — assumed 00-68 => 2000s, "
                "69-99 => 1900s; verify this matches your source system"
            )

        df[col] = chosen.dt.strftime("%Y-%m-%d").where(chosen.notna(), None)
        report.bump("date_columns_normalized")


# ---------------------------------------------------------------------------
# 9. Formula-injection sanitization
# ---------------------------------------------------------------------------

_FORMULA_TRIGGER_RE = re.compile(r"^[=@]|^[+\-](?!\d|\.\d)")


def _sanitize_formula_injection(df: pd.DataFrame, report: RepairReport) -> None:
    sanitized = 0
    for col in df.columns:
        if not pd.api.types.is_object_dtype(df[col]):
            continue

        def fix(v):
            nonlocal sanitized
            if isinstance(v, str) and _FORMULA_TRIGGER_RE.match(v):
                sanitized += 1
                return "'" + v
            return v

        df[col] = df[col].map(fix)

    if sanitized:
        report.bump("formula_injection_cells_sanitized", sanitized)
        report.warn(
            f"Sanitized {sanitized} cell(s) starting with '=', '@', or a non-numeric '+'/'-' "
            "to prevent them from executing as spreadsheet formulas if opened in Excel/Sheets"
        )


# ---------------------------------------------------------------------------
# 10. Whole-dataset checks
# ---------------------------------------------------------------------------

def _flag_mixed_types_and_duplicates(df: pd.DataFrame, report: RepairReport) -> None:
    for col in df.columns:
        if not pd.api.types.is_object_dtype(df[col]):
            continue
        non_null = df[col].dropna()
        if len(non_null) < 5:
            continue
        numeric_like = non_null.map(lambda v: _looks_numeric(str(v))).mean()
        if 0.15 <= numeric_like <= 0.85:
            report.warn(f"Column '{col}' mixes numeric-looking and text values — kept as text")

    dup_count = int(df.duplicated().sum())
    if dup_count:
        report.bump("duplicate_rows", dup_count)
        report.warn(f"Found {dup_count} exact duplicate row(s) — left in place, since duplicates may be intentional")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def repair_csv(raw: bytes) -> tuple[pd.DataFrame, RepairReport]:
    report = RepairReport()

    _reject_if_not_csv_shaped(raw)

    text = _detect_and_decode(raw, report)
    text = _strip_bom_char(text, report)
    text = _normalize_line_endings(text, report)

    if text.strip() == "":
        raise CSVRepairError("Uploaded file has no rows")

    delimiter = _detect_delimiter(text, report)
    report.delimiter = delimiter

    rows = _tokenize(text, delimiter, report)
    header, data_rows = _split_header_and_rows(rows, report)

    if not data_rows:
        raise CSVRepairError("Uploaded file has no rows")

    reconciled_rows = _reconcile_rows(header, data_rows, delimiter, report)
    if not reconciled_rows:
        raise CSVRepairError("Uploaded file has no rows")

    df = _build_raw_dataframe(header, reconciled_rows, report)

    _normalize_numeric_columns(df, report)
    _normalize_date_columns(df, report)
    _sanitize_formula_injection(df, report)
    _flag_mixed_types_and_duplicates(df, report)

    return df, report
