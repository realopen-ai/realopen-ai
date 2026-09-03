"""
Excel generation service — LLM emits a strict JSON workbook spec, a
deterministic openpyxl converter turns it into a real .xlsx file.

Pipeline (mirrors report_gen.py / pptx_gen.py):
  1. Specialized Excel AI call — the LLM receives a brief (+ optional
     requirements) and emits a STRICT JSON workbook specification. It
     decides the structure: sheet names, columns, whether to add a
     summary/notes sheet, realistic demo data, live formulas, charts.
     It NEVER emits markdown, python, or prose — only one JSON object.
  2. Validation — the JSON is checked against the schema below plus
     sanity checks (cell refs, sheet names, formula syntax, size
     limits, table overlaps). On failure the LLM gets ONE repair
     round with the validation errors attached, then the service
     gives up with a clear error.
  3. Deterministic conversion — pure openpyxl (no LLM): tables with
     styled headers, zebra rows, number formats, live formulas,
     freeze panes, column widths, merged cells, tab colors, notes,
     and native Excel charts. Warnings are auto-fixed (padded rows,
     defaulted colors, capped sizes) so the converter never sees an
     invalid spec.
  4. Save to data/reports/{report_id}.xlsx and return the same
     deliverable contract as report/pptx generation, so the
     use_excel_gen agent tool and the frontend badge pipeline work
     unchanged.

════════════════════════════════════════════════════════════════════
WORKBOOK JSON SPEC
════════════════════════════════════════════════════════════════════

Root object:
  filename   optional string. Sanitized; ".xlsx" appended when missing.
             The download filename shown in the UI.
  sheets     required array of SHEET, 1..MAX_SHEETS (12) entries.

SHEET object:
  name          required string, 1-31 chars, no [ ] : * ? / \\,
                unique across the workbook (case-insensitive).
  tab_color     optional "RRGGBB".
  freeze_panes  optional cell ref such as "A2" — rows above + columns
                left of it stay frozen while scrolling.
  column_widths optional map {column letter → width in chars}.
  merged_cells  optional array of ranges ["A1:C1", ...].
  notes         optional string — rendered under the content in muted
                italic ("Sheet-level documentation").
  text_blocks   optional array of TEXT_BLOCK (free labels/headings).
  tables        optional array of TABLE.
  formulas      optional — EITHER an array
                [{"cell": "B10", "formula": "=SUM(B2:B9)"}, ...]
                OR a map {"B10": "=SUM(B2:B9)", ...}.
                Written AFTER tables/text so they override.
  charts        optional array of CHART (floating, anchored to a cell).

TABLE object:
  start_cell      default "A1" — top-left corner of the header row.
  title           optional string — bold heading placed above the
                  header row (start_cell shifts down by 1).
  headers         required non-empty array of strings.
  rows            required array of arrays. Cell values are string,
                  number, boolean, or null (empty). Any string that
                  starts with "=" is written as a LIVE formula.
  number_formats  optional map — keys are column letters ("B") or
                  header names ("Revenue"); values are Excel number
                  format strings ("#,##0.00", "0.0%", "yyyy-mm-dd").
  total_row       optional array — appended under the data with a bold
                  style + top rule. Entries may be formulas. The
                  placeholders {first_row}/{last_row} are replaced
                  with the table's first/last data row numbers.
  zebra           default true — alternating row tint.
  auto_filter     default false — adds Excel's filter dropdowns on the
                  header row.
  fill_down       optional {"rows": N, "exclude_columns": ["A", ...]} —
                  replicates the LAST data row N more times, shifting
                  relative formula refs exactly like Excel fill-down
                  (openpyxl Translator). Numeric cells whose last two
                  values form a sequence continue it (1, 2 → 3, 4, …);
                  other literals repeat. Used for amortization
                  schedules, cumulative series, projections.

TEXT_BLOCK object:
  cell        required cell ref.
  text        required string.
  bold / italic   default false.
  font_size   default 11 (clamped 6..72).
  font_color  optional "RRGGBB".
  wrap        default false.

CHART object:
  type              "bar" | "bar_h" | "line" | "area" | "pie" | "scatter".
  title             optional string.
  anchor            cell ref, default "E2".
  width / height    optional cm (defaults 15 × 9, clamped).
  categories_range  optional "Sheet1!A2:A13" — x-axis / pie labels.
  series            required non-empty array of
                    {"name": "Revenue", "values_range": "Sheet1!B2:B13"}.
                    When name is missing, the header cell above the
                    range is used. Ranges may use {last_row}
                    (= last data row of the last table on that sheet).
                    Sheet names with spaces use 'My Sheet'!B2:B13.

Value typing rules for the LLM:
  numbers as JSON numbers, text as strings, booleans as JSON
  booleans, empty as null. Dates as "YYYY-MM-DD" strings — the
  converter turns them into real dates with a "yyyy-mm-dd" format.

Design system (matches report_gen.py tokens):
  Navy  16304F — header fills, chart series 1
  Steel 1B3A5C — series 2
  Gold  C9A227 — accents, series 3
  Ink   2A2F36 — body text
  Muted 5C6470 — notes/secondary text
  Zebra F4F6F9 — alternating rows
  Callout FAF6EA — total-row tint
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import date as _date, datetime as _datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.chart import (
    AreaChart,
    BarChart,
    LineChart,
    PieChart,
    Reference,
    ScatterChart,
    Series,
)
from openpyxl.chart.label import DataLabelList
from openpyxl.chart.marker import DataPoint
from openpyxl.formula.translate import Translator
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import column_index_from_string, get_column_letter

from app.config import settings

logger = logging.getLogger(__name__)

SPEC_VERSION = 1


def _log(msg: str, *args) -> None:
    try:
        formatted = msg % args if args else msg
    except (TypeError, ValueError):
        formatted = f"{msg} {args}"
    print(f"[excel_gen] {formatted}", flush=True)


# ── Limits (protect the LLM token budget + converter runtime) ────────

MAX_SHEETS = 12
MAX_TABLES_PER_SHEET = 6
MAX_TEXT_BLOCKS_PER_SHEET = 30
MAX_CHARTS_PER_SHEET = 6
MAX_SERIES_PER_CHART = 8
MAX_HEADERS_PER_TABLE = 40
MAX_ROWS_PER_TABLE = 500  # explicit rows in the JSON
MAX_FILL_DOWN_ROWS = 5000  # expanded rows via fill_down
MAX_TOTAL_CELLS = 60_000
MAX_ROW_INDEX = 1_048_576  # Excel hard limit
MAX_COL_INDEX = 16_384  # Excel hard limit (XFD)

# Design tokens (same as report_gen.py)
NAVY = "16304F"
STEEL = "1B3A5C"
GOLD = "C9A227"
INK = "2A2F36"
MUTED = "5C6470"
ZEBRA = "F4F6F9"
CALLOUT_BG = "FAF6EA"
BORDER_COLOR = "D9DEE7"
CHART_SERIES_COLORS = [NAVY, GOLD, STEEL, "8A94A3", "C9554B", "3D7A5C"]


# ── Cell / range helpers ──────────────────────────────────────────────

_CELL_RE = re.compile(r"^([A-Za-z]{1,3})([1-9][0-9]{0,6})$")
_RANGE_SPLIT_RE = re.compile(r"^([^!]+!)?(.+)$")
_RANGE_RE = re.compile(
    r"^(?:'((?:[^']|'')*)'!|([^'!:]+)!)?"
    r"\$?([A-Za-z]{1,3})\$?([1-9][0-9]{0,6})"
    r"(?::\$?([A-Za-z]{1,3})\$?([1-9][0-9]{0,6}))?$"
)
_HEX_COLOR_RE = re.compile(r"^[0-9A-Fa-f]{6}$")
# Safe characters for Excel custom number formats (letters cover dates,
# "General", "Text", colors in [], AM/PM, etc.)
_NUMFMT_ALLOWED = set(
    "#0123456789.,%-$€£¥()[]/:+\"'\\*_&@?<>; "
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
)
_INT_STR_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)$")
_FLOAT_STR_RE = re.compile(r"^-?(?:[0-9]+\.[0-9]*|\.[0-9]+)$")
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_SHEET_REF_IN_FORMULA_RE = re.compile(r"(?:'([^']+)'|([A-Za-z0-9_][A-Za-z0-9_. ]*))!")


def cell_to_indices(cell: str) -> Tuple[int, int]:
    """Convert an A1 cell ref like "B12" → (row, col) 1-based indices.

    Raises ValueError for invalid refs, non-string input, or
    out-of-bounds coordinates.
    """
    if not isinstance(cell, str):
        raise ValueError(f"Invalid cell reference: {cell!r}")
    m = _CELL_RE.match(cell.strip())
    if not m:
        raise ValueError(f"Invalid cell reference: {cell!r}")
    col = column_index_from_string(m.group(1).upper())
    row = int(m.group(2))
    if col > MAX_COL_INDEX:
        raise ValueError(f"Column out of range in {cell!r} (max XFD)")
    if row > MAX_ROW_INDEX:
        raise ValueError(f"Row out of range in {cell!r} (max {MAX_ROW_INDEX})")
    return row, col


def is_valid_cell(cell: Any) -> bool:
    if not isinstance(cell, str):
        return False
    try:
        cell_to_indices(cell)
        return True
    except ValueError:
        return False


def parse_range(range_str: str, default_sheet: str) -> Tuple[str, int, int, int, int]:
    """Parse "Sheet1!B2:B13" / "'My Sheet'!A1" / "B2" →
    (sheet_name, min_row, max_row, min_col, max_col).

    Raises ValueError when the range cannot be parsed.
    """
    if not isinstance(range_str, str) or not range_str.strip():
        raise ValueError(f"Invalid range: {range_str!r}")
    m = _RANGE_RE.match(range_str.strip())
    if not m:
        raise ValueError(f"Invalid range: {range_str!r}")
    quoted, unquoted, c1, r1, c2, r2 = m.groups()
    sheet = (
        quoted.replace("''", "'")
        if quoted is not None
        else (unquoted if unquoted is not None else default_sheet)
    )
    col1 = column_index_from_string(c1.upper())
    row1 = int(r1)
    if c2 is not None and r2 is not None:
        col2 = column_index_from_string(c2.upper())
        row2 = int(r2)
    else:
        col2, row2 = col1, row1
    if max(col1, col2) > MAX_COL_INDEX or max(row1, row2) > MAX_ROW_INDEX:
        raise ValueError(f"Range out of Excel bounds: {range_str!r}")
    return sheet, min(row1, row2), max(row1, row2), min(col1, col2), max(col1, col2)


def valid_hex_color(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX_COLOR_RE.match(value))


def valid_number_format(fmt: Any) -> bool:
    if not isinstance(fmt, str) or not fmt or len(fmt) > 80:
        return False
    return all(ch in _NUMFMT_ALLOWED for ch in fmt)


def _sanitize_cell_text(value: str) -> str:
    """Strip characters openpyxl/Excel reject inside cell strings."""
    cleaned = ILLEGAL_CHARACTERS_RE.sub("", value)
    # Also strip remaining C0 controls openpyxl's regex may allow
    cleaned = "".join(ch for ch in cleaned if ch >= " " or ch in "\t\n")
    return cleaned


def _default_date_format(value: Any) -> Optional[str]:
    """Number format for date values when no explicit format was given."""
    if isinstance(value, _datetime):
        return "yyyy-mm-dd hh:mm:ss"
    if isinstance(value, _date):
        return "yyyy-mm-dd"
    return None


# ── Spec validation ───────────────────────────────────────────────────


def validate_workbook_spec(spec: Any) -> Tuple[List[str], List[str]]:
    """Validate a parsed workbook spec.

    Returns (errors, warnings):
      errors   — schema violations that make conversion impossible.
                 Trigger an LLM repair round.
      warnings — quality issues auto-fixed during normalization
                 (row padding, capped sizes, defaulted colors…).
    """
    errors: List[str] = []
    warnings: List[str] = []

    if not isinstance(spec, dict):
        return ["root must be a JSON object"], []

    if not isinstance(spec.get("sheets"), list) or not spec["sheets"]:
        return ["'sheets' must be a non-empty array"], []

    if len(spec["sheets"]) > MAX_SHEETS:
        errors.append(f"too many sheets ({len(spec['sheets'])} > {MAX_SHEETS})")

    if (
        "filename" in spec
        and spec["filename"] is not None
        and not isinstance(spec["filename"], str)
    ):
        errors.append("'filename' must be a string")

    seen_names = set()
    total_cells = 0
    tables_by_sheet: Dict[int, List[dict]] = {}

    for si, sheet in enumerate(spec["sheets"]):
        ctx = f"sheets[{si}]"
        if not isinstance(sheet, dict):
            errors.append(f"{ctx}: must be an object")
            continue

        name = sheet.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{ctx}: 'name' must be a non-empty string")
            continue
        if len(name) > 31:
            errors.append(f"{ctx}: sheet name {name!r} longer than 31 chars")
        if re.search(r"[\[\]:*?/\\]", name):
            errors.append(
                f"{ctx}: sheet name {name!r} contains forbidden chars [ ] : * ? / \\"
            )
        key = name.strip().lower()
        if key in seen_names:
            errors.append(f"{ctx}: duplicate sheet name {name!r}")
        seen_names.add(key)

        sheet_errors, sheet_warnings, table_list = _validate_sheet(ctx, sheet)
        errors.extend(sheet_errors)
        warnings.extend(sheet_warnings)
        tables_by_sheet[si] = table_list
        total_cells += sum(
            len(t.get("headers", [])) * (1 + len(t.get("rows", []))) for t in table_list
        )

    # Table overlap detection (same sheet, intersecting rectangles)
    for si, table_list in tables_by_sheet.items():
        rects = []
        for t in table_list:
            try:
                start = t.get("start_cell", "A1")
                r, c = cell_to_indices(start)
                ncols = max(len(t.get("headers", [])), 1)
                offset = 1 if t.get("title") else 0
                nrows = 1 + len(t.get("rows", [])) + (1 if t.get("total_row") else 0)
                # fill_down extension
                fd = t.get("fill_down") or {}
                nrows += min(_as_int(fd.get("rows"), 0), MAX_FILL_DOWN_ROWS)
                rects.append(
                    (r + offset, c, r + offset + nrows - 1, c + ncols - 1, start)
                )
            except (ValueError, TypeError):
                continue  # already reported as an error
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                r1, c1, r1e, c1e, s1 = rects[i]
                r2, c2, r2e, c2e, s2 = rects[j]
                if not (r1e < r2 or r2e < r1 or c1e < c2 or c2e < c1):
                    errors.append(f"sheets[{si}]: tables at {s1} and {s2} overlap")

    if total_cells > MAX_TOTAL_CELLS:
        errors.append(f"workbook too large ({total_cells} cells > {MAX_TOTAL_CELLS})")

    return errors, warnings


def _validate_sheet(ctx: str, sheet: dict) -> Tuple[List[str], List[str], List[dict]]:
    errors: List[str] = []
    warnings: List[str] = []
    table_list: List[dict] = []

    if (
        "tab_color" in sheet
        and sheet["tab_color"] is not None
        and not valid_hex_color(sheet["tab_color"])
    ):
        warnings.append(
            f"{ctx}: invalid tab_color {sheet['tab_color']!r} — using default"
        )

    if "freeze_panes" in sheet and sheet["freeze_panes"] is not None:
        if not is_valid_cell(sheet["freeze_panes"]):
            errors.append(f"{ctx}: invalid freeze_panes {sheet['freeze_panes']!r}")
        elif sheet["freeze_panes"].upper() == "A1":
            warnings.append(f"{ctx}: freeze_panes A1 freezes nothing — ignored")

    if "column_widths" in sheet and sheet["column_widths"] is not None:
        cw = sheet["column_widths"]
        if not isinstance(cw, dict):
            errors.append(f"{ctx}: 'column_widths' must be an object")
        else:
            for col, width in cw.items():
                if not re.match(r"^[A-Za-z]{1,3}$", str(col)):
                    errors.append(
                        f"{ctx}: column_widths key {col!r} is not a column letter"
                    )
                if not isinstance(width, (int, float)) or not (1 <= width <= 100):
                    warnings.append(
                        f"{ctx}: column width for {col!r} out of range — clamped"
                    )

    if "merged_cells" in sheet and sheet["merged_cells"] is not None:
        mc = sheet["merged_cells"]
        if not isinstance(mc, list):
            errors.append(f"{ctx}: 'merged_cells' must be an array")
        else:
            for rng in mc:
                try:
                    parse_range(str(rng), sheet.get("name", "Sheet"))
                except ValueError:
                    errors.append(f"{ctx}: invalid merged_cells range {rng!r}")

    if "notes" in sheet and sheet["notes"] is not None:
        if not isinstance(sheet["notes"], str):
            errors.append(f"{ctx}: 'notes' must be a string")
        elif len(sheet["notes"]) > 2000:
            warnings.append(f"{ctx}: notes longer than 2000 chars — truncated")

    tables = sheet.get("tables", [])
    if tables is not None:
        if not isinstance(tables, list):
            errors.append(f"{ctx}: 'tables' must be an array")
            tables = []
        if len(tables) > MAX_TABLES_PER_SHEET:
            warnings.append(
                f"{ctx}: {len(tables)} tables > {MAX_TABLES_PER_SHEET} — truncated"
            )
        for ti, table in enumerate(tables[:MAX_TABLES_PER_SHEET]):
            tctx = f"{ctx}.tables[{ti}]"
            if not isinstance(table, dict):
                errors.append(f"{tctx}: must be an object")
                continue
            table_list.append(table)
            errors.extend(_validate_table(tctx, table, sheet.get("name", "Sheet")))

    text_blocks = sheet.get("text_blocks", [])
    if text_blocks is not None:
        if not isinstance(text_blocks, list):
            errors.append(f"{ctx}: 'text_blocks' must be an array")
        else:
            if len(text_blocks) > MAX_TEXT_BLOCKS_PER_SHEET:
                warnings.append(
                    f"{ctx}: {len(text_blocks)} text_blocks > {MAX_TEXT_BLOCKS_PER_SHEET} — truncated"
                )
            for bi, block in enumerate(text_blocks[:MAX_TEXT_BLOCKS_PER_SHEET]):
                bctx = f"{ctx}.text_blocks[{bi}]"
                if not isinstance(block, dict):
                    errors.append(f"{bctx}: must be an object")
                    continue
                if not is_valid_cell(block.get("cell")):
                    errors.append(f"{bctx}: invalid 'cell' {block.get('cell')!r}")
                if not isinstance(block.get("text"), (str, int, float, bool)):
                    errors.append(
                        f"{bctx}: 'text' must be a string, number, or boolean"
                    )
                if (
                    "font_color" in block
                    and block["font_color"] is not None
                    and not valid_hex_color(block["font_color"])
                ):
                    warnings.append(f"{bctx}: invalid font_color — using default")
                if "font_size" in block and not isinstance(
                    block.get("font_size"), (int, float)
                ):
                    warnings.append(
                        f"{bctx}: font_size must be a number — using default"
                    )

    formulas = sheet.get("formulas")
    if formulas is not None:
        pairs = _normalize_formula_pairs(formulas)
        if pairs is None:
            errors.append(
                f"{ctx}: 'formulas' must be an array of {{cell, formula}} "
                "or a map {cell: formula}"
            )
        else:
            for cell, formula in pairs:
                if not is_valid_cell(cell):
                    errors.append(f"{ctx}.formulas: invalid cell {cell!r}")
                if not isinstance(formula, str) or not formula.strip():
                    errors.append(
                        f"{ctx}.formulas: formula for {cell!r} must be a non-empty string"
                    )
                elif not formula.lstrip().startswith("="):
                    warnings.append(
                        f"{ctx}.formulas: formula for {cell!r} missing '=' — prepended"
                    )

    charts = sheet.get("charts", [])
    if charts is not None:
        if not isinstance(charts, list):
            errors.append(f"{ctx}: 'charts' must be an array")
        else:
            if len(charts) > MAX_CHARTS_PER_SHEET:
                warnings.append(
                    f"{ctx}: {len(charts)} charts > {MAX_CHARTS_PER_SHEET} — truncated"
                )
            for ci, chart in enumerate(charts[:MAX_CHARTS_PER_SHEET]):
                errors.extend(_validate_chart(f"{ctx}.charts[{ci}]", chart))

    has_content = bool(
        table_list
        or sheet.get("text_blocks")
        or sheet.get("formulas")
        or sheet.get("notes")
    )
    if not has_content:
        warnings.append(f"{ctx}: sheet {sheet.get('name')!r} has no content")

    return errors, warnings, table_list


def _validate_table(ctx: str, table: dict, sheet_name: str) -> List[str]:
    errors: List[str] = []

    start = table.get("start_cell", "A1")
    if start is None:
        start = "A1"
    if not is_valid_cell(start):
        errors.append(f"{ctx}: invalid start_cell {start!r}")

    headers = table.get("headers")
    if not isinstance(headers, list) or not headers:
        errors.append(f"{ctx}: 'headers' must be a non-empty array")
        return errors
    if len(headers) > MAX_HEADERS_PER_TABLE:
        errors.append(f"{ctx}: {len(headers)} headers > {MAX_HEADERS_PER_TABLE}")
    for hi, h in enumerate(headers):
        if isinstance(h, (dict, list)):
            errors.append(f"{ctx}.headers[{hi}]: must be a scalar value")

    rows = table.get("rows")
    if not isinstance(rows, list):
        errors.append(f"{ctx}: 'rows' must be an array")
        return errors
    for ri, row in enumerate(rows[:MAX_ROWS_PER_TABLE]):
        if not isinstance(row, list):
            errors.append(f"{ctx}.rows[{ri}]: must be an array")
            continue
        for ci, val in enumerate(row):
            if isinstance(val, (dict, list)):
                errors.append(
                    f"{ctx}.rows[{ri}][{ci}]: value must be a string/number/boolean/null"
                )

    total_row = table.get("total_row")
    if total_row is not None:
        if not isinstance(total_row, list):
            errors.append(f"{ctx}: 'total_row' must be an array")

    fill_down = table.get("fill_down")
    if fill_down is not None:
        if not isinstance(fill_down, dict):
            errors.append(f"{ctx}: 'fill_down' must be an object")
        else:
            fd_rows = fill_down.get("rows")
            if not isinstance(fd_rows, (int, float)) or fd_rows < 0:
                errors.append(f"{ctx}: fill_down.rows must be a non-negative number")
            exc = fill_down.get("exclude_columns")
            if exc is not None and not isinstance(exc, list):
                errors.append(f"{ctx}: fill_down.exclude_columns must be an array")

    number_formats = table.get("number_formats")
    if number_formats is not None:
        if not isinstance(number_formats, dict):
            errors.append(f"{ctx}: 'number_formats' must be an object")

    header_style = table.get("header_style")
    if header_style is not None and not isinstance(header_style, dict):
        errors.append(f"{ctx}: 'header_style' must be an object")

    return errors


def _validate_chart(ctx: str, chart: Any) -> List[str]:
    errors: List[str] = []
    if not isinstance(chart, dict):
        return [f"{ctx}: must be an object"]

    ctype = chart.get("type")
    if ctype not in ("bar", "bar_h", "line", "area", "pie", "scatter"):
        errors.append(
            f"{ctx}: type must be one of bar/bar_h/line/area/pie/scatter, got {ctype!r}"
        )

    if (
        "anchor" in chart
        and chart["anchor"] is not None
        and not is_valid_cell(chart["anchor"])
    ):
        errors.append(f"{ctx}: invalid anchor {chart['anchor']!r}")

    if "categories_range" in chart and chart["categories_range"] is not None:
        if not isinstance(chart["categories_range"], str):
            errors.append(f"{ctx}: categories_range must be a string")
        else:
            try:
                parse_range(
                    _expand_placeholder(chart["categories_range"], 100), "Sheet"
                )
            except ValueError:
                errors.append(
                    f"{ctx}: unparseable categories_range {chart['categories_range']!r}"
                )

    series = chart.get("series")
    if not isinstance(series, list) or not series:
        errors.append(f"{ctx}: 'series' must be a non-empty array")
        return errors
    for i, s in enumerate(series):
        sctx = f"{ctx}.series[{i}]"
        if not isinstance(s, dict):
            errors.append(f"{sctx}: must be an object")
            continue
        vr = s.get("values_range")
        if not isinstance(vr, str):
            errors.append(f"{sctx}: 'values_range' must be a string")
        else:
            try:
                parse_range(_expand_placeholder(vr, 100), "Sheet")
            except ValueError:
                errors.append(f"{sctx}: unparseable values_range {vr!r}")

    return errors


def _expand_placeholder(range_str: str, last_row: int) -> str:
    """Replace {last_row}/{first_row} placeholders for range parsing."""
    return range_str.replace("{last_row}", str(last_row)).replace("{first_row}", "1")


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_formula_pairs(formulas: Any) -> Optional[List[Tuple[str, str]]]:
    """Accept [{cell, formula}, ...] or {cell: formula} → [(cell, formula), ...]."""
    if isinstance(formulas, list):
        pairs = []
        for item in formulas:
            if not isinstance(item, dict):
                return None
            cell = item.get("cell")
            formula = item.get("formula")
            if not isinstance(cell, str) or not isinstance(formula, str):
                return None
            pairs.append((cell, formula))
        return pairs
    if isinstance(formulas, dict):
        pairs = []
        for cell, formula in formulas.items():
            if not isinstance(cell, str) or not isinstance(formula, str):
                return None
            pairs.append((cell, formula))
        return pairs
    return None


# ── Normalization (auto-fix warnings, apply defaults) ─────────────────


def _clamp(value: Any, lo: float, hi: float, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _coerce_cell_value(value: Any) -> Any:
    """Coerce LLM cell values into openpyxl-friendly values.

    - numeric-looking strings WITHOUT leading zeros → int/float so
      SUM() & friends work on them
    - ISO date strings "YYYY-MM-DD" → datetime.date (+ format later)
    - everything else sanitized for illegal characters
    """
    if isinstance(value, str):
        s = value.strip()
        if s and _INT_STR_RE.match(s) and len(s) < 16:
            return int(s)
        if s and _FLOAT_STR_RE.match(s):
            return float(s)
        m = _ISO_DATE_RE.match(s)
        if m:
            try:
                return _date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
        return _sanitize_cell_text(value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    return _sanitize_cell_text(str(value))


def _normalize_spec(spec: dict) -> dict:
    """Apply warning-level fixes so the converter sees a clean spec."""
    out: Dict[str, Any] = {
        "filename": spec.get("filename"),
        "sheets": [],
    }

    fname = out["filename"]
    if isinstance(fname, str) and fname.strip():
        base = re.sub(r"\.xlsx$", "", fname.strip(), flags=re.IGNORECASE)
        base = re.sub(r"[^\w\s-]", "", base)[:60].strip()
        base = re.sub(r"[\s_-]+", "_", base).strip("_")
        out["filename"] = f"{base}.xlsx" if base else None
    else:
        out["filename"] = None

    for sheet in spec["sheets"]:
        s: Dict[str, Any] = {"name": str(sheet.get("name", "Sheet")).strip()[:31]}

        if valid_hex_color(sheet.get("tab_color")):
            s["tab_color"] = sheet["tab_color"].upper()

        fp = sheet.get("freeze_panes")
        if is_valid_cell(fp) and fp.upper() != "A1":
            s["freeze_panes"] = fp.upper()

        cw = sheet.get("column_widths")
        if isinstance(cw, dict):
            s["column_widths"] = {
                str(k).upper(): _clamp(v, 1, 100, 12.0)
                for k, v in cw.items()
                if re.match(r"^[A-Za-z]{1,3}$", str(k))
            }

        mc = sheet.get("merged_cells")
        if isinstance(mc, list):
            good = []
            for rng in mc:
                try:
                    sheet_name, r1, r2, c1, c2 = parse_range(str(rng), s["name"])
                    good.append(
                        f"{get_column_letter(c1)}{r1}:{get_column_letter(c2)}{r2}"
                    )
                except ValueError:
                    continue
            if good:
                s["merged_cells"] = good

        notes = sheet.get("notes")
        if isinstance(notes, str) and notes.strip():
            s["notes"] = notes.strip()[:2000]

        # text blocks
        blocks = []
        for block in (sheet.get("text_blocks") or [])[:MAX_TEXT_BLOCKS_PER_SHEET]:
            if not isinstance(block, dict) or not is_valid_cell(block.get("cell")):
                continue
            raw_text = block.get("text")
            if not isinstance(raw_text, (str, int, float, bool)):
                continue
            # Scalar "text" values (e.g. 250000) are coerced like cell
            # values so numbers stay numbers and formulas stay formulas.
            text = _coerce_cell_value(raw_text)
            blocks.append(
                {
                    "cell": block["cell"].upper(),
                    "text": text,
                    "bold": bool(block.get("bold", False)),
                    "italic": bool(block.get("italic", False)),
                    "font_size": _clamp(block.get("font_size"), 6, 72, 11.0),
                    "font_color": (
                        block["font_color"].upper()
                        if valid_hex_color(block.get("font_color"))
                        else None
                    ),
                    "wrap": bool(block.get("wrap", False)),
                }
            )
        if blocks:
            s["text_blocks"] = blocks

        # tables
        tables = []
        for table in (sheet.get("tables") or [])[:MAX_TABLES_PER_SHEET]:
            if not isinstance(table, dict):
                continue
            headers = [
                (
                    _sanitize_cell_text(str(h))
                    if not isinstance(h, str)
                    else _sanitize_cell_text(h)
                )
                for h in (table.get("headers") or [])
            ][:MAX_HEADERS_PER_TABLE]
            if not headers:
                continue
            nrows = len(headers)
            rows_raw = (table.get("rows") or [])[:MAX_ROWS_PER_TABLE]
            rows = []
            for row in rows_raw:
                if not isinstance(row, list):
                    row = [row]
                row = row[:nrows] + [None] * (nrows - len(row[:nrows]))  # pad/truncate
                rows.append([_coerce_cell_value(v) for v in row])

            # fill_down
            fill_down_rows = 0
            exclude_cols: List[str] = []
            fd = table.get("fill_down")
            if isinstance(fd, dict):
                fill_down_rows = int(
                    _clamp(fd.get("rows", 0), 0, MAX_FILL_DOWN_ROWS, 0)
                )
                if isinstance(fd.get("exclude_columns"), list):
                    exclude_cols = [
                        str(c).upper() for c in fd["exclude_columns"][:nrows]
                    ]
            total_rows = len(rows) + fill_down_rows
            if total_rows > MAX_FILL_DOWN_ROWS:
                fill_down_rows = MAX_FILL_DOWN_ROWS - len(rows)

            # number formats: resolve header names → column letters
            numfmts: Dict[str, str] = {}
            nf = table.get("number_formats")
            if isinstance(nf, dict):
                for key, fmt in nf.items():
                    if not valid_number_format(fmt):
                        continue
                    k = str(key)
                    if re.match(r"^[A-Za-z]{1,3}$", k):
                        numfmts[k.upper()] = fmt
                    else:
                        # header-name lookup (case-insensitive)
                        for hi, h in enumerate(headers):
                            if h.strip().lower() == k.strip().lower():
                                numfmts[get_column_letter(hi + 1)] = fmt
                                break

            tnorm: Dict[str, Any] = {
                "start_cell": (
                    table["start_cell"].upper()
                    if is_valid_cell(table.get("start_cell"))
                    else "A1"
                ),
                "headers": headers,
                "rows": rows,
                "zebra": bool(table.get("zebra", True)),
                "auto_filter": bool(table.get("auto_filter", False)),
                "borders": bool(table.get("borders", True)),
                "number_formats": numfmts,
                "fill_down": (
                    {"rows": fill_down_rows, "exclude_columns": exclude_cols}
                    if fill_down_rows > 0
                    else None
                ),
            }
            if isinstance(table.get("title"), str) and table["title"].strip():
                tnorm["title"] = _sanitize_cell_text(table["title"])[:300]
            tr = table.get("total_row")
            if isinstance(tr, list) and tr:
                tr = tr[:nrows] + [None] * (nrows - len(tr[:nrows]))
                tnorm["total_row"] = [_coerce_cell_value(v) for v in tr]
            hs = table.get("header_style")
            if isinstance(hs, dict):
                tnorm["header_style"] = {
                    "fill": (
                        hs["fill"].upper() if valid_hex_color(hs.get("fill")) else NAVY
                    ),
                    "font_color": (
                        hs["font_color"].upper()
                        if valid_hex_color(hs.get("font_color"))
                        else "FFFFFF"
                    ),
                    "bold": bool(hs.get("bold", True)),
                    "font_size": _clamp(hs.get("font_size"), 6, 72, 11.0),
                }
            tables.append(tnorm)
        if tables:
            s["tables"] = tables

        # formulas
        pairs = _normalize_formula_pairs(sheet.get("formulas"))
        if pairs:
            good = []
            for cell, formula in pairs[:500]:
                if not is_valid_cell(cell) or not formula.strip():
                    continue
                f = formula.strip()
                if not f.startswith("="):
                    f = "=" + f
                good.append((cell.upper(), _sanitize_cell_text(f)))
            if good:
                s["formulas"] = good

        # charts
        charts = []
        for chart in (sheet.get("charts") or [])[:MAX_CHARTS_PER_SHEET]:
            if not isinstance(chart, dict) or chart.get("type") not in (
                "bar",
                "bar_h",
                "line",
                "area",
                "pie",
                "scatter",
            ):
                continue
            series = []
            for sr in (chart.get("series") or [])[:MAX_SERIES_PER_CHART]:
                if not isinstance(sr, dict) or not isinstance(
                    sr.get("values_range"), str
                ):
                    continue
                name = sr.get("name") if isinstance(sr.get("name"), str) else None
                series.append({"name": name, "values_range": sr["values_range"]})
            if not series:
                continue
            c: Dict[str, Any] = {
                "type": chart["type"],
                "series": series,
                "anchor": (
                    chart["anchor"].upper()
                    if is_valid_cell(chart.get("anchor"))
                    else "E2"
                ),
                "width": _clamp(chart.get("width"), 4, 40, 15.0),
                "height": _clamp(chart.get("height"), 3, 30, 9.0),
            }
            if isinstance(chart.get("title"), str) and chart["title"].strip():
                c["title"] = _sanitize_cell_text(chart["title"])[:200]
            if isinstance(chart.get("categories_range"), str):
                c["categories_range"] = chart["categories_range"]
            charts.append(c)
        if charts:
            s["charts"] = charts

        out["sheets"].append(s)

    return out


# ── LLM prompt ────────────────────────────────────────────────────────

EXCEL_SYSTEM_PROMPT = """You are a spreadsheet architect. You convert a brief into ONE strict JSON workbook specification. A deterministic converter turns that JSON into a real .xlsx file.

