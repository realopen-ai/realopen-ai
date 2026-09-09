"""
Tests for the Excel generation service (services/excel_gen.py) and the
use_excel_gen agent tool.

Layers:
1. Cell/range helpers — refs, bounds, range parsing, color + number
   format safety.
2. Spec validation — every error class (structure, names, refs, rows,
   charts, overlaps, limits) + warning classes.
3. JSON extraction from LLM output — fences, prose, wrappers, trailing
   commas.
4. Normalization — filename, row padding, formula '=' prefix, header
   name → column letter number formats, value coercion, defaults.
5. Converter e2e — multi-sheet, text blocks, live formulas, fill_down
   (Translator shifts + numeric sequences + exclude columns), total row
   placeholders, number formats, freeze panes / widths / tab colors /
   auto-filter / merges, all chart types, notes, openpyxl round-trip,
   LibreOffice round-trip (gated on soffice).
6. LLM flow — mocked _call_llm: first-try success, repair round,
   double failure, wrapper unwrapping.
7. Public API contract — generate_spreadsheet returns the exact
   deliverable shape used by the tool/frontend.
8. Agent tool wrapper — registry, parameters, execute success/error,
   param aliases; agent service keyword selection + fenced parsing.
"""

import re
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services import excel_gen  # noqa: E402
from app import prompts as app_prompts  # noqa: E402

# Importing the tools package registers every tool (including
# ExcelGenTool) in the global registry at import time.
import app.agent.tools  # noqa: E402,F401

# ─────────────────────────────────────────────────────────────────────
# 1. Cell / range helpers
# ─────────────────────────────────────────────────────────────────────


class TestCellHelpers:
    def test_cell_to_indices_basic(self):
        assert excel_gen.cell_to_indices("A1") == (1, 1)
        assert excel_gen.cell_to_indices("B12") == (12, 2)
        assert excel_gen.cell_to_indices("AA5") == (5, 27)
        assert excel_gen.cell_to_indices(" b3 ") == (3, 2)

    def test_cell_to_indices_invalid(self):
        for bad in ("", "A", "1", "A0", "1234", "A12345678901234", None, 5, "XFD!"):
            with pytest.raises(ValueError):
                excel_gen.cell_to_indices(bad)

    def test_cell_bounds(self):
        # max col XFD = 16384
        assert excel_gen.cell_to_indices("XFD1") == (1, 16384)
        with pytest.raises(ValueError):
            excel_gen.cell_to_indices("XFE1")  # one past XFD
        with pytest.raises(ValueError):
            excel_gen.cell_to_indices("A1048577")  # one past max row

    def test_is_valid_cell(self):
        assert excel_gen.is_valid_cell("A1") is True
        assert excel_gen.is_valid_cell("ZZ999") is True
        assert excel_gen.is_valid_cell("nope") is False
        assert excel_gen.is_valid_cell(None) is False
        assert excel_gen.is_valid_cell(42) is False

    def test_parse_range_no_sheet(self):
        sheet, r1, r2, c1, c2 = excel_gen.parse_range("B2:B13", "S")
        assert (sheet, r1, r2, c1, c2) == ("S", 2, 13, 2, 2)

    def test_parse_range_unquoted_sheet(self):
        sheet, r1, r2, c1, c2 = excel_gen.parse_range("Sheet1!A2:A13", "S")
        assert (sheet, r1, r2, c1, c2) == ("Sheet1", 2, 13, 1, 1)

    def test_parse_range_quoted_sheet_with_spaces(self):
        sheet, *_ = excel_gen.parse_range("'My Sheet'!A1:B3", "S")
        assert sheet == "My Sheet"

    def test_parse_range_dollar_anchors(self):
        sheet, r1, r2, c1, c2 = excel_gen.parse_range("$B$2:$B$13", "S")
        assert (sheet, r1, r2, c1, c2) == ("S", 2, 13, 2, 2)

    def test_parse_range_single_cell(self):
        sheet, r1, r2, c1, c2 = excel_gen.parse_range("Inputs!B4", "S")
        assert (sheet, r1, r2, c1, c2) == ("Inputs", 4, 4, 2, 2)

    def test_parse_range_reversed_normalized(self):
        _, r1, r2, c1, c2 = excel_gen.parse_range("C5:A2", "S")
        assert (r1, r2, c1, c2) == (2, 5, 1, 3)

    def test_parse_range_invalid(self):
        for bad in ("", "!", "A1:", "Sheet!!A1", "B2:B", None):
            with pytest.raises(ValueError):
                excel_gen.parse_range(bad, "S")

    def test_valid_hex_color(self):
        assert excel_gen.valid_hex_color("16304F") is True
        assert excel_gen.valid_hex_color("abc") is False
        assert excel_gen.valid_hex_color("12345Z") is False
        assert excel_gen.valid_hex_color(None) is False
        assert excel_gen.valid_hex_color("1234567") is False

    def test_valid_number_format(self):
        assert excel_gen.valid_number_format("#,##0.00") is True
        assert excel_gen.valid_number_format("0.0%") is True
        assert excel_gen.valid_number_format("yyyy-mm-dd") is True
        assert excel_gen.valid_number_format('"€"#,##0.00') is True
        assert excel_gen.valid_number_format("") is False
        assert excel_gen.valid_number_format("x" * 100) is False
        assert excel_gen.valid_number_format("bad\nformat") is False
        assert excel_gen.valid_number_format(None) is False


# ─────────────────────────────────────────────────────────────────────
# 2. Spec validation
# ─────────────────────────────────────────────────────────────────────

VALID_SPEC = {
    "filename": "demo.xlsx",
    "sheets": [
        {
            "name": "Data",
            "tables": [
                {
                    "start_cell": "A1",
                    "headers": ["Item", "Qty", "Price"],
                    "rows": [
                        ["Widget", 2, 9.99],
                        ["Gadget", 5, 19.5],
                    ],
                    "number_formats": {"C": "#,##0.00"},
                    "total_row": ["Total", "=SUM(B2:B3)", "=SUM(C2:C3)"],
                }
            ],
            "formulas": [{"cell": "E2", "formula": "=B2*C2"}],
            "notes": "demo data",
        }
    ],
}


