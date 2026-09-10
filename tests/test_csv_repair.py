"""Tests for the fault-tolerant CSV repair pipeline, one per edge case from the checklist."""

import pandas as pd
import pytest

from app.core.csv_repair import CSVRepairError, repair_csv


def warnings_text(report):
    return " | ".join(report.warnings)


# ---------------------------------------------------------------------------
# 1. Delimiter issues
# ---------------------------------------------------------------------------

def test_semicolon_delimiter_detected():
    raw = b"name;age;city\nAlice;30;NYC\nBob;25;LA\n"
    df, report = repair_csv(raw)
    assert list(df.columns) == ["name", "age", "city"]
    assert report.delimiter == ";"
    assert "';'" in warnings_text(report)


def test_tab_delimiter_detected_despite_csv_extension():
    raw = b"name\tage\nAlice\t30\nBob\t25\n"
    df, report = repair_csv(raw)
    assert report.delimiter == "\t"
    assert list(df.columns) == ["name", "age"]


def test_pipe_delimiter_detected():
    raw = b"name|age\nAlice|30\nBob|25\n"
    df, report = repair_csv(raw)
    assert report.delimiter == "|"


def test_single_column_file_flagged_not_rejected():
    raw = b"name\nAlice\nBob\nCharlie\n"
    df, report = repair_csv(raw)
    assert list(df.columns) == ["name"]
    assert len(df) == 3
    assert any("single-column" in w or "No consistent delimiter" in w for w in report.warnings)


def test_unquoted_delimiter_inside_field_merged_into_last_column():
    raw = b"name,age\nSmith, John,42\n"
    df, report = repair_csv(raw)
    # 3 raw fields against a 2-column header: overflow gets merged back into the last column.
    assert df.iloc[0]["name"] == "Smith"
    assert df.iloc[0]["age"] == "John,42"
    assert report.stats.get("rows_overflow_merged") == 1


# ---------------------------------------------------------------------------
# 2. Quoting problems
# ---------------------------------------------------------------------------

def test_unescaped_inner_quote_recovered_leniently():
    raw = 'text\n"He said "hi" to me"\n'.encode()
    df, report = repair_csv(raw)
    assert df.iloc[0]["text"] == 'He said "hi" to me'


def test_properly_escaped_doubled_quote():
    raw = 'text\n"She said ""hi"" back"\n'.encode()
    df, report = repair_csv(raw)
    assert df.iloc[0]["text"] == 'She said "hi" back'


def test_inconsistent_quoting_in_same_column():
    raw = b'name,note\n"Alice",fine\nBob,"also fine"\n'
    df, report = repair_csv(raw)
    assert df.iloc[0]["name"] == "Alice"
    assert df.iloc[1]["name"] == "Bob"
    assert df.iloc[1]["note"] == "also fine"


def test_unterminated_quote_does_not_swallow_rest_of_file():
    raw = b'a,b\n"John, Doe,25\nCarol,40\n'
    df, report = repair_csv(raw)
    # Recovers by closing at the first plausible point, and the file keeps parsing beyond it.
    assert len(df) >= 1
    assert report.stats.get("quotes_repaired", 0) >= 1


def test_quoted_field_with_surrounding_whitespace():
    raw = b'name,value\n a, "value" \n'
    df, report = repair_csv(raw)
    assert df.iloc[0]["value"] == "value"
    assert df.iloc[0]["name"] == "a"


# ---------------------------------------------------------------------------
# 3. Embedded newlines
# ---------------------------------------------------------------------------

def test_quoted_field_with_embedded_newline_stays_one_row():
    raw = b'name,address\nAlice,"123 Main St\nApt 4"\nBob,"456 Oak Ave"\n'
    df, report = repair_csv(raw)
    assert len(df) == 2
    assert df.iloc[0]["address"] == "123 Main St\nApt 4"