## OUTPUT RULES (critical)
- Output exactly ONE JSON object. No markdown fences, no comments, no prose before/after, no trailing commas.
- Follow the schema EXACTLY. Unknown keys are ignored.
- Numbers MUST be JSON numbers (42, 12.5) — never quoted strings. Text stays a string. Empty cell = null. Boolean = true/false.
- Any cell value string starting with "=" becomes a LIVE Excel formula. PREFER live formulas over pre-computed values whenever the sheet involves calculations, models, or demonstrations.
- Use standard English function names with "," separators: SUM, AVERAGE, MIN, MAX, COUNT, COUNTA, IF, ROUND, ABS, PMT, FV, PV, RATE, NPER, IFERROR, TEXT, TODAY, VLOOKUP, SUMIF, SUMPRODUCT.
- Cross-sheet references: Inputs!$B$4 or 'My Sheet'!$B$4 (quote names with spaces). ALWAYS use $-absolute references when referencing other sheets so fill-down cannot break them.

## SCHEMA
{
 "filename": "string (optional)",
 "sheets": [
  {
   "name": "string 1-31 chars, no [ ] : * ? / \\, unique",
   "tab_color": "RRGGBB (optional)",
   "freeze_panes": "A2 (optional — keeps the header row visible)",
   "column_widths": {"A": 24} (optional — only for columns that need it),
   "merged_cells": ["A1:C1"] (optional),
   "notes": "string (optional — rendered under the sheet content in small gray italic)",
   "text_blocks": [{"cell": "A1", "text": "…", "bold": false, "italic": false, "font_size": 11, "font_color": "RRGGBB", "wrap": false}],
   "tables": [
    {
     "start_cell": "A3",
     "title": "Bold heading above the header row (optional)",
     "headers": ["Month", "Payment", "Balance"],
     "rows": [[1, "=B4-C4", 250000], [2, "=B5-C5", "=E4-D5"]],
     "number_formats": {"B": "#,##0.00", "C": "0.0%"},  // keys = column letters OR header names
     "total_row": ["Total", "=SUM(B4:B363)", "=SUM(C4:C363)"],  // optional, formulas allowed; {first_row}/{last_row} placeholders replaced automatically
     "zebra": true,
     "auto_filter": false,
     "fill_down": {"rows": 356, "exclude_columns": []}  // optional: replicate the LAST row N more times, auto-shifting relative refs like Excel fill-down; numeric sequence cells (1,2,3…) continue automatically
    }
   ],
   "formulas": [{"cell": "B10", "formula": "=SUM(B2:B9)"}],
   "charts": [
    {
     "type": "bar | bar_h | line | area | pie | scatter",
     "title": "Chart title (optional)",
     "anchor": "E2",
     "width": 15, "height": 9,
     "categories_range": "Sheet1!A2:A13",
     "series": [{"name": "Revenue", "values_range": "Sheet1!B2:B13"}]
    }
   ]
  }
 ]
}