class TestValidation:
    def test_valid_spec_no_errors(self):
        errors, _ = excel_gen.validate_workbook_spec(VALID_SPEC)
        assert errors == []

    def test_root_not_dict(self):
        errors, _ = excel_gen.validate_workbook_spec([1, 2])
        assert errors == ["root must be a JSON object"]

    def test_sheets_missing_or_empty(self):
        for spec in ({}, {"sheets": []}, {"sheets": "no"}, {"sheets": None}):
            errors, _ = excel_gen.validate_workbook_spec(spec)
            assert len(errors) == 1
            assert "sheets" in errors[0]

    def test_filename_not_string(self):
        spec = {"filename": 12, "sheets": VALID_SPEC["sheets"]}
        errors, _ = excel_gen.validate_workbook_spec(spec)
        assert any("filename" in e for e in errors)

    def test_sheet_name_rules(self):
        def spec_with_name(name):
            return {
                "sheets": [
                    {"name": name, "tables": [{"headers": ["A"], "rows": [[1]]}]}
                ]
            }

        for bad in ("x" * 32, "Bad[Name", "a:b", "a*b", "a?b", ""):
            errors, _ = excel_gen.validate_workbook_spec(spec_with_name(bad))
            assert errors, f"name {bad!r} should fail"

    def test_duplicate_sheet_names(self):
        spec = {
            "sheets": [
                {"name": "Data", "tables": [{"headers": ["A"], "rows": [[1]]}]},
                {"name": "DATA", "tables": [{"headers": ["A"], "rows": [[1]]}]},
            ]
        }
        errors, _ = excel_gen.validate_workbook_spec(spec)
        assert any("duplicate" in e for e in errors)

    def test_too_many_sheets(self):
        spec = {
            "sheets": [
                {"name": f"S{i}", "tables": [{"headers": ["A"], "rows": [[1]]}]}
                for i in range(excel_gen.MAX_SHEETS + 1)
            ]
        }
        errors, _ = excel_gen.validate_workbook_spec(spec)
        assert any("too many sheets" in e for e in errors)

    def test_table_headers_rules(self):
        spec = {"sheets": [{"name": "S", "tables": [{"rows": [[1]]}]}]}
        errors, _ = excel_gen.validate_workbook_spec(spec)
        assert any("headers" in e for e in errors)

        spec = {"sheets": [{"name": "S", "tables": [{"headers": [], "rows": []}]}]}
        errors, _ = excel_gen.validate_workbook_spec(spec)
        assert any("headers" in e for e in errors)

    def test_rows_must_be_list(self):
        spec = {
            "sheets": [{"name": "S", "tables": [{"headers": ["A"], "rows": "nope"}]}]
        }
        errors, _ = excel_gen.validate_workbook_spec(spec)
        assert any("'rows' must be an array" in e for e in errors)

    def test_row_values_scalar_only(self):
        spec = {
            "sheets": [
                {"name": "S", "tables": [{"headers": ["A"], "rows": [[{"nested": 1}]]}]}
            ]
        }
        errors, _ = excel_gen.validate_workbook_spec(spec)
        assert any("must be a string/number/boolean/null" in e for e in errors)

    def test_invalid_start_cell(self):
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "tables": [
                        {"start_cell": "not-a-cell", "headers": ["A"], "rows": [[1]]}
                    ],
                }
            ]
        }
        errors, _ = excel_gen.validate_workbook_spec(spec)
        assert any("start_cell" in e for e in errors)

    def test_invalid_freeze_panes(self):
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "freeze_panes": "nope",
                    "tables": [{"headers": ["A"], "rows": [[1]]}],
                }
            ]
        }
        errors, _ = excel_gen.validate_workbook_spec(spec)
        assert any("freeze_panes" in e for e in errors)

    def test_freeze_a1_is_warning_not_error(self):
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "freeze_panes": "A1",
                    "tables": [{"headers": ["A"], "rows": [[1]]}],
                }
            ]
        }
        errors, warnings = excel_gen.validate_workbook_spec(spec)
        assert errors == []
        assert any("freezes nothing" in w for w in warnings)

    def test_invalid_formula_cell(self):
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "tables": [{"headers": ["A"], "rows": [[1]]}],
                    "formulas": [{"cell": "bad", "formula": "=SUM(A1:A2)"}],
                }
            ]
        }
        errors, _ = excel_gen.validate_workbook_spec(spec)
        assert any("formulas" in e for e in errors)

    def test_formula_missing_eq_is_warning(self):
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "tables": [{"headers": ["A"], "rows": [[1]]}],
                    "formulas": [{"cell": "B2", "formula": "SUM(A1:A2)"}],
                }
            ]
        }
        errors, warnings = excel_gen.validate_workbook_spec(spec)
        assert errors == []
        assert any("missing '='" in w for w in warnings)

    def test_formulas_map_form_accepted(self):
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "tables": [{"headers": ["A"], "rows": [[1]]}],
                    "formulas": {"B2": "=SUM(A1:A1)"},
                }
            ]
        }
        errors, _ = excel_gen.validate_workbook_spec(spec)
        assert errors == []

    def test_chart_bad_type(self):
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "tables": [{"headers": ["A", "B"], "rows": [[1, 2]]}],
                    "charts": [
                        {"type": "radar", "series": [{"values_range": "S!B2:B2"}]}
                    ],
                }
            ]
        }
        errors, _ = excel_gen.validate_workbook_spec(spec)
        assert any("type must be one of" in e for e in errors)

    def test_chart_empty_series(self):
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "tables": [{"headers": ["A", "B"], "rows": [[1, 2]]}],
                    "charts": [{"type": "bar", "series": []}],
                }
            ]
        }
        errors, _ = excel_gen.validate_workbook_spec(spec)
        assert any("'series' must be a non-empty array" in e for e in errors)

    def test_chart_bad_range(self):
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "tables": [{"headers": ["A", "B"], "rows": [[1, 2]]}],
                    "charts": [
                        {"type": "bar", "series": [{"values_range": "!!garbage!!"}]}
                    ],
                }
            ]
        }
        errors, _ = excel_gen.validate_workbook_spec(spec)
        assert any("values_range" in e for e in errors)

    def test_chart_placeholder_ranges_accepted(self):
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "tables": [{"headers": ["A", "B"], "rows": [[1, 2]]}],
                    "charts": [
                        {
                            "type": "line",
                            "categories_range": "S!A2:A{last_row}",
                            "series": [{"values_range": "S!B2:B{last_row}"}],
                        }
                    ],
                }
            ]
        }
        errors, _ = excel_gen.validate_workbook_spec(spec)
        assert errors == []

    def test_table_overlap_is_error(self):
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "tables": [
                        {"start_cell": "A1", "headers": ["A", "B"], "rows": [[1, 2]]},
                        {"start_cell": "B2", "headers": ["C"], "rows": [[3]]},
                    ],
                }
            ]
        }
        errors, _ = excel_gen.validate_workbook_spec(spec)
        assert any("overlap" in e for e in errors)

    def test_disjoint_tables_ok(self):
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "tables": [
                        {"start_cell": "A1", "headers": ["A", "B"], "rows": [[1, 2]]},
                        {"start_cell": "D1", "headers": ["C"], "rows": [[3]]},
                    ],
                }
            ]
        }
        errors, _ = excel_gen.validate_workbook_spec(spec)
        assert errors == []

    def test_empty_sheet_is_warning(self):
        spec = {"sheets": [{"name": "S"}]}
        errors, warnings = excel_gen.validate_workbook_spec(spec)
        assert errors == []
        assert any("no content" in w for w in warnings)

    def test_invalid_tab_color_warning(self):
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "tab_color": "zzz",
                    "tables": [{"headers": ["A"], "rows": [[1]]}],
                }
            ]
        }
        errors, warnings = excel_gen.validate_workbook_spec(spec)
        assert errors == []
        assert any("tab_color" in w for w in warnings)

    def test_text_block_text_scalars(self):
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "text_blocks": [
                        {"cell": "A1", "text": "hello"},
                        {"cell": "B1", "text": 250000},
                        {"cell": "C1", "text": {"bad": "dict"}},
                    ],
                }
            ]
        }
        errors, _ = excel_gen.validate_workbook_spec(spec)
        assert any("'text' must be" in e for e in errors)

    def test_text_block_number_format(self):
        # money-valued labels outside tables (e.g. the amortization
        # Monthly Payment input at B5) can carry an Excel number format
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "text_blocks": [
                        {
                            "cell": "B5",
                            "text": "=-PMT(B3/12,B4,B2)",
                            "number_format": '"$"#,##0.00',
                        },
                        {"cell": "A5", "text": "Monthly Payment"},
                        {
                            "cell": "B6",
                            "text": 1,
                            "number_format": "bad\nformat",
                        },
                    ],
                }
            ]
        }
        errors, warnings = excel_gen.validate_workbook_spec(spec)
        assert errors == []
        assert any("number_format" in w for w in warnings)
        norm = excel_gen._normalize_spec(spec)
        blocks = {b["cell"]: b for b in norm["sheets"][0]["text_blocks"]}
        assert blocks["B5"]["number_format"] == '"$"#,##0.00'
        assert blocks["A5"]["number_format"] is None
        assert blocks["B6"]["number_format"] is None  # invalid → dropped

    def test_invalid_number_format_dropped_in_normalize(self):
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "tables": [
                        {
                            "headers": ["A"],
                            "rows": [[1]],
                            "number_formats": {"A": "bad\nformat"},
                        }
                    ],
                }
            ]
        }
        errors, _ = excel_gen.validate_workbook_spec(spec)
        assert errors == []  # format issues handled in normalize, not errors
        norm = excel_gen._normalize_spec(spec)
        assert norm["sheets"][0]["tables"][0]["number_formats"] == {}