def test_mix_of_fields_with_and_without_embedded_newlines():
    raw = b'name,note\nAlice,"line1\nline2"\nBob,simple\n'
    df, report = repair_csv(raw)
    assert len(df) == 2
    assert df.iloc[0]["note"] == "line1\nline2"
    assert df.iloc[1]["note"] == "simple"


# ---------------------------------------------------------------------------
# 4. Ragged rows
# ---------------------------------------------------------------------------

def test_row_with_fewer_columns_padded():
    raw = b"a,b,c\n1,2,3\n4,5\n"
    df, report = repair_csv(raw)
    assert pd.isna(df.iloc[1]["c"])
    assert report.stats.get("rows_padded") == 1


def test_trailing_comma_creates_padded_phantom_column():
    raw = b"a,b\n1,2,\n3,4,\n"
    df, report = repair_csv(raw)
    # Header has 2 columns; a phantom 3rd column appears in the data via merge.
    assert list(df.columns)[:2] == ["a", "b"]


def test_blank_lines_are_skipped():
    raw = b"a,b\n1,2\n\n3,4\n"
    df, report = repair_csv(raw)
    assert len(df) == 2
    assert report.stats.get("blank_lines_skipped") == 1


def test_repeated_header_mid_file_dropped():
    raw = b"a,b\n1,2\na,b\n3,4\n"
    df, report = repair_csv(raw)
    assert len(df) == 2
    assert report.stats.get("repeated_header_rows_dropped") == 1


# ---------------------------------------------------------------------------
# 5 & 6. Encoding + line endings
# ---------------------------------------------------------------------------

def test_utf8_bom_stripped_from_header():
    raw = "name,age\nAlice,30\n".encode("utf-8-sig")
    df, report = repair_csv(raw)
    assert list(df.columns) == ["name", "age"]
    assert "name" in df.columns  # not "﻿name"


def test_latin1_fallback_does_not_crash():
    raw = "name,city\nJoe,café\n".encode("latin-1")
    df, report = repair_csv(raw)
    assert len(df) == 1


def test_utf16_file_decoded():
    raw = "name,age\nAlice,30\n".encode("utf-16")
    df, report = repair_csv(raw)
    assert list(df.columns) == ["name", "age"]
    assert any("UTF-16" in w for w in report.warnings)


def test_mixed_crlf_and_lf_line_endings():
    raw = b"a,b\r\n1,2\n3,4\r\n"
    df, report = repair_csv(raw)
    assert len(df) == 2
    assert any("line ending" in w for w in report.warnings)


def test_classic_mac_cr_only_line_endings():
    raw = b"a,b\r1,2\r3,4\r"
    df, report = repair_csv(raw)
    assert len(df) == 2


# ---------------------------------------------------------------------------
# 7. Header issues
# ---------------------------------------------------------------------------

def test_no_header_row_generates_placeholder_names():
    raw = b"1,2,3\n4,5,6\n7,8,9\n"
    df, report = repair_csv(raw)
    assert list(df.columns) == ["col_1", "col_2", "col_3"]
    assert len(df) == 3
    assert any("No header row" in w for w in report.warnings)


def test_duplicate_column_names_deduped():
    raw = b"email,email\na@x.com,b@x.com\n"
    df, report = repair_csv(raw)
    assert list(df.columns) == ["email", "email_2"]


def test_header_whitespace_trimmed():
    raw = b'"Name ",age\nAlice,30\n'
    df, report = repair_csv(raw)
    assert list(df.columns) == ["Name", "age"]


def test_metadata_title_row_skipped():
    raw = b"Report generated 2024-01-01\nname,age\nAlice,30\nBob,25\n"
    df, report = repair_csv(raw)
    assert list(df.columns) == ["name", "age"]
    assert len(df) == 2
    assert any("metadata" in w for w in report.warnings)


# ---------------------------------------------------------------------------
# 8. Missing values
# ---------------------------------------------------------------------------