## DESIGN DEFAULTS (unless the brief overrides)
- Header row: dark navy fill 16304F, white bold text, frozen panes, zebra rows — applied automatically; you rarely need header_style.
- Number formats: money "#,##0.00" (or "#,##0.00 \\$" / "#,##0.00 €" if a currency is asked), percents "0.0%", big counts "#,##0", dates "yyyy-mm-dd".
- Percent VALUES must be decimals (0.052 = 5.2%) with a "0.0%" format — never the string "5.2%".
- Add a "total_row" with =SUM(...) under numeric tables when it makes sense. In total_row formulas and chart ranges you may write {last_row} / {first_row} — they are replaced with the table's actual first/last data row numbers.
- Dates: "2025-06-01" strings (auto-converted to real dates).
- Long tables that follow a formula pattern (schedules, projections, cumulative series): write the first 2-3 rows, then use fill_down with the remaining row count. Compute ranges/total rows accordingly (row = header_row + 1 + total_rows).
- Multi-sheet workbooks with formulas: add a final "Notes" sheet (text_blocks) documenting each sheet's purpose and the key formulas. Keep it short.
- Demo/sample data: REALISTIC and internally consistent (plausible names, prices, growth patterns, regional mix). User-provided data: use it EXACTLY as given, in the exact order.
- Web-search results / user data compilations: one clean table, all source rows preserved, an auto_filter, and notes stating the source + date. NO invented data.
- Keep every sheet on ONE clear idea. Prefer 1-3 sheets.