# ─────────────────────────────────────────────────────────────────────
# 3. JSON extraction from LLM output
# ─────────────────────────────────────────────────────────────────────


class TestJsonExtraction:
    def test_plain_json(self):
        obj = excel_gen._extract_json_object('{"sheets": []}')
        assert obj == {"sheets": []}

    def test_fenced_json(self):
        obj = excel_gen._extract_json_object('```json\n{"sheets": []}\n```')
        assert obj == {"sheets": []}

    def test_prose_around_json(self):
        text = 'Here is the spec:\n{"sheets": []}\nHope this helps!'
        obj = excel_gen._extract_json_object(text)
        assert obj == {"sheets": []}

    def test_workbook_wrapper_unwrapped(self):
        inner = {"sheets": [{"name": "S"}]}
        obj = excel_gen._extract_json_object(json_dumps({"workbook": inner}))
        assert obj == inner

    def test_wrapper_without_sheets_kept(self):
        obj = excel_gen._extract_json_object('{"foo": {"bar": 1}}')
        assert obj == {"foo": {"bar": 1}}

    def test_trailing_comma_fixed(self):
        obj = excel_gen._extract_json_object('{"sheets": [{"name": "S"},],}')
        assert obj["sheets"][0]["name"] == "S"

    def test_no_object_raises(self):
        with pytest.raises(ValueError):
            excel_gen._extract_json_object("no json here")
        with pytest.raises(ValueError):
            excel_gen._extract_json_object("")

    def test_nested_objects_preserved(self):
        spec = json_dumps(VALID_SPEC)
        obj = excel_gen._extract_json_object(f"prefix {spec} suffix")
        assert obj == VALID_SPEC

    def test_missing_array_closer_repaired(self):
        """Reproduces the exact small-LLM failure from the project logs.

        The LLM emitted `"auto_filter":true}}` — closed the table object
        and the sheet object, but forgot to close the `tables` array
        with `]` in between. The repair walker must insert the missing
        `]` so the spec parses.
        """
        broken = (
            '{"filename":"shopping_list.xlsx","sheets":['
            '{"name":"Shopping List","tables":['
            '{"start_cell":"A2","headers":["Item","Qty","Price"],'
            '"rows":[["Milk",2,3.50]],"total_row":["Total","", "=SUM(C3:C10)"],'
            '"auto_filter":true}},'
            '{"name":"Notes","text_blocks":[{"cell":"A1","text":"hi"}]}'
            "]}"
        )
        # Without repair, this JSON is unparseable.
        import json as _json

        with pytest.raises(_json.JSONDecodeError):
            _json.loads(broken)
        # With repair, the spec parses and the structure is recovered.
        obj = excel_gen._extract_json_object(broken)
        assert obj["filename"] == "shopping_list.xlsx"
        assert obj["sheets"][0]["name"] == "Shopping List"
        assert obj["sheets"][0]["tables"][0]["headers"] == ["Item", "Qty", "Price"]
        assert obj["sheets"][1]["name"] == "Notes"

    def test_missing_object_closer_repaired(self):
        """Symmetric case: LLM closed an outer array but not the inner object."""
        broken = '{"sheets":[{"name":"S","tables":[{"headers":["A"],"rows":[[1]]]}'
        obj = excel_gen._extract_json_object(broken)
        assert obj["sheets"][0]["name"] == "S"

    def test_trailing_comma_in_nested_array(self):
        """Trailing comma before a closer — already handled by the original code."""
        obj = excel_gen._extract_json_object(
            '{"sheets":[{"name":"S","tables":[1,2,]}]}'
        )
        assert obj["sheets"][0]["tables"] == [1, 2]

    def test_missing_comma_between_elements_repaired(self):
        """The LLM forgot a comma between two array elements."""
        broken = '{"sheets":[{"name":"S","tables":[{"headers":["A"],"rows":[[1]]}]}{"extra":1}'
        # The `{` directly after `]` triggers "Extra data" — repair truncates.
        obj = excel_gen._extract_json_object(broken)
        assert obj["sheets"][0]["name"] == "S"

    def test_garbage_input_still_raises(self):
        """Inputs with no JSON object start (`{`) must still raise ValueError.

        The repair function is aggressive at salvaging broken JSON, but
        it cannot invent structure where there is none. Inputs that
        have no `{` at all (or only `{` followed by content the parser
        cannot consume at all) must still surface a ValueError so the
        LLM repair round can be triggered.
        """
        with pytest.raises(ValueError):
            excel_gen._extract_json_object("no json here at all")
        with pytest.raises(ValueError):
            excel_gen._extract_json_object("")
        with pytest.raises(ValueError):
            excel_gen._extract_json_object("```json\n```\n")  # empty fence
        # A non-empty empty object is salvageable — that's by design.
        # The validator catches the empty spec downstream.
        obj = excel_gen._extract_json_object("not even close to json {{{ }[")
        assert isinstance(obj, dict)


def json_dumps(obj) -> str:
    import json

    return json.dumps(obj)


# ─────────────────────────────────────────────────────────────────────
# 4. Normalization
# ─────────────────────────────────────────────────────────────────────