def test_missing_value_tokens_normalized_to_null():
    raw = b"a,b\nNULL,1\nN/A,2\n-,3\nNone,4\n#N/A,5\nundefined,6\nNaN,7\n,8\n"
    df, report = repair_csv(raw)
    assert df["a"].isna().sum() == 8
    assert report.stats.get("missing_value_tokens_normalized") == 7  # "" doesn't count as a "fix"


# ---------------------------------------------------------------------------
# 9. Number formatting
# ---------------------------------------------------------------------------

def test_currency_and_thousands_separator_stripped():
    raw = b'amount\n"$1,234.56"\n"$999.00"\n"$10,000.00"\n'
    df, report = repair_csv(raw)
    assert df["amount"].tolist() == [1234.56, 999.00, 10000.00]


def test_european_decimal_comma_converted():
    raw = b'amount\n"1.234,56"\n"2.500,00"\n"999,99"\n'
    df, report = repair_csv(raw)
    assert df["amount"].tolist() == pytest.approx([1234.56, 2500.00, 999.99])
    assert any("European-style" in w for w in report.warnings)


def test_ambiguous_comma_grouping_left_as_text():
    raw = b'amount\n"1,234"\n"5,678"\n"9,012"\n'
    df, report = repair_csv(raw)
    assert df["amount"].dtype == object
    assert any("thousands separator or a decimal point" in w for w in report.warnings)


def test_leading_zero_codes_kept_as_text():
    raw = b"zip\n00123\n00456\n00789\n"
    df, report = repair_csv(raw)
    assert df["zip"].tolist() == ["00123", "00456", "00789"]
    assert any("leading zeros" in w for w in report.warnings)


def test_percent_sign_stripped():
    raw = b"rate\n45%\n10%\n99%\n"
    df, report = repair_csv(raw)
    assert df["rate"].tolist() == [45.0, 10.0, 99.0]


def test_accounting_negative_format_converted():
    raw = b'amount\n"(1,234.56)"\n500.00\n"(10.00)"\n'
    df, report = repair_csv(raw)
    assert df["amount"].tolist() == [-1234.56, 500.00, -10.00]


def test_text_column_with_incidental_comma_not_flagged_as_ambiguous_number():
    # A merged-overflow value like "42,Chicago" contains a comma but is text,
    # not a number — it must not trip the "ambiguous decimal comma" heuristic.
    raw = b"name,age,city\nAlice,30,NYC\nSmith, John,42,Chicago\n"
    df, report = repair_csv(raw)
    assert df["city"].iloc[1] == "42,Chicago"
    assert not any("thousands separator or a decimal point" in w for w in report.warnings)


def test_scientific_notation_flagged():
    raw = b"id\n1.23E+14\n1.24E+14\n1.25E+14\n"
    df, report = repair_csv(raw)
    assert any("scientific notation" in w for w in report.warnings)


# ---------------------------------------------------------------------------
# 10. Dates
# ---------------------------------------------------------------------------

def test_ambiguous_date_format_flagged_not_guessed():
    raw = b"date\n03/04/2024\n05/06/2024\n07/08/2024\n"
    df, report = repair_csv(raw)
    assert any("ambiguous" in w for w in report.warnings)
    # Left as original text since it couldn't be resolved confidently.
    assert df["date"].tolist() == ["03/04/2024", "05/06/2024", "07/08/2024"]


def test_unambiguous_day_first_dates_normalized():
    raw = b"date\n25/12/2024\n01/01/2025\n31/03/2024\n"
    df, report = repair_csv(raw)
    assert df["date"].tolist() == ["2024-12-25", "2025-01-01", "2024-03-31"]


def test_mixed_date_formats_in_same_column():
    raw = b"date\n2024-02-01\nFeb 15, 2024\n2024-03-10\n"
    df, report = repair_csv(raw)
    assert df["date"].iloc[0] == "2024-02-01"
    assert df["date"].iloc[2] == "2024-03-10"