## EXAMPLE 1 — brief: "Compile these web-search results about AI frameworks into a spreadsheet" (results given in the user message)
{"filename":"ai_frameworks_2025.xlsx","sheets":[{"name":"Results","tables":[{"start_cell":"A1","title":"AI Frameworks — Web Search Results (June 2025)","headers":["#","Framework","Vendor","License","GitHub Stars","Key Strength"],"rows":[[1,"PyTorch","Linux Foundation","BSD-3","88.4k","Research flexibility, dynamic graphs"],[2,"TensorFlow","Google","Apache-2.0","186.2k","Production serving, TFLite/Edge"]],"number_formats":{"E":"#,##0"},"auto_filter":true}],"notes":"8 results compiled from web search on 2025-06-14. Stars rounded to the nearest hundred."}]}

## EXAMPLE 2 — brief: "Build a loan amortization schedule for $250,000 at 4.5% over 30 years with a chart"
{"filename":"loan_amortization.xlsx","sheets":[{"name":"Inputs","text_blocks":[{"cell":"B2","text":"Loan Inputs","bold":true,"font_size":14},{"cell":"A4","text":"Principal ($)"},{"cell":"B4","text":250000},{"cell":"A5","text":"Annual rate"},{"cell":"B5","text":0.045},{"cell":"A6","text":"Term (years)"},{"cell":"B6","text":30}],"tables":[{"start_cell":"A4","headers":["Value","Amount"],"rows":[["Principal",250000],["Rate",0.045],["Years",30]],"number_formats":{"B":"#,##0.00"}}]},{"name":"Schedule","freeze_panes":"A3","tables":[{"start_cell":"A1","title":"Loan Amortization — $250,000 @ 4.5% / 30 years","headers":["Month","Payment","Interest","Principal","Balance"],"rows":[[1,"=PMT($B$5/12,$B$6*12,-$B$4)","=ROUND(250000*$B$5/12,2)","=B3-C3","=250000-D3"],[2,"=B3","=ROUND(E3*$B$5/12,2)","=B4-C4","=E3-D4"]],"number_formats":{"B":"#,##0.00","C":"#,##0.00","D":"#,##0.00","E":"#,##0.00"},"fill_down":{"rows":358},"total_row":["Total","=SUM(B3:B{last_row})","=SUM(C3:C{last_row})","=SUM(D3:D{last_row})",""]}],"charts":[{"type":"line","title":"Remaining Balance","anchor":"H2","categories_range":"Schedule!A3:A{last_row}","series":[{"name":"Balance","values_range":"Schedule!E3:E{last_row}"}]}]},{"name":"Notes","text_blocks":[{"cell":"A1","text":"How this workbook works","bold":true,"font_size":13},{"cell":"A3","text":"Inputs: principal, rate and term. Change B4/B5/B6 and the whole schedule recalculates."},{"cell":"A5","text":"Schedule: 360 monthly rows. Payment uses PMT; Interest = previous balance × rate/12; Principal = Payment − Interest."},{"cell":"A7","text":"The chart on 'Schedule' plots the remaining balance over the term."}]}]}