class TestNormalization:
    def test_filename_sanitized(self):
        spec = {"filename": "my report (final).xlsx", "sheets": VALID_SPEC["sheets"]}
        norm = excel_gen._normalize_spec(spec)
        assert norm["filename"] == "my_report_final.xlsx"

    def test_filename_extension_added(self):
        spec = {"filename": "budget", "sheets": VALID_SPEC["sheets"]}
        norm = excel_gen._normalize_spec(spec)
        assert norm["filename"] == "budget.xlsx"

    def test_filename_path_chars_stripped(self):
        spec = {"filename": "../../etc/passwd.xlsx", "sheets": VALID_SPEC["sheets"]}
        norm = excel_gen._normalize_spec(spec)
        assert "/" not in norm["filename"]
        assert norm["filename"].endswith(".xlsx")

    def test_row_padding_and_truncation(self):
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "tables": [
                        {
                            "headers": ["A", "B", "C"],
                            "rows": [
                                [1],  # padded to 3
                                [1, 2, 3, 4, 5],  # truncated to 3
                            ],
                        }
                    ],
                }
            ]
        }
        norm = excel_gen._normalize_spec(spec)
        rows = norm["sheets"][0]["tables"][0]["rows"]
        assert rows[0] == [1, None, None]
        assert rows[1] == [1, 2, 3]

    def test_value_coercion(self):
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "tables": [
                        {
                            "headers": ["A", "B", "C", "D"],
                            "rows": [
                                ["42", "3.14", "007", "2025-06-01"],
                            ],
                        }
                    ],
                }
            ]
        }
        norm = excel_gen._normalize_spec(spec)
        row = norm["sheets"][0]["tables"][0]["rows"][0]
        assert row[0] == 42 and isinstance(row[0], int)
        assert row[1] == 3.14 and isinstance(row[1], float)
        assert row[2] == "007"  # leading zero stays text (zip codes etc.)
        assert isinstance(row[3], __import__("datetime").date)

    def test_european_decimal_comma_coerced(self):
        """Small LLMs often emit European decimal commas ("1,9" instead of "1.9").

        The coercion is conservative — only 1-2 digits after the comma
        qualify, so "1,234" (which could legitimately mean 1234 in
        English) stays a string.
        """
        spec = {
            "sheets": [
                {
                    "name": "Shopping",
                    "tables": [
                        {
                            "headers": ["Item", "Qty", "Price"],
                            "rows": [
                                ["Milk", "1,9", "3,50"],
                                ["Bread", "3", "2,75"],
                                ["Cheese", "0,25", "3,75"],
                            ],
                        }
                    ],
                }
            ]
        }
        norm = excel_gen._normalize_spec(spec)
        rows = norm["sheets"][0]["tables"][0]["rows"]
        assert rows[0] == ["Milk", 1.9, 3.5]
        assert rows[1] == ["Bread", 3, 2.75]
        assert rows[2] == ["Cheese", 0.25, 3.75]
        # Anglos thousands separator stays text (cannot be auto-coerced safely).
        spec_anglo = {
            "sheets": [
                {
                    "name": "S",
                    "tables": [
                        {
                            "headers": ["Population"],
                            "rows": [["1,234"]],
                        }
                    ],
                }
            ]
        }
        norm_anglo = excel_gen._normalize_spec(spec_anglo)
        assert norm_anglo["sheets"][0]["tables"][0]["rows"][0][0] == "1,234"
        # Strings with units / extra characters are NOT touched.
        spec_units = {
            "sheets": [
                {
                    "name": "S",
                    "tables": [
                        {
                            "headers": ["Qty"],
                            "rows": [["1,9 kg"]],
                        }
                    ],
                }
            ]
        }
        norm_units = excel_gen._normalize_spec(spec_units)
        assert norm_units["sheets"][0]["tables"][0]["rows"][0][0] == "1,9 kg"

    def test_total_row_true_auto_generates_sum(self):
        """Shortcut `total_row: true` → auto-generate =SUM for numeric columns."""
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "tables": [
                        {
                            "headers": ["Item", "Qty", "Price"],
                            "rows": [["Milk", 2, 3.5], ["Bread", 1, 2.75]],
                            "total_row": True,
                        }
                    ],
                }
            ]
        }
        processed = excel_gen._post_process_spec(spec)
        tr = processed["sheets"][0]["tables"][0]["total_row"]
        assert tr[0] == "Total"
        assert tr[1] == "=SUM(B{first_row}:B{last_row})"
        assert tr[2] == "=SUM(C{first_row}:C{last_row})"

    def test_total_row_sum_columns_shortcut(self):
        """Shortcut `total_row: {"sum_columns": ["C"]}` only sums specific columns."""
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "tables": [
                        {
                            "headers": ["Item", "Qty", "Price"],
                            "rows": [["Milk", 2, 3.5], ["Bread", 1, 2.75]],
                            "total_row": {"sum_columns": ["C"]},
                        }
                    ],
                }
            ]
        }
        processed = excel_gen._post_process_spec(spec)
        tr = processed["sheets"][0]["tables"][0]["total_row"]
        assert tr[0] == "Total"
        assert tr[1] is None  # column B not in sum_columns
        assert tr[2] == "=SUM(C{first_row}:C{last_row})"

    def test_total_row_sum_columns_by_header_name(self):
        """sum_columns accepts header names (case-insensitive), not just letters."""
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "tables": [
                        {
                            "headers": ["Item", "Quantity", "Price"],
                            "rows": [["Milk", 2, 3.5]],
                            "total_row": {"sum_columns": ["Price"]},
                        }
                    ],
                }
            ]
        }
        processed = excel_gen._post_process_spec(spec)
        tr = processed["sheets"][0]["tables"][0]["total_row"]
        assert tr[2] == "=SUM(C{first_row}:C{last_row})"

    def test_total_row_auto_fills_empty_numeric_cells(self):
        """A list `total_row` with empty cells in numeric columns is auto-filled.

        The LLM often leaves the quantity column blank when emitting a
        "Total" row. Post-processing injects =SUM(...) so the total is
        computed live instead of being blank.
        """
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "tables": [
                        {
                            "headers": ["Item", "Qty", "Price"],
                            "rows": [["Milk", 2, 3.5], ["Bread", 1, 2.75]],
                            "total_row": ["Total", "", "=SUM(C2:C3)"],
                        }
                    ],
                }
            ]
        }
        processed = excel_gen._post_process_spec(spec)
        tr = processed["sheets"][0]["tables"][0]["total_row"]
        # Column B was empty — auto-filled with SUM formula.
        assert tr[1] == "=SUM(B{first_row}:B{last_row})"
        # Column C already had a formula — preserved untouched.
        assert tr[2] == "=SUM(C2:C3)"
        # Column A's label is preserved.
        assert tr[0] == "Total"

    def test_total_row_non_empty_values_preserved(self):
        """Pre-computed numbers and labels in total_row are NOT auto-replaced.

        The LLM may legitimately have a "Target" / "Budget" / "Average"
        value in a numeric column — replacing it with SUM would be wrong.
        Only empty cells are filled.
        """
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "tables": [
                        {
                            "headers": ["Item", "Qty"],
                            "rows": [["Milk", 2], ["Bread", 1]],
                            "total_row": ["Total", 100],  # target value, not a sum
                        }
                    ],
                }
            ]
        }
        processed = excel_gen._post_process_spec(spec)
        tr = processed["sheets"][0]["tables"][0]["total_row"]
        assert tr == ["Total", 100]  # untouched

    def test_freeze_header_shortcut(self):
        """Shortcut `freeze_header: true` sets freeze_panes from the first table.

        If the table starts at A1 with no title, freeze_panes = "A2"
        (header row 1 frozen, data starts at row 2).
        """
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "freeze_header": True,
                    "tables": [
                        {
                            "start_cell": "A1",
                            "headers": ["A", "B"],
                            "rows": [[1, 2]],
                        }
                    ],
                }
            ]
        }
        processed = excel_gen._post_process_spec(spec)
        assert processed["sheets"][0]["freeze_panes"] == "A2"

    def test_freeze_header_with_title_offset(self):
        """`freeze_header: true` + a table title → freeze below the title + header."""
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "freeze_header": True,
                    "tables": [
                        {
                            "start_cell": "A3",
                            "title": "My Table",
                            "headers": ["A", "B"],
                            "rows": [[1, 2]],
                        }
                    ],
                }
            ]
        }
        processed = excel_gen._post_process_spec(spec)
        # Title at row 3, header at row 4, data at row 5 — freeze A5.
        assert processed["sheets"][0]["freeze_panes"] == "A5"

    def test_freeze_header_with_existing_freeze_panes_preserved(self):
        """If `freeze_panes` is already set, `freeze_header` is ignored."""
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "freeze_header": True,
                    "freeze_panes": "A3",
                    "tables": [
                        {
                            "start_cell": "A1",
                            "headers": ["A"],
                            "rows": [[1]],
                        }
                    ],
                }
            ]
        }
        processed = excel_gen._post_process_spec(spec)
        assert processed["sheets"][0]["freeze_panes"] == "A3"  # preserved

    def test_post_process_spec_idempotent_on_valid_spec(self):
        """A valid spec without shortcuts passes through unchanged (structurally)."""
        spec_copy = json_dumps(VALID_SPEC)
        import json as _json

        spec = _json.loads(spec_copy)
        processed = excel_gen._post_process_spec(spec)
        # Same sheets, same name, same headers, same total_row (already a list).
        assert processed["sheets"][0]["name"] == VALID_SPEC["sheets"][0]["name"]
        assert processed["sheets"][0]["tables"][0]["headers"] == [
            "Item",
            "Qty",
            "Price",
        ]
        # total_row was a list with formulas — _post_process_spec may
        # auto-fill any empty numeric cells (none here). Structure preserved.
        assert processed["sheets"][0]["tables"][0]["total_row"][0] == "Total"

    def test_post_process_spec_handles_malformed_input(self):
        """Malformed input is returned unchanged so the validator can complain."""
        assert excel_gen._post_process_spec("not a dict") == "not a dict"
        assert excel_gen._post_process_spec({"no_sheets": 1}) == {"no_sheets": 1}
        assert excel_gen._post_process_spec({"sheets": "not a list"}) == {
            "sheets": "not a list"
        }

    def test_number_format_by_header_name(self):
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "tables": [
                        {
                            "headers": ["Item", "Price"],
                            "rows": [["x", 1]],
                            "number_formats": {"Price": "#,##0.00", "A": "0.0%"},
                        }
                    ],
                }
            ]
        }
        norm = excel_gen._normalize_spec(spec)
        nf = norm["sheets"][0]["tables"][0]["number_formats"]
        assert nf == {"B": "#,##0.00", "A": "0.0%"}

    def test_invalid_formula_prefix_added(self):
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "tables": [{"headers": ["A"], "rows": [[1]]}],
                    "formulas": {"B2": "SUM(A1:A1)"},
                }
            ]
        }
        norm = excel_gen._normalize_spec(spec)
        assert norm["sheets"][0]["formulas"] == [("B2", "=SUM(A1:A1)")]

    def test_zebra_and_autofilter_defaults(self):
        norm = excel_gen._normalize_spec(VALID_SPEC)
        t = norm["sheets"][0]["tables"][0]
        assert t["zebra"] is True
        assert t["auto_filter"] is False
        assert t["borders"] is True

    def test_fill_down_kept(self):
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "tables": [
                        {
                            "headers": ["A", "B"],
                            "rows": [[1, "=A1"], [2, "=A2"]],
                            "fill_down": {"rows": 10, "exclude_columns": ["B"]},
                        }
                    ],
                }
            ]
        }
        norm = excel_gen._normalize_spec(spec)
        fd = norm["sheets"][0]["tables"][0]["fill_down"]
        assert fd == {"rows": 10, "exclude_columns": ["B"]}

    def test_invalid_chart_series_dropped(self):
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "tables": [{"headers": ["A", "B"], "rows": [[1, 2]]}],
                    "charts": [
                        {
                            "type": "bar",
                            "series": [
                                {"values_range": "S!B2:B2"},
                                {"name": "bad", "values_range": 42},
                            ],
                        },
                        {"type": "radar", "series": [{"values_range": "S!B2:B2"}]},
                    ],
                }
            ]
        }
        norm = excel_gen._normalize_spec(spec)
        charts = norm["sheets"][0]["charts"]
        assert len(charts) == 1
        assert len(charts[0]["series"]) == 1