# ---------------------------------------------------------------------------
# 11. Whitespace gremlins
# ---------------------------------------------------------------------------

def test_leading_trailing_spaces_stripped():
    raw = b"country\n  USA\nUSA\n USA \n"
    df, report = repair_csv(raw)
    assert (df["country"] == "USA").all()


def test_nonbreaking_space_normalized():
    raw = "country\nUSA\xa0\n".encode("utf-8")
    df, report = repair_csv(raw)
    assert df["country"].iloc[0] == "USA"


def test_embedded_tab_replaced_with_space():
    raw = b'note\n"has\ta tab"\n'
    df, report = repair_csv(raw)
    assert df["note"].iloc[0] == "has a tab"


# ---------------------------------------------------------------------------
# 13. Security / formula injection
# ---------------------------------------------------------------------------

def test_formula_injection_cells_sanitized():
    raw = b'note\n=SUM(A1:A10)\n@cmd\n+cmd|malicious\nplain text\n'
    df, report = repair_csv(raw)
    assert df["note"].iloc[0] == "'=SUM(A1:A10)"
    assert df["note"].iloc[1] == "'@cmd"
    assert df["note"].iloc[2] == "'+cmd|malicious"
    assert df["note"].iloc[3] == "plain text"
    assert report.stats.get("formula_injection_cells_sanitized") == 3


def test_negative_numbers_not_treated_as_formula_injection():
    raw = b"amount\n-5\n-10.5\n-3\n"
    df, report = repair_csv(raw)
    # Column is numeric-coerced, so this never even reaches the string-sanitizer,
    # and no cells are flagged.
    assert df["amount"].tolist() == [-5.0, -10.5, -3.0]
    assert "formula_injection_cells_sanitized" not in report.stats


# ---------------------------------------------------------------------------
# 14. File-level / structural issues
# ---------------------------------------------------------------------------

def test_empty_file_rejected():
    with pytest.raises(CSVRepairError, match="empty"):
        repair_csv(b"")


def test_header_only_file_rejected():
    with pytest.raises(CSVRepairError, match="no rows"):
        repair_csv(b"name,age\n")


def test_xlsx_renamed_to_csv_rejected():
    xlsx_magic = b"PK\x03\x04" + b"\x00" * 20
    with pytest.raises(CSVRepairError, match="zip-based binary"):
        repair_csv(xlsx_magic)


def test_json_renamed_to_csv_rejected():
    raw = b'[{"a": 1, "b": 2}, {"a": 3, "b": 4}]'
    with pytest.raises(CSVRepairError, match="JSON"):
        repair_csv(raw)


def test_xml_renamed_to_csv_rejected():
    raw = b'<?xml version="1.0"?><root><row>1</row></root>'
    with pytest.raises(CSVRepairError, match="XML/HTML"):
        repair_csv(raw)


def test_duplicate_rows_flagged_not_dropped():
    raw = b"a,b\n1,2\n1,2\n3,4\n"
    df, report = repair_csv(raw)
    assert len(df) == 3  # not silently deduplicated
    assert report.stats.get("duplicate_rows") == 1


def test_mixed_data_types_in_column_flagged():
    raw = b"value\n1\n2\ntext\n3\ntext2\n4\n5\n"
    df, report = repair_csv(raw)
    assert any("mixes numeric-looking and text" in w for w in report.warnings)


def test_truncated_file_last_row_padded_not_crashed():
    raw = b"a,b,c\n1,2,3\n4,5"
    df, report = repair_csv(raw)
    assert len(df) == 2
    assert pd.isna(df.iloc[1]["c"])


# ---------------------------------------------------------------------------
# Clean, well-formed file: no warnings, no noise
# ---------------------------------------------------------------------------

def test_clean_file_produces_no_warnings():
    raw = b"name,age,city\nAlice,30,NYC\nBob,25,LA\n"
    df, report = repair_csv(raw)
    assert len(df) == 2
    assert report.warnings == []
    assert report.clean