Respond with the JSON object only."""


# ── LLM call + JSON extraction ────────────────────────────────────────


def _extract_json_object(content: str) -> Any:
    """Extract the first balanced JSON object from LLM output.

    Handles: markdown fences, leading/trailing prose, a single
    wrapper key like {"workbook": {...}}.
    Raises ValueError when no object can be extracted.
    """
    if not content or not content.strip():
        raise ValueError("empty LLM response")

    text = content.strip()
    # Strip ``` fences
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text[3:]
        text = text.rsplit("```", 1)[0]

    # Find the first '{' and parse the balanced object from there
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object found in LLM output")

    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(text[start:])
    except json.JSONDecodeError as e:
        # common fix: trailing commas
        cleaned = re.sub(r",\s*([}\]])", r"\1", text[start:])
        try:
            obj, _ = decoder.raw_decode(cleaned)
        except json.JSONDecodeError:
            raise ValueError(f"unparseable JSON from LLM: {e}") from e

    # Unwrap {"workbook": {...}} / {"spec": {...}} single-key wrappers
    if isinstance(obj, dict) and "sheets" not in obj and len(obj) == 1:
        inner = next(iter(obj.values()))
        if isinstance(inner, dict) and "sheets" in inner:
            return inner
    return obj


async def _call_llm(messages: List[dict]) -> str:
    """Single Ollama /api/chat call in JSON mode."""
    model = settings.resolve_model(settings.EXCEL_GENERATION_MODEL_ROLE)
    ollama_url = settings.OLLAMA_BASE_URL
    if not ollama_url or not model:
        raise RuntimeError("No LLM model or URL configured for Excel generation")

    timeout = float(getattr(settings, "EXCEL_GENERATION_TIMEOUT_SECONDS", 600) or 600)
    num_predict = int(getattr(settings, "EXCEL_GENERATION_MAX_TOKENS", 8192) or 8192)

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{ollama_url}/api/chat",
            json={
                "model": model,
                "messages": messages,
                "stream": False,
                "think": False,
                "format": "json",  # JSON mode — constrains output to valid JSON
                "options": {"num_predict": num_predict, "temperature": 0.2},
            },
        )
        response.raise_for_status()
        _log(response)
        data = response.json()
        _log(data)
        content = data.get("message", {}).get("content", "")

    if not content.strip():
        raise RuntimeError("LLM returned empty content for Excel generation")
    return content


async def _generate_workbook_json(brief: str, requirements: str) -> dict:
    """Specialized Excel AI call: brief → validated + normalized workbook spec.

    One repair round on validation errors (the errors are fed back to
    the LLM). Raises RuntimeError when the spec is still invalid.
    """
    user_content = f"Brief: {brief}"
    if requirements and requirements.strip():
        user_content += (
            f"\n\nAdditional requirements (MUST follow):\n{requirements.strip()}"
        )
    user_content += "\n\nRespond with the JSON workbook specification now."

    messages: List[dict] = [
        {"role": "system", "content": EXCEL_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    last_errors: List[str] = []
    last_raw = ""
    for attempt in (1, 2):
        _log("LLM call %d: brief=%r", attempt, brief[:60])
        raw = await _call_llm(messages)
        last_raw = raw
        try:
            parsed = _extract_json_object(raw)
        except ValueError as e:
            last_errors = [f"JSON parse failure: {e}"]
            parsed = None

        if parsed is not None:
            errors, warnings = validate_workbook_spec(parsed)
            if warnings:
                _log("validation warnings: %s", "; ".join(warnings[:8]))
            if not errors:
                normalized = _normalize_spec(parsed)
                _log(
                    "spec OK: %d sheets, %d cells",
                    len(normalized["sheets"]),
                    sum(
                        len(t["headers"]) * (1 + len(t["rows"]))
                        for s in normalized["sheets"]
                        for t in s.get("tables", [])
                    ),
                )
                return normalized
            last_errors = errors

        # Build the repair prompt
        _log("attempt %d invalid: %s", attempt, "; ".join(last_errors[:5]))
        messages = [
            {"role": "system", "content": EXCEL_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": user_content,
            },
            {"role": "assistant", "content": raw[:4000]},
            {
                "role": "user",
                "content": (
                    "Your previous output is INVALID. Problems:\n- "
                    + "\n- ".join(last_errors[:15])
                    + "\n\nOutput the CORRECTED JSON workbook specification only. "
                    "Follow the schema exactly; output ONE JSON object, nothing else."
                ),
            },
        ]

    raise RuntimeError(
        f"Excel workbook spec failed validation after retry: {'; '.join(last_errors[:5])} "
        f"(raw output started: {last_raw[:200]!r})"
    )


# ── Deterministic converter (openpyxl, no LLM) ────────────────────────


def _get_reports_dir() -> Path:
    """Get the reports directory under data/. Creates it if missing."""
    candidates = [
        Path("/app/data/reports"),
        Path(__file__).resolve().parent.parent.parent / "data" / "reports",
    ]
    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
            return d
        except OSError:
            continue
    fallback = Path("/app/data/reports")
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


_THIN = Side(style="thin", color=BORDER_COLOR)
_ALL_THIN = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_TOP_GOLD = Side(style="medium", color=GOLD)


class _SheetWriter:
    """Writes one sheet's spec into an openpyxl worksheet."""

    def __init__(self, ws, spec: dict, sheet_name: str):
        self.ws = ws
        self.spec = spec
        self.sheet_name = sheet_name
        self.max_row = 0
        self.max_col = 0
        self.table_last_rows: List[int] = []  # last DATA row per table
        self.table_end_rows: List[int] = []  # last row incl. total row (filters)
        # column → max content length (for auto width)
        self.col_widths: Dict[int, int] = {}

    # ── low-level writers ──

    def _track(self, row: int, col: int, text_len: int) -> None:
        self.max_row = max(self.max_row, row)
        self.max_col = max(self.max_col, col)
        self.col_widths[col] = max(self.col_widths.get(col, 0), text_len)

    def _put(self, row: int, col: int, value: Any) -> None:
        cell = self.ws.cell(row=row, column=col)
        if value is not None:
            cell.value = value
        self._track(row, col, len(str(value)) if value is not None else 0)

    def _put_styled(
        self,
        row: int,
        col: int,
        value: Any,
        *,
        font: Optional[Font] = None,
        fill: Optional[PatternFill] = None,
        border: Optional[Border] = None,
        align: Optional[Alignment] = None,
        number_format: Optional[str] = None,
    ) -> None:
        self._put(row, col, value)
        cell = self.ws.cell(row=row, column=col)
        if font:
            cell.font = font
        if fill:
            cell.fill = fill
        if border:
            cell.border = border
        if align:
            cell.alignment = align
        if number_format:
            cell.number_format = number_format

    # ── pieces ──

    def write_text_blocks(self) -> None:
        for block in self.spec.get("text_blocks", []):
            row, col = cell_to_indices(block["cell"])
            self._put_styled(
                row,
                col,
                block["text"],
                font=Font(
                    size=block["font_size"],
                    bold=block["bold"],
                    italic=block["italic"],
                    color=block["font_color"] or INK,
                ),
                align=Alignment(wrap_text=block["wrap"], vertical="center"),
                number_format=_default_date_format(block["text"]),
            )
            if block["wrap"]:
                span = 6  # merged-ish visual width for wrapped labels
                for c in range(col, col + span):
                    self._track(row, c, 0)

    def write_tables(self) -> None:
        for table in self.spec.get("tables", []):
            self._write_table(table)

    def _write_table(self, table: dict) -> None:
        start_row, start_col = cell_to_indices(table["start_cell"])
        headers: List[str] = table["headers"]
        ncols = len(headers)
        row = start_row

        # Optional title above the header row
        if table.get("title"):
            title_font = Font(size=13, bold=True, color=INK)
            self._put_styled(
                row,
                start_col,
                table["title"],
                font=title_font,
                align=Alignment(vertical="center"),
            )
            for c in range(start_col + 1, start_col + min(ncols, 4)):
                self._track(row, c, 0)
            row += 1

        header_row = row
        numfmts: Dict[str, str] = table.get("number_formats", {})
        hs = table.get("header_style") or {}
        h_fill = PatternFill(
            fill_type="solid",
            start_color=hs.get("fill", NAVY),
            end_color=hs.get("fill", NAVY),
        )
        h_font = Font(
            bold=hs.get("bold", True),
            size=hs.get("font_size", 11.0),
            color=hs.get("font_color", "FFFFFF"),
        )
        h_align = Alignment(vertical="center", wrap_text=True)
        for ci, header in enumerate(headers):
            self._put_styled(
                header_row,
                start_col + ci,
                header,
                font=h_font,
                fill=h_fill,
                border=_ALL_THIN,
                align=h_align,
                number_format=numfmts.get(get_column_letter(start_col + ci)),
            )
        row += 1
        first_data_row = row

        # Data rows (+ fill_down expansion)
        rows: List[list] = table["rows"]
        n_explicit = len(rows)
        fd = table.get("fill_down")
        if fd and fd.get("rows"):
            rows = rows + [None] * fd["rows"]  # type: ignore[list-item]
        exclude_cols = set(fd.get("exclude_columns") or []) if fd else set()

        zebra = table["zebra"]
        borders_on = table["borders"]
        data_font = Font(color=INK)
        zebra_fill = PatternFill(fill_type="solid", start_color=ZEBRA, end_color=ZEBRA)

        # The fill-down pattern row = last EXPLICIT row (before the
        # placeholder entries appended above).
        origin_row = first_data_row + n_explicit - 1 if n_explicit else None
        seq_diffs: Dict[int, Optional[float]] = {}

        for ri, data_row in enumerate(rows):
            r = first_data_row + ri
            if data_row is None:  # fill_down row
                data_row = self._fill_down_row(
                    table, r, origin_row, exclude_cols, seq_diffs
                )
            is_zebra = zebra and (ri % 2 == 1)
            for ci, value in enumerate(data_row):
                col = start_col + ci
                col_letter = get_column_letter(col)
                nf = numfmts.get(col_letter)
                nf = nf or _default_date_format(value)
                self._put_styled(
                    r,
                    col,
                    value,
                    font=data_font,
                    fill=zebra_fill if is_zebra else None,
                    border=_ALL_THIN if borders_on else None,
                    align=Alignment(vertical="center"),
                    number_format=nf,
                )
        last_data_row = first_data_row + len(rows) - 1 if rows else first_data_row - 1
        self.table_last_rows.append(last_data_row)
        table_end_row = last_data_row

        # Total row
        total_row = table.get("total_row")
        if total_row:
            r = last_data_row + 1
            t_font = Font(bold=True, color=INK)
            t_fill = PatternFill(
                fill_type="solid", start_color=CALLOUT_BG, end_color=CALLOUT_BG
            )
            t_border = Border(left=_THIN, right=_THIN, top=_TOP_GOLD, bottom=_THIN)
            for ci, value in enumerate(total_row):
                col = start_col + ci
                col_letter = get_column_letter(col)
                nf = numfmts.get(col_letter)
                if isinstance(value, str):
                    value = self._expand_row_placeholders(
                        value, first_data_row, last_data_row
                    )
                nf = nf or _default_date_format(value)
                self._put_styled(
                    r,
                    col,
                    value,
                    font=t_font,
                    fill=t_fill,
                    border=t_border,
                    align=Alignment(vertical="center"),
                    number_format=nf,
                )
            table_end_row = r

        self.table_end_rows.append(table_end_row)

        # Auto filter
        if table.get("auto_filter") and rows:
            end_row = table_end_row
            end_col = get_column_letter(start_col + ncols - 1)
            start_letter = get_column_letter(start_col)
            self.ws.auto_filter.ref = f"{start_letter}{header_row}:{end_col}{end_row}"

    def _fill_down_row(
        self,
        table: dict,
        row: int,
        origin_row: Optional[int],
        exclude_cols: set,
        seq_diffs: Dict[int, Optional[float]],
    ) -> list:
        """Replicate the table's last explicit row at `row`.

        - formula cells are shifted with openpyxl's Translator
          (exactly Excel fill-down semantics: relative refs shift,
          absolute $refs stay)
        - numeric cells whose last two explicit values form a sequence
          continue it (1, 2 → 3, 4, …)
        - other literals repeat; exclude_columns stay empty
        """
        rows: List[list] = table["rows"]  # explicit rows only
        if not rows or origin_row is None:
            return [None] * len(table["headers"])
        start_col = self._table_start_col(table)
        pattern = rows[-1]
        prev = rows[-2] if len(rows) >= 2 else None

        out: List[Any] = []
        for ci in range(len(table["headers"])):
            col_letter = get_column_letter(start_col + ci)
            if col_letter in exclude_cols:
                out.append(None)
                continue
            value = pattern[ci] if ci < len(pattern) else None

            if isinstance(value, str) and value.startswith("="):
                origin = f"{col_letter}{origin_row}"
                target = f"{col_letter}{row}"
                try:
                    shifted = Translator(value, origin=origin).translate_formula(target)
                except Exception:
                    shifted = value
                out.append(shifted)
                continue

            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if ci not in seq_diffs:
                    diff: Optional[float] = None
                    if (
                        prev is not None
                        and ci < len(prev)
                        and isinstance(prev[ci], (int, float))
                        and not isinstance(prev[ci], bool)
                    ):
                        diff = value - prev[ci]
                    seq_diffs[ci] = diff
                diff = seq_diffs.get(ci)
                if diff is not None and diff != 0:
                    steps = row - origin_row
                    out.append(
                        int(value + diff * steps)
                        if isinstance(value, int) and float(diff).is_integer()
                        else value + diff * steps
                    )
                    continue
            out.append(value)
        return out

    def _table_start_col(self, table: dict) -> int:
        _, start_col = cell_to_indices(table["start_cell"])
        return start_col

    @staticmethod
    def _expand_row_placeholders(value: str, first_row: int, last_row: int) -> str:
        if "{" not in value:
            return value
        return value.replace("{first_row}", str(first_row)).replace(
            "{last_row}", str(last_row)
        )

    def write_formulas(self) -> None:
        for cell_ref, formula in self.spec.get("formulas", []):
            row, col = cell_to_indices(cell_ref)
            # placeholder support relative to the last table on the sheet
            first_row = 1
            last_row = (
                self.table_last_rows[-1] if self.table_last_rows else self.max_row
            )
            formula = self._expand_row_placeholders(formula, first_row, last_row)
            self._put(row, col, formula)

    def write_notes(self) -> None:
        notes = self.spec.get("notes")
        if not notes:
            return
        row = max(self.max_row + 2, 1)
        self._put_styled(
            row,
            1,
            f"Notes: {notes}",
            font=Font(size=10, italic=True, color=MUTED),
            align=Alignment(vertical="top"),
        )

    def write_merged_cells(self) -> None:
        for rng in self.spec.get("merged_cells", []):
            try:
                self.ws.merge_cells(rng)
            except Exception:
                continue  # overlapping merges are cosmetic — skip

    def write_charts(self) -> None:
        for chart_spec in self.spec.get("charts", []):
            try:
                self._write_chart(chart_spec)
            except Exception as e:
                _log("chart skipped (non-fatal): %s", e)

    def _resolve_ws(self, sheet_name: str):
        """Resolve a range's sheet name to the actual worksheet."""
        wb = self.ws.parent
        if sheet_name == self.sheet_name:
            return self.ws
        if sheet_name in wb.sheetnames:
            return wb[sheet_name]
        for title in wb.sheetnames:  # case-insensitive fallback
            if title.lower() == sheet_name.lower():
                return wb[title]
        raise ValueError(f"chart references unknown sheet {sheet_name!r}")

    def _parse_spec_range(self, range_str: str):
        """Parse a range string, expanding {last_row} placeholders."""
        last_row = self.table_last_rows[-1] if self.table_last_rows else self.max_row
        first_row = 1
        expanded = range_str.replace("{last_row}", str(max(last_row, 1))).replace(
            "{first_row}", str(max(first_row, 1))
        )
        return parse_range(expanded, self.sheet_name)

    def _write_chart(self, chart_spec: dict) -> None:
        ctype = chart_spec["type"]
        series_specs = chart_spec["series"]
        # A pie chart shows ONE series (the value distribution); extra
        # series would silently hide behind the first in Excel.
        if ctype == "pie":
            series_specs = series_specs[:1]

        categories_ref = None
        if chart_spec.get("categories_range"):
            sheet, r1, r2, c1, c2 = self._parse_spec_range(
                chart_spec["categories_range"]
            )
            ws = self._resolve_ws(sheet)
            categories_ref = Reference(
                ws, min_col=c1, min_row=r1, max_col=c2, max_row=r2
            )

        if ctype == "pie":
            chart = PieChart()
        elif ctype == "line":
            chart = LineChart()
        elif ctype == "area":
            chart = AreaChart()
        elif ctype == "scatter":
            chart = ScatterChart()
        else:
            chart = BarChart()
            chart.type = "bar" if ctype == "bar_h" else "col"
        chart.style = 10
        chart.width = chart_spec["width"]
        chart.height = chart_spec["height"]

        color_idx = 0
        for s_i, s_spec in enumerate(series_specs):
            sheet, r1, r2, c1, c2 = self._parse_spec_range(s_spec["values_range"])
            ws = self._resolve_ws(sheet)
            values_ref = Reference(ws, min_col=c1, min_row=r1, max_col=c2, max_row=r2)
            name = s_spec.get("name") or self._header_above(ws, c1, r1)

            if ctype == "scatter":
                series = Series(
                    values_ref,
                    categories_ref if categories_ref is not None else values_ref,
                    title=name or f"Series {s_i + 1}",
                )
                chart.series.append(series)
            else:
                if s_i == 0:
                    chart.add_data(values_ref, titles_from_data=False)
                else:
                    chart.add_data(values_ref, titles_from_data=False)
                series = chart.series[-1]
                if name:
                    series.tx = None  # reset; set below via SeriesLabel
                    from openpyxl.chart.series import SeriesLabel

                    series.tx = SeriesLabel(v=str(name))
                if categories_ref is not None:
                    chart.set_categories(categories_ref)

            color = CHART_SERIES_COLORS[color_idx % len(CHART_SERIES_COLORS)]
            color_idx += 1
            if ctype == "line":
                series.graphicalProperties.line.solidFill = color
                series.graphicalProperties.line.width = 28575  # ≈ 2.25pt
            elif ctype == "pie":
                self._color_pie_slices(series, r2 - r1 + 1)
            else:
                series.graphicalProperties.solidFill = color

        if ctype == "pie":
            chart.dataLabels = DataLabelList()
            chart.dataLabels.showPercent = True
        if chart_spec.get("title"):
            chart.title = chart_spec["title"]
        # PieChart has no axes; make sure axes are visible on the others
        # (openpyxl charts otherwise render axis-less in some viewers).
        if not isinstance(chart, PieChart):
            chart.x_axis.delete = False
            chart.y_axis.delete = False
        self.ws.add_chart(chart, chart_spec["anchor"])

    def _color_pie_slices(self, series, count: int) -> None:
        count = max(count, 1)
        pts = []
        for i in range(min(count, 20)):
            pt = DataPoint(idx=i)
            pt.graphicalProperties.solidFill = CHART_SERIES_COLORS[
                i % len(CHART_SERIES_COLORS)
            ]
            pts.append(pt)
        series.data_points = pts

    @staticmethod
    def _header_above(ws, col: int, row: int) -> Optional[str]:
        """Use the cell above a data range as the series title."""
        if row <= 1:
            return None
        try:
            value = ws.cell(row=row - 1, column=col).value
        except Exception:
            return None
        if value is None or isinstance(value, str) and value.startswith("="):
            return None
        return str(value) if value is not None else None

    # ── finishing touches ──

    def apply_layout(self) -> None:
        spec = self.spec

        # Auto column widths from content, then explicit overrides
        for col, max_len in self.col_widths.items():
            width = max(9.0, min(55.0, 4 + max_len * 1.05))
            letter = get_column_letter(col)
            self.ws.column_dimensions[letter].width = width
        for letter, width in (spec.get("column_widths") or {}).items():
            try:
                self.ws.column_dimensions[letter.upper()].width = width
            except Exception:
                continue

        if spec.get("freeze_panes"):
            self.ws.freeze_panes = spec["freeze_panes"]
        elif self._has_header_row():
            # sensible default: freeze below the first table's header
            tables = spec.get("tables") or []
            if tables:
                try:
                    start_row, _ = cell_to_indices(tables[0]["start_cell"])
                    if tables[0].get("title"):
                        start_row += 1
                    self.ws.freeze_panes = f"A{start_row + 1}"
                except ValueError:
                    pass

        if spec.get("tab_color"):
            self.ws.sheet_properties.tabColor = spec["tab_color"]

    def _has_header_row(self) -> bool:
        return bool(self.spec.get("tables"))