# ─────────────────────────────────────────────────────────────────────
# 5. Converter e2e
# ─────────────────────────────────────────────────────────────────────

AMORT_SPEC = {
    "filename": "loan.xlsx",
    "sheets": [
        {
            "name": "Inputs",
            "text_blocks": [
                {"cell": "B2", "text": "Loan Inputs", "bold": True, "font_size": 14},
                {"cell": "A4", "text": "Principal"},
                {"cell": "B4", "text": 250000},
            ],
        },
        {
            "name": "Schedule",
            "freeze_panes": "A3",
            "tables": [
                {
                    "start_cell": "A1",
                    "title": "Amortization",
                    "headers": ["Month", "Payment", "Interest", "Principal", "Balance"],
                    "rows": [
                        [
                            1,
                            "=PMT(Inputs!$B$4*0/12,360,0)",
                            "=ROUND(250000*0.045/12,2)",
                            "=B3-C3",
                            248720.94,
                        ],
                        [2, "=B3", "=ROUND(E3*0.045/12,2)", "=B4-C4", "=E3-D4"],
                    ],
                    "number_formats": {
                        "B": "#,##0.00",
                        "C": "#,##0.00",
                        "D": "#,##0.00",
                        "E": "#,##0.00",
                    },
                    "fill_down": {"rows": 4},
                    "total_row": [
                        "Total",
                        "=SUM(B3:B{last_row})",
                        "=SUM(C3:C{last_row})",
                        "",
                        "",
                    ],
                }
            ],
            "charts": [
                {
                    "type": "line",
                    "title": "Balance",
                    "anchor": "G2",
                    "categories_range": "Schedule!A3:A{last_row}",
                    "series": [
                        {"name": "Balance", "values_range": "Schedule!E3:E{last_row}"}
                    ],
                },
            ],
            "notes": "Payment uses PMT.",
        },
    ],
}


@pytest.fixture()
def amort_norm():
    errors, _ = excel_gen.validate_workbook_spec(AMORT_SPEC)
    assert errors == []
    return excel_gen._normalize_spec(AMORT_SPEC)


@pytest.fixture()
def amort_file(tmp_path, amort_norm):
    out = tmp_path / "amort.xlsx"
    excel_gen._build_xlsx(amort_norm, out)
    return out


class TestConverter:
    def test_file_created_and_roundtrips(self, amort_file):
        assert amort_file.exists() and amort_file.stat().st_size > 1000
        from openpyxl import load_workbook

        wb = load_workbook(str(amort_file))
        assert wb.sheetnames == ["Inputs", "Schedule"]
        wb.close()

    def test_print_setup_written_for_viewer(self, amort_file):
        """Every sheet must carry print defaults so the XLSX preview
        (LibreOffice workbook→PDF) paginates like a print preview:
        landscape A4, fit to one page WIDE (never slice columns), as many
        pages tall as needed, and repeated header rows via print_title_rows
        wherever freeze panes anchor below row 1."""
        from openpyxl import load_workbook

        wb = load_workbook(str(amort_file))
        try:
            for ws in wb.worksheets:
                pr = ws.sheet_properties.pageSetUpPr
                assert pr is not None and pr.fitToPage is True, ws.title
                assert ws.page_setup.orientation == "landscape", ws.title
                assert str(ws.page_setup.paperSize) == "9", ws.title  # A4
                assert int(ws.page_setup.fitToWidth) == 1, ws.title
                assert int(ws.page_setup.fitToHeight) == 0, ws.title

                freeze = ws.freeze_panes
                if freeze:
                    anchor_row = int(
                        re.match(r"^[A-Za-z]+(\d+)", str(freeze).split(":")[0]).group(1)
                    )
                    if anchor_row > 1:
                        expected = f"1:{anchor_row - 1}"
                        # openpyxl normalizes to absolute form ("$1:$2").
                        actual = str(ws.print_title_rows or "").replace("$", "")
                        assert actual == expected, (
                            f"{ws.title}: print_title_rows {ws.print_title_rows!r}"
                            f" != {expected!r}"
                        )
        finally:
            wb.close()

    def test_text_blocks_written(self, amort_file):
        from openpyxl import load_workbook

        wb = load_workbook(str(amort_file))
        ws = wb["Inputs"]
        assert ws["B2"].value == "Loan Inputs"
        assert ws["B2"].font.bold is True
        assert ws["A4"].value == "Principal"
        assert ws["B4"].value == 250000
        wb.close()

    def test_title_and_headers(self, amort_file):
        from openpyxl import load_workbook

        wb = load_workbook(str(amort_file))
        ws = wb["Schedule"]
        assert ws["A1"].value == "Amortization"
        assert [ws.cell(row=2, column=c).value for c in range(1, 6)] == [
            "Month",
            "Payment",
            "Interest",
            "Principal",
            "Balance",
        ]
        # header style
        assert ws["A2"].fill.start_color.rgb.endswith(excel_gen.NAVY)
        assert ws["A2"].font.bold is True
        wb.close()

    def test_live_formulas_preserved(self, amort_file):
        from openpyxl import load_workbook

        wb = load_workbook(str(amort_file))
        ws = wb["Schedule"]
        assert ws["B3"].data_type == "f"
        assert ws["B3"].value.startswith("=PMT")
        assert ws["E4"].value == "=E3-D4"
        wb.close()

    def test_fill_down_sequence_and_shift(self, amort_file):
        from openpyxl import load_workbook

        wb = load_workbook(str(amort_file))
        ws = wb["Schedule"]
        # months 1..6 (2 explicit + 4 filled), header row 2 → rows 3..8
        months = [ws.cell(row=r, column=1).value for r in range(3, 9)]
        assert months == [1, 2, 3, 4, 5, 6]
        # formula shifted exactly like Excel fill-down
        assert ws["B5"].value == "=B4"
        assert ws["C5"].value == "=ROUND(E4*0.045/12,2)"
        assert ws["E5"].value == "=E4-D5"
        wb.close()

    def test_total_row_placeholders_expanded(self, amort_file):
        from openpyxl import load_workbook

        wb = load_workbook(str(amort_file))
        ws = wb["Schedule"]
        # last data row = 8 → total at 9
        assert ws["A9"].value == "Total"
        assert ws["B9"].value == "=SUM(B3:B8)"
        assert ws["C9"].value == "=SUM(C3:C8)"
        assert ws["A9"].font.bold is True
        wb.close()

    def test_number_formats_applied(self, amort_file):
        from openpyxl import load_workbook

        wb = load_workbook(str(amort_file))
        ws = wb["Schedule"]
        assert ws["B3"].number_format == "#,##0.00"
        wb.close()

    def test_freeze_panes(self, amort_file):
        from openpyxl import load_workbook

        wb = load_workbook(str(amort_file))
        assert wb["Schedule"].freeze_panes == "A3"
        wb.close()

    def test_chart_with_placeholder_range(self, amort_file):
        from openpyxl import load_workbook

        wb = load_workbook(str(amort_file))
        ws = wb["Schedule"]
        assert len(ws._charts) == 1
        chart = ws._charts[0]
        assert chart.title is not None
        # series range was expanded to the real last data row (8);
        # openpyxl writes absolute refs
        ref = chart.series[0].val.numRef.f
        assert "$E$3:$E$8" in ref
        # categories
        cat = chart.series[0].cat.numRef.f if chart.series[0].cat.numRef else None
        if cat is None and chart.series[0].cat.strRef:
            cat = chart.series[0].cat.strRef.f
        assert cat is not None and "$A$3:$A$8" in cat
        wb.close()

    def test_notes_written(self, amort_file):
        from openpyxl import load_workbook

        wb = load_workbook(str(amort_file))
        ws = wb["Schedule"]
        found = any(
            isinstance(ws.cell(row=r, column=1).value, str)
            and ws.cell(row=r, column=1).value.startswith("Notes:")
            for r in range(9, 15)
        )
        assert found
        wb.close()

    def test_auto_filter(self, tmp_path):
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "tables": [
                        {
                            "headers": ["A", "B"],
                            "rows": [[1, 2], [3, 4]],
                            "auto_filter": True,
                        }
                    ],
                }
            ]
        }
        excel_gen._build_xlsx(excel_gen._normalize_spec(spec), tmp_path / "f.xlsx")
        from openpyxl import load_workbook

        wb = load_workbook(str(tmp_path / "f.xlsx"))
        assert wb["S"].auto_filter.ref == "A1:B3"
        wb.close()

    def test_merged_cells_and_tab_color(self, tmp_path):
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "tab_color": "C9A227",
                    "merged_cells": ["A1:B1"],
                    "text_blocks": [{"cell": "A1", "text": "Title"}],
                }
            ]
        }
        excel_gen._build_xlsx(excel_gen._normalize_spec(spec), tmp_path / "m.xlsx")
        from openpyxl import load_workbook

        wb = load_workbook(str(tmp_path / "m.xlsx"))
        ws = wb["S"]
        assert "A1:B1" in [str(r) for r in ws.merged_cells.ranges]
        assert ws.sheet_properties.tabColor.rgb.endswith("C9A227")
        wb.close()

    def test_column_widths_spec_override(self, tmp_path):
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "column_widths": {"A": 40},
                    "tables": [{"headers": ["A"], "rows": [["x"]]}],
                }
            ]
        }
        excel_gen._build_xlsx(excel_gen._normalize_spec(spec), tmp_path / "w.xlsx")
        from openpyxl import load_workbook

        wb = load_workbook(str(tmp_path / "w.xlsx"))
        assert abs(wb["S"].column_dimensions["A"].width - 40) < 0.01
        wb.close()

    def test_all_chart_types(self, tmp_path):
        from openpyxl import load_workbook

        specs = {
            "bar": "col",
            "bar_h": "bar",
            "line": None,
            "area": None,
            "pie": None,
            "scatter": None,
        }
        for ctype in specs:
            spec = {
                "sheets": [
                    {
                        "name": "Data",
                        "tables": [
                            {
                                "headers": ["Cat", "V1", "V2"],
                                "rows": [
                                    ["a", 10, 5],
                                    ["b", 20, 8],
                                    ["c", 15, 12],
                                ],
                            }
                        ],
                        "charts": [
                            {
                                "type": ctype,
                                "anchor": "F2",
                                "categories_range": "Data!A2:A4",
                                "series": [
                                    {"name": "S1", "values_range": "Data!B2:B4"},
                                    {"name": "S2", "values_range": "Data!C2:C4"},
                                ],
                            }
                        ],
                    }
                ]
            }
            out = tmp_path / f"{ctype}.xlsx"
            excel_gen._build_xlsx(excel_gen._normalize_spec(spec), out)
            wb = load_workbook(str(out))
            ws = wb["Data"]
            assert len(ws._charts) == 1, ctype
            if ctype in ("bar", "bar_h"):
                assert ws._charts[0].type == specs[ctype]
            if ctype == "pie":
                assert len(ws._charts[0].series) == 1  # pie uses first series
            else:
                assert len(ws._charts[0].series) == 2
            wb.close()

    def test_freeze_default_from_first_table(self, tmp_path):
        # no explicit freeze → freeze below first table's header
        spec = {
            "sheets": [
                {
                    "name": "S",
                    "tables": [
                        {
                            "start_cell": "A1",
                            "headers": ["A"],
                            "rows": [[1], [2]],
                        }
                    ],
                }
            ]
        }
        excel_gen._build_xlsx(excel_gen._normalize_spec(spec), tmp_path / "fd.xlsx")
        from openpyxl import load_workbook

        wb = load_workbook(str(tmp_path / "fd.xlsx"))
        assert wb["S"].freeze_panes == "A2"
        wb.close()

    def test_fill_down_exclude_columns(self, tmp_path):
        from openpyxl import load_workbook

        spec = {
            "sheets": [
                {
                    "name": "S",
                    "tables": [
                        {
                            "headers": ["A", "B"],
                            "rows": [[1, "=A1*2"], [2, "=A2*2"]],
                            "fill_down": {"rows": 2, "exclude_columns": ["B"]},
                        }
                    ],
                }
            ]
        }
        excel_gen._build_xlsx(excel_gen._normalize_spec(spec), tmp_path / "x.xlsx")
        wb = load_workbook(str(tmp_path / "x.xlsx"))
        ws = wb["S"]
        # 2 explicit + 2 filled rows → rows 2..5
        assert [ws.cell(row=r, column=1).value for r in range(2, 6)] == [1, 2, 3, 4]
        assert ws["B4"].value is None  # excluded column stays empty
        wb.close()

    def test_multiple_tables_disjoint(self, tmp_path):
        from openpyxl import load_workbook

        spec = {
            "sheets": [
                {
                    "name": "S",
                    "tables": [
                        {"start_cell": "A1", "headers": ["X"], "rows": [[1]]},
                        {"start_cell": "D1", "headers": ["Y"], "rows": [[2]]},
                    ],
                }
            ]
        }
        excel_gen._build_xlsx(excel_gen._normalize_spec(spec), tmp_path / "t.xlsx")
        wb = load_workbook(str(tmp_path / "t.xlsx"))
        ws = wb["S"]
        assert ws["A1"].value == "X" and ws["D1"].value == "Y"
        wb.close()

    def test_libreoffice_roundtrip(self, amort_file):
        """The generated file must open without a repair prompt."""
        import shutil
        import subprocess

        soffice = shutil.which("soffice") or "/usr/lib/libreoffice/program/soffice"
        if not Path(soffice).exists():
            pytest.skip("LibreOffice not available")
        out_dir = amort_file.parent / "lo"
        r = subprocess.run(
            [
                soffice,
                "--headless",
                "--norestore",
                "--convert-to",
                "xlsx",
                "--outdir",
                str(out_dir),
                str(amort_file),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert r.returncode == 0, r.stderr
        assert (out_dir / (amort_file.stem + ".xlsx")).exists()


# ─────────────────────────────────────────────────────────────────────
# 6. LLM flow (mocked)
# ─────────────────────────────────────────────────────────────────────


class TestLlmFlow:
    @pytest.mark.asyncio
    async def test_first_try_success(self, monkeypatch):
        calls = []

        async def fake_llm(messages, **kwargs):
            calls.append(messages)
            return json_dumps(VALID_SPEC)

        monkeypatch.setattr(excel_gen, "_call_llm", fake_llm)
        spec = await excel_gen._generate_workbook_json("demo brief", "")
        assert spec["filename"] == "demo.xlsx"
        assert len(calls) == 1

    def test_prompts_live_in_markdown_files(self):
        # Prompts are NEVER inlined in code — both Excel prompts are
        # loaded from prompts/*.md via the get_prompt loader.
        from app.prompts import get_prompt
        from pathlib import Path

        prompts_dir = Path(app_prompts.__file__).parent
        assert (prompts_dir / "excel_system.md").exists()
        assert (prompts_dir / "excel_simplified.md").exists()
        assert excel_gen.EXCEL_SYSTEM_PROMPT == get_prompt("excel_system")
        assert excel_gen._SIMPLIFIED_EXCEL_PROMPT == get_prompt("excel_simplified")
        # the simplified prompt is really the compact fallback contract
        assert "ONE JSON workbook" in excel_gen._SIMPLIFIED_EXCEL_PROMPT

    @pytest.mark.asyncio
    async def test_repair_round(self, monkeypatch):
        responses = [
            json_dumps({"sheets": "not-a-list"}),  # invalid
            json_dumps(VALID_SPEC),  # repaired
        ]

        async def fake_llm(messages, **kwargs):
            return responses.pop(0)

        monkeypatch.setattr(excel_gen, "_call_llm", fake_llm)
        spec = await excel_gen._generate_workbook_json("demo brief", "use formulas")
        assert spec["sheets"][0]["name"] == "Data"

    @pytest.mark.asyncio
    async def test_repair_prompt_contains_errors(self, monkeypatch):
        prompts = []

        async def fake_llm(messages, **kwargs):
            prompts.append(messages)
            if len(messages) == 2:  # first attempt
                return json_dumps({"sheets": []})
            return json_dumps(VALID_SPEC)

        monkeypatch.setattr(excel_gen, "_call_llm", fake_llm)
        await excel_gen._generate_workbook_json("brief", "")
        repair = prompts[1][-1]["content"]
        assert "INVALID" in repair
        assert "sheets" in repair

    @pytest.mark.asyncio
    async def test_double_failure_raises(self, monkeypatch):
        async def fake_llm(messages, **kwargs):
            return "I refuse to output JSON"

        monkeypatch.setattr(excel_gen, "_call_llm", fake_llm)
        with pytest.raises(RuntimeError, match="failed validation"):
            await excel_gen._generate_workbook_json("brief", "")

    @pytest.mark.asyncio
    async def test_fenced_output_handled(self, monkeypatch):
        async def fake_llm(messages, **kwargs):
            return f"```json\n{json_dumps(VALID_SPEC)}\n```"

        monkeypatch.setattr(excel_gen, "_call_llm", fake_llm)
        spec = await excel_gen._generate_workbook_json("brief", "")
        assert spec["sheets"]

    @pytest.mark.asyncio
    async def test_wrapper_unwrapped_in_flow(self, monkeypatch):
        async def fake_llm(messages, **kwargs):
            return json_dumps({"workbook": VALID_SPEC})

        monkeypatch.setattr(excel_gen, "_call_llm", fake_llm)
        spec = await excel_gen._generate_workbook_json("brief", "")
        assert "sheets" in spec

    @pytest.mark.asyncio
    async def test_three_attempt_flow_uses_simplified_prompt_on_third(
        self, monkeypatch
    ):
        """Two failures followed by a simplified-prompt success.

        The third call's system message must be the simplified one (much
        shorter than the full prompt). Verifies the new fallback path.
        """
        responses = [
            "I refuse to output JSON",  # attempt 1: parse failure
            json_dumps({"sheets": "still bad"}),  # attempt 2: validation failure
            json_dumps(
                {
                    "filename": "simple.xlsx",
                    "sheets": [
                        {
                            "name": "S",
                            "tables": [
                                {
                                    "headers": ["A", "B"],
                                    "rows": [[1, 2]],
                                    "total_row": True,
                                }
                            ],
                        }
                    ],
                }
            ),  # attempt 3: simplified-prompt success
        ]
        system_messages_seen = []

        async def fake_llm(messages, **kwargs):
            # The system message is always messages[0]; its content is
            # what tells us which prompt was used.
            system_messages_seen.append(messages[0]["content"])
            return responses.pop(0)

        monkeypatch.setattr(excel_gen, "_call_llm", fake_llm)
        spec = await excel_gen._generate_workbook_json("brief", "")
        assert spec["filename"] == "simple.xlsx"
        # The 3rd call must have used the simplified prompt.
        assert system_messages_seen[2] == excel_gen._SIMPLIFIED_EXCEL_PROMPT
        # The 1st and 2nd calls use the full prompt.
        assert system_messages_seen[0] == excel_gen.EXCEL_SYSTEM_PROMPT
        assert system_messages_seen[1] == excel_gen.EXCEL_SYSTEM_PROMPT

    @pytest.mark.asyncio
    async def test_post_processing_runs_on_every_attempt(self, monkeypatch):
        """Even attempt 1 benefits from shortcut expansion.

        The LLM emits `total_row: true` and post-processing expands it
        to a proper list of SUM formulas before validation runs.
        """

        async def fake_llm(messages, **kwargs):
            return json_dumps(
                {
                    "filename": "x.xlsx",
                    "sheets": [
                        {
                            "name": "S",
                            "freeze_header": True,
                            "tables": [
                                {
                                    "headers": ["Item", "Price"],
                                    "rows": [["A", 1], ["B", 2]],
                                    "total_row": True,
                                }
                            ],
                        }
                    ],
                }
            )

        monkeypatch.setattr(excel_gen, "_call_llm", fake_llm)
        spec = await excel_gen._generate_workbook_json("brief", "")
        table = spec["sheets"][0]["tables"][0]
        # Shortcut was expanded to a real list with SUM formulas.
        assert isinstance(table["total_row"], list)
        assert table["total_row"][1].startswith("=SUM(B")
        # freeze_header was expanded to freeze_panes.
        assert spec["sheets"][0]["freeze_panes"] == "A2"

    @pytest.mark.asyncio
    async def test_json_repair_saves_attempt_that_would_have_failed(self, monkeypatch):
        """The exact failing JSON from the project logs (missing `]`)
        must now parse and produce a valid spec on attempt 1.
        """
        broken = (
            '{"filename":"shopping_list.xlsx","sheets":['
            '{"name":"Shopping List","tables":['
            '{"start_cell":"A1","headers":["Item","Qty","Price"],'
            '"rows":[["Milk",2,3.50],["Bread",1,2.75]],'
            '"total_row":true,'
            '"auto_filter":true}},'
            '{"name":"Notes","text_blocks":[{"cell":"A1","text":"hi"}]}'
            "]}"
        )

        async def fake_llm(messages, **kwargs):
            return broken

        monkeypatch.setattr(excel_gen, "_call_llm", fake_llm)
        spec = await excel_gen._generate_workbook_json("shopping list", "")
        assert spec["filename"] == "shopping_list.xlsx"
        assert spec["sheets"][0]["name"] == "Shopping List"
        assert len(spec["sheets"][0]["tables"][0]["rows"]) == 2
        # total_row shortcut was expanded to SUM formulas.
        tr = spec["sheets"][0]["tables"][0]["total_row"]
        assert any(isinstance(v, str) and v.startswith("=SUM(B") for v in tr)
        assert any(isinstance(v, str) and v.startswith("=SUM(C") for v in tr)

    @pytest.mark.asyncio
    async def test_triple_failure_raises_with_three_attempts_message(self, monkeypatch):
        async def fake_llm(messages, **kwargs):
            return "not json at all"

        monkeypatch.setattr(excel_gen, "_call_llm", fake_llm)
        with pytest.raises(RuntimeError, match="3 attempts"):
            await excel_gen._generate_workbook_json("brief", "")


# ─────────────────────────────────────────────────────────────────────
# 7. Public API contract
# ─────────────────────────────────────────────────────────────────────


class TestPublicApi:
    @pytest.mark.asyncio
    async def test_contract(self, tmp_path, monkeypatch):
        async def fake_spec(brief, requirements):
            return excel_gen._normalize_spec(VALID_SPEC)

        monkeypatch.setattr(excel_gen, "_generate_workbook_json", fake_spec)
        monkeypatch.setattr(excel_gen, "_get_reports_dir", lambda: tmp_path)

        result = await excel_gen.generate_spreadsheet("Demo budget tracker")
        assert result["type"] == "excel"
        assert result["format"] == "xlsx"
        assert result["filename"] == "demo.xlsx"
        assert result["file_path"] == f"reports/{result['report_id']}.xlsx"
        assert result["download_url"] == f"/api/reports/{result['report_id']}/download"
        assert result["created_at"] > 0
        assert result["sheet_count"] == 1
        assert (tmp_path / f"{result['report_id']}.xlsx").exists()

    @pytest.mark.asyncio
    async def test_filename_from_brief_when_spec_missing(self, tmp_path, monkeypatch):
        spec = excel_gen._normalize_spec(VALID_SPEC)
        spec["filename"] = None

        async def fake_spec(brief, requirements):
            return spec

        monkeypatch.setattr(excel_gen, "_generate_workbook_json", fake_spec)
        monkeypatch.setattr(excel_gen, "_get_reports_dir", lambda: tmp_path)

        result = await excel_gen.generate_spreadsheet("Loan Amortization!! 2025")
        assert result["filename"] == "Loan_Amortization_2025.xlsx"

    @pytest.mark.asyncio
    async def test_empty_brief_raises(self):
        with pytest.raises(ValueError):
            await excel_gen.generate_spreadsheet("   ")

    @pytest.mark.asyncio
    async def test_requirements_forwarded(self, tmp_path, monkeypatch):
        captured = {}

        async def fake_spec(brief, requirements):
            captured["brief"] = brief
            captured["requirements"] = requirements
            return excel_gen._normalize_spec(VALID_SPEC)

        monkeypatch.setattr(excel_gen, "_generate_workbook_json", fake_spec)
        monkeypatch.setattr(excel_gen, "_get_reports_dir", lambda: tmp_path)
        await excel_gen.generate_spreadsheet("my brief", "2 sheets, use formulas")
        assert captured["requirements"] == "2 sheets, use formulas"


# ─────────────────────────────────────────────────────────────────────
# 8. Agent tool wrapper + service integration
# ─────────────────────────────────────────────────────────────────────


class TestExcelGenTool:
    def test_registered(self):
        from app.agent.base import tool_registry

        tool = tool_registry.get("use_excel_gen")
        assert tool is not None
        assert tool.name == "use_excel_gen"
        assert tool.tool_type.value == "image_gen"

    def test_parameters(self):
        from app.agent.base import tool_registry

        tool = tool_registry.get("use_excel_gen")
        params = tool.get_parameters()
        assert set(params.keys()) == {"brief", "requirements"}
        assert tool.get_required_params() == ["brief"]

    @pytest.mark.asyncio
    async def test_execute_success(self, tmp_path, monkeypatch):
        from app.agent.base import tool_registry

        async def fake_gen(brief, requirements):
            return {
                "type": "excel",
                "format": "xlsx",
                "filename": "demo.xlsx",
                "file_path": "reports/r1.xlsx",
                "download_url": "/api/reports/r1/download",
                "report_id": "r1",
                "created_at": 1234,
                "sheet_count": 2,
                "table_count": 3,
                "chart_count": 1,
                "formula_count": 47,
                "sheet_names": ["Data", "Summary"],
                "summary": (
                    "2 sheets, 3 tables, 1 charts, 47 live formulas (Data, Summary)"
                ),
            }

        # Patch where the TOOL imports it (bound reference), not just
        # the defining module.
        monkeypatch.setattr("app.agent.tools.excel_gen.generate_spreadsheet", fake_gen)
        tool = tool_registry.get("use_excel_gen")
        result = await tool.execute(brief="demo brief", requirements="")
        assert result.success
        tc = result.tool_call
        assert tc.status == "completed"
        assert tc.gen_results[0]["type"] == "excel"
        assert tc.gen_results[0]["filename"] == "demo.xlsx"
        assert tc.gen_results[0]["format"] == "xlsx"
        # the workbook summary travels back to the agent (and rides
        # along in the deliverable metadata for the UI)
        assert tc.gen_results[0]["summary"] == (
            "2 sheets, 3 tables, 1 charts, 47 live formulas (Data, Summary)"
        )
        assert "2 sheets, 3 tables" in result.output
        assert "ready for download" in result.output

    @pytest.mark.asyncio
    async def test_execute_empty_brief(self):
        from app.agent.base import tool_registry

        tool = tool_registry.get("use_excel_gen")
        result = await tool.execute(brief="  ")
        assert not result.success
        assert result.tool_call.status == "error"
        assert "brief" in result.output.lower()

    @pytest.mark.asyncio
    async def test_execute_service_failure(self, monkeypatch):
        from app.agent.base import tool_registry

        async def failing_gen(brief, requirements):
            raise RuntimeError("boom")

        monkeypatch.setattr(
            "app.agent.tools.excel_gen.generate_spreadsheet", failing_gen
        )
        tool = tool_registry.get("use_excel_gen")
        result = await tool.execute(brief="demo")
        assert not result.success
        assert "boom" in result.output
        assert result.tool_call.status == "error"

    def test_param_aliases(self):
        from app.agent.base import tool_registry

        tool = tool_registry.get("use_excel_gen")
        resolved = tool._apply_aliases({"topic": "t", "constraints": "c"})
        assert resolved["brief"] == "t"
        assert resolved["requirements"] == "c"


class TestAgentServiceIntegration:
    def test_keywords_select_excel_tool(self):
        from app.agent import service

        for kw in ("excel", "spreadsheet", "xlsx", "workbook"):
            selected = service._select_tools(f"make me a {kw} please")
            assert "use_excel_gen" in selected, kw

    def test_fenced_parsing_with_requirements(self):
        from app.agent import service

        text = (
            "```use_excel_gen\n"
            "Compile these results into a sheet\n"
            "requirements: 2 sheets, live formulas\n"
            "```"
        )
        calls = service._parse_tool_calls(text)
        assert calls == [
            {
                "tool": "use_excel_gen",
                "args": {
                    "brief": "Compile these results into a sheet",
                    "requirements": "2 sheets, live formulas",
                },
            }
        ]

    def test_fenced_parsing_brief_only(self):
        from app.agent import service

        calls = service._parse_tool_calls(
            "```use_excel_gen\nturn this data into an Excel file\n```"
        )
        assert calls[0]["args"]["brief"] == "turn this data into an Excel file"
        assert "requirements" not in calls[0]["args"]

    def test_deliverable_persistence_types(self):
        """chat.py _BlockBuilder must persist type 'excel' deliverables."""
        from app.api.chat import _BlockBuilder  # noqa: F401

        builder = _BlockBuilder()
        builder.on_tool_call_update(
            "tc1",
            {
                "genResults": [
                    {
                        "type": "excel",
                        "format": "xlsx",
                        "filename": "demo.xlsx",
                        "file_path": "reports/x.xlsx",
                        "download_url": "/api/reports/x/download",
                        "report_id": "x",
                        "created_at": 1,
                    }
                ]
            },
        )
        assert builder.deliverables
        assert builder.deliverables[0]["type"] == "excel"