def _build_xlsx(spec: dict, output_path: Path) -> None:
    """Deterministic JSON spec → .xlsx (pure openpyxl, no LLM)."""
    wb = Workbook()
    default_ws = wb.active
    wb.remove(default_ws)

    for sheet_spec in spec["sheets"]:
        ws = wb.create_sheet(title=sheet_spec["name"])
        writer = _SheetWriter(ws, sheet_spec, sheet_spec["name"])
        writer.write_tables()  # tables first (structural backbone)
        writer.write_text_blocks()  # labels/headings
        writer.write_formulas()  # override anything beneath
        writer.write_charts()  # floating objects
        writer.write_notes()  # documentation under content
        writer.write_merged_cells()
        writer.apply_layout()

    if wb.sheetnames:
        wb.active = 0
    wb.save(str(output_path))

    # Round-trip sanity check — a file openpyxl can't re-read would be
    # a corrupt deliverable.
    probe = load_workbook(str(output_path))
    if len(probe.sheetnames) != len(spec["sheets"]):
        raise RuntimeError("workbook round-trip failed: sheet count mismatch")
    probe.close()


# ── Public API ────────────────────────────────────────────────────────


async def generate_spreadsheet(
    brief: str,
    requirements: str = "",
) -> dict:
    """Generate an Excel workbook from a brief.

    Args:
        brief: What the spreadsheet should contain / achieve. Can be
               very short ("turn this data into an Excel file") or
               complex ("full loan amortization schedule with charts").
        requirements: Optional extra constraints (sheet count, formulas,
               specific columns, currency…).

    Returns:
        Dict with: type, format, filename, file_path, download_url,
        report_id, created_at, sheet_count — the same deliverable
        contract as generate_report / generate_presentation.
    """
    if not brief or not brief.strip():
        raise ValueError("brief cannot be empty")

    report_id = str(uuid.uuid4())
    reports_dir = _get_reports_dir()

    # 1. Specialized Excel AI call → validated + normalized JSON spec
    spec = await _generate_workbook_json(brief, requirements)

    # 2. Deterministic conversion (sync/CPU-bound → thread pool)
    output_path = reports_dir / f"{report_id}.xlsx"
    await asyncio.to_thread(_build_xlsx, spec, output_path)

    if not output_path.exists():
        raise RuntimeError(f"Excel file was not created: {output_path}")

    file_size = output_path.stat().st_size
    _log(
        "workbook saved: %s (%d bytes, %d sheets)",
        output_path.name,
        file_size,
        len(spec["sheets"]),
    )

    # 3. Download filename: LLM-provided name > brief-derived
    filename = spec.get("filename")
    if not filename:
        safe = re.sub(r"[^\w\s-]", "", brief)[:50].strip()
        safe = re.sub(r"[\s_-]+", "_", safe) or "spreadsheet"
        filename = f"{safe}.xlsx"

    rel_path = f"reports/{report_id}.xlsx"

    return {
        "type": "excel",
        "format": "xlsx",
        "filename": filename,
        "file_path": rel_path,
        "download_url": f"/api/reports/{report_id}/download",
        "report_id": report_id,
        "created_at": int(time.time()),
        "sheet_count": len(spec["sheets"]),
    }
