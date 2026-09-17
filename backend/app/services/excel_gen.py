"""
Excel generation service — LLM emits a strict JSON workbook spec, a
deterministic openpyxl converter turns it into a real .xlsx file.

Pipeline:
  0. Deterministic template routing — a small classifier call maps the
     brief to a built-in pattern (loan amortization, invoice, budget
     planner, investment portfolio, habit tracker) and extracts scalar
     parameters.
     Matched requests are built by the patterns package
     (app/services/patterns/ — one module per pattern, discovered
     dynamically like agent tools): every formula reference is
     computed from the actual layout rows in code, so off-by-N model
     row-math is impossible (the failure class behind silently wrong
     amortization schedules). No match → the AI path below, unchanged.
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
     defaulted colors, capped sizes, currency strings like "$4.50"
     coerced to numbers, text blocks colliding with a table promoted
     to its title or dropped) so the converter never sees an invalid
     spec.
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
  no_freeze     optional true — opt out of the default
                freeze-below-first-header; for sheets meant to be
                scrolled freely (budgets, planners).
  hidden        optional true — hide the sheet (helper/calc sheets the
                user never edits; unhidable from the tab context menu).
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
  data_validation     optional array of DATA_VALIDATION (in-cell
                      dropdown lists — habit marks, Yes/No flags…).
  conditional_formats optional array of CONDITIONAL_FORMATS entry
                      (green done / red missed / highlight-today…).
  protect       optional true, or {"unlocked_ranges": ["C5:L66", ...],
                "password": optional} — locks every cell on the sheet
                EXCEPT the unlocked ranges (and the password, when
                given) so live formulas can't be overwritten by
                accident. Selection stays allowed; cosmetic cell
                formatting stays allowed.

TABLE object:
  start_cell      default "A1" — the table's anchor row. WITHOUT a
                  title: the header row is ON start_cell's row and
                  the first data row is one below. WITH a title: the
                  title is ON start_cell's row, the header row one
                  below, the first data row two below.
  title           optional string — bold heading rendered ON
                  start_cell's row; pushes header + data down 1.
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
  alignments      optional map {column letter → "left" | "center" |
                  "right"} — horizontal alignment for that column's
                  header and data cells (✓ marks, scores…).
  fill_down       optional {"rows": N, "exclude_columns": ["A", ...]} —
                  replicates the LAST data row N more times, shifting
                  relative formula refs exactly like Excel fill-down
                  (openpyxl Translator). Numeric cells whose last two
                  values form a sequence continue it (1, 2 → 3, 4, …);
                  other literals repeat. Used for amortization
                  schedules, cumulative series, projections.

DATA_VALIDATION object (one dropdown list):
  range          required "C5:L66" (this sheet).
  values         required non-empty array of scalars — the dropdown
                 entries (omit when source_range is given). Commas/
                 quotes are not allowed inside values (Excel's list
                 syntax); the joined list is capped at Excel's
                 255-char limit.
  source_range   optional "Categories!$A$5:$A$24" — a live RANGE the
                 dropdown reads its entries from (sheet-qualified, no
                 leading "="). The list then grows with the source
                 sheet: rows added later show up in the dropdown
                 automatically — use it for user-extensible pick
                 lists (categories, locations). Takes precedence
                 over values; no 255-char limit applies.
  allow_blank    default true — clearing a cell stays legal.
  prompt_title / prompt     optional input tooltip strings.
  error_title / error       optional rejection message.
  error_style    "stop" (default) | "warning" | "information".

CONDITIONAL_FORMATS entry (rules apply in array order — earlier rules
have higher priority; set stop_if_true to keep later rules from also
painting a matching cell):
  range          required "C5:L66" (this sheet). Formula rules anchor
                 relative refs to the range's TOP-LEFT cell.
  rules          required non-empty array (max 5) of:
    type         "cell_is" | "formula"
    operator     cell_is only: equal | not_equal | greater_than |
                 less_than | greater_than_or_equal |
                 less_than_or_equal | between | not_between
    value        the comparison value (scalar, or [lo, hi] for
                 between); strings compare as text.
    formula      formula-type only — the expression, e.g.
                 "$A5=TODAY()" (no leading "=").
    fill         optional "RRGGBB" background.
    font_color   optional "RRGGBB" text color.
    bold         default false.
    stop_if_true default false.

TEXT_BLOCK object:
  cell        required cell ref. NEVER inside a table's rectangle
              (the table + its title row) — colliding blocks are
              auto-removed to protect the table. A title-like block
              (bold or font_size ≥ 13) sitting exactly on a table's
              start_cell is promoted to that table's title instead.
  text        required string.
  bold / italic   default false.
  font_size   default 11 (clamped 6..72).
  font_color  optional "RRGGBB".
  wrap        default false.
  number_format optional string — Excel number format for this cell
              ("#,##0.00", "0.0%"…). Money/percent values shown
              OUTSIDE tables (labels, input cells) render formatted.

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
  value_numfmt      optional Excel number format applied to the value
                    axis AND the value data labels — "0%" turns a
                    0..1 fraction series into a percentage chart.

Value typing rules for the LLM:
  numbers as JSON numbers, text as strings, booleans as JSON
  booleans, empty as null. Dates as "YYYY-MM-DD" strings — the
  converter turns them into real dates with a "yyyy-mm-dd" format.
  Rescue path for models that quote money anyway: pure currency
  strings ("$4.50", "1,234.56 €") are coerced to numbers so SUM()
  totals keep working.

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
from openpyxl.formatting.rule import CellIsRule, FormulaRule
from openpyxl.formula.translate import Translator
from openpyxl.styles import Alignment, Border, Font, PatternFill, Protection, Side
from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.properties import PageSetupProperties

from app.config import settings
from app.services import model_prefs
from app.services import providers
from app.services.patterns import PATTERN_BUILDERS, PATTERN_FILLABLE, PATTERN_KEYWORDS
from app.prompts import get_prompt

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
MAX_VALIDATIONS_PER_SHEET = 10
MAX_CF_ENTRIES_PER_SHEET = 20
MAX_CF_RULES_PER_ENTRY = 5
MAX_DV_VALUES = 10

# Range-source dropdown refs: "Categories!$A$5:$A$24" (sheet part
# optional → same-sheet source). Quoted sheet names may contain
# anything; unquoted ones stick to the safe charset.
_SOURCE_RANGE_RE = re.compile(
    r"^(?:'([^']+)'!|([A-Za-z0-9_][A-Za-z0-9_.\- ]*)!)?"
    r"\$?[A-Za-z]{1,3}\$?\d+:\$?[A-Za-z]{1,3}\$?\d+$"
)

# spec operator → openpyxl CellIsRule operator
_CF_OPERATORS = {
    "equal": "equal",
    "not_equal": "notEqual",
    "greater_than": "greaterThan",
    "less_than": "lessThan",
    "greater_than_or_equal": "greaterThanOrEqual",
    "less_than_or_equal": "lessThanOrEqual",
    "between": "between",
    "not_between": "notBetween",
}

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
# European decimal comma — conservative: 1-3 digits before the comma,
# 1-2 after ("1,9", "3,50"). "1,234" (3 digits after) is deliberately
# excluded: it could be an English thousands number.
_DECIMAL_COMMA_RE = re.compile(r"^-?\d{1,3},\d{1,2}$")
# Currency amounts — REQUIRES a currency symbol so plain leading-zero
# strings ("007", zip codes) and ambiguous bare comma-groups ("1,234"
# could be an English thousands number OR a European decimal) stay text.
# Branches: leading symbol "$4.50" / trailing symbol "4.50 €" (the
# trailing branch also covers comma groups: "1,234.56 €").
_CURRENCY_STR_RE = re.compile(
    r"^(?:[€$£¥]\s?(-?\d{1,3}(?:,\d{3})*(?:\.\d+)?)"
    r"|(-?\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s?[€$£¥])$"
)
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

    # Overlap detection (same sheet, intersecting rectangles)
    for si, table_list in tables_by_sheet.items():
        sheet = spec["sheets"][si]
        rects = []
        for t in table_list:
            rect = _table_rect(t)
            if rect is not None:
                rects.append((rect, t.get("start_cell", "A1")))
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                (r1, c1, r1e, c1e), s1 = rects[i]
                (r2, c2, r2e, c2e), s2 = rects[j]
                if not (r1e < r2 or r2e < r1 or c1e < c2 or c2e < c1):
                    errors.append(f"sheets[{si}]: tables at {s1} and {s2} overlap")

        # text_block ↔ table overlap: small models LOVE placing heading
        # labels exactly where the table starts. The converter writes
        # tables first and text blocks after, so a colliding block would
        # silently clobber the header row / data. Warn here; the
        # normalizer auto-fixes (title promotion or drop).
        blocks = sheet.get("text_blocks") or []
        if isinstance(blocks, list) and rects:
            for bi, block in enumerate(blocks):
                if not isinstance(block, dict) or not is_valid_cell(block.get("cell")):
                    continue
                try:
                    br, bc = cell_to_indices(block["cell"])
                except ValueError:
                    continue
                for (r1, c1, r1e, c1e), _s in rects:
                    if r1 <= br <= r1e and c1 <= bc <= c1e:
                        warnings.append(
                            f"sheets[{si}].text_blocks[{bi}]: cell "
                            f"{block['cell']!r} overlaps the table at "
                            f"{_s!r} — "
                            + (
                                "promoted to its title"
                                if block["cell"].upper() == str(_s).upper()
                                and _looks_like_title(block)
                                else "dropped"
                            )
                        )
                        break

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

    if "no_freeze" in sheet and sheet["no_freeze"] is not None:
        if not isinstance(sheet["no_freeze"], bool):
            warnings.append(f"{ctx}: no_freeze must be true/false — ignored")

    if "hidden" in sheet and sheet["hidden"] is not None:
        if not isinstance(sheet["hidden"], bool):
            warnings.append(f"{ctx}: hidden must be true/false — ignored")

    dvs = sheet.get("data_validation")
    if dvs is not None:
        if not isinstance(dvs, list):
            errors.append(f"{ctx}: 'data_validation' must be an array")
        else:
            if len(dvs) > MAX_VALIDATIONS_PER_SHEET:
                warnings.append(
                    f"{ctx}: {len(dvs)} validations > "
                    f"{MAX_VALIDATIONS_PER_SHEET} — truncated"
                )
            for di, dv in enumerate(dvs[:MAX_VALIDATIONS_PER_SHEET]):
                errors.extend(
                    _validate_data_validation(f"{ctx}.data_validation[{di}]", dv)
                )

    cfs = sheet.get("conditional_formats")
    if cfs is not None:
        if not isinstance(cfs, list):
            errors.append(f"{ctx}: 'conditional_formats' must be an array")
        else:
            if len(cfs) > MAX_CF_ENTRIES_PER_SHEET:
                warnings.append(
                    f"{ctx}: {len(cfs)} conditional_formats > "
                    f"{MAX_CF_ENTRIES_PER_SHEET} — truncated"
                )
            for fi, cf in enumerate(cfs[:MAX_CF_ENTRIES_PER_SHEET]):
                errors.extend(
                    _validate_conditional_format(
                        f"{ctx}.conditional_formats[{fi}]",
                        cf,
                        sheet.get("name", "Sheet"),
                    )
                )

    prot = sheet.get("protect")
    if prot is not None and prot is not True:
        if not isinstance(prot, dict):
            warnings.append(f"{ctx}: protect must be true or an object — ignored")
        else:
            ur = prot.get("unlocked_ranges")
            if ur is not None:
                if not isinstance(ur, list):
                    errors.append(f"{ctx}: protect.unlocked_ranges must be an array")
                else:
                    for rng in ur:
                        try:
                            parse_range(str(rng), sheet.get("name", "Sheet"))
                        except ValueError:
                            errors.append(
                                f"{ctx}: invalid protect.unlocked_range {rng!r}"
                            )
            if (
                "password" in prot
                and prot["password"] is not None
                and not isinstance(prot["password"], str)
            ):
                warnings.append(f"{ctx}: protect.password must be a string — ignored")

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
                    f"{ctx}: {len(text_blocks)} text_blocks > "
                    f"{MAX_TEXT_BLOCKS_PER_SHEET} — truncated"
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
                if (
                    "number_format" in block
                    and block["number_format"] is not None
                    and not valid_number_format(block["number_format"])
                ):
                    warnings.append(f"{bctx}: invalid number_format — ignored")

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

    # Latent off-by-N detection: a table's data/total formulas that
    # reference cells ABOVE the table's header row, inside the table's
    # column band, that NOTHING on this sheet populates. Those cells
    # render empty (a table title only fills its anchor cell), so the
    # formulas silently read 0 — the signature of model row-math that
    # doesn't match the rendered layout (e.g. an amortization interest
    # chain reading the balance three rows back, or a starting balance
    # referencing the empty title row → negative-balance spiral).
    if table_list:
        pairs = _normalize_formula_pairs(sheet.get("formulas")) or []
        populated = set()
        for block in text_blocks or []:
            if isinstance(block, dict) and is_valid_cell(block.get("cell")):
                populated.add(str(block["cell"]).upper())
        for cell, _formula in pairs:
            populated.add(str(cell).upper())
        for table in table_list:
            rect = _table_rect(table)
            if rect is None:
                continue
            r1, c1, r2, c2 = rect
            start = table.get("start_cell") or "A1"
            if table.get("title") and isinstance(start, str) and is_valid_cell(start):
                try:
                    sr, sc = cell_to_indices(start)
                except (ValueError, TypeError):
                    sr = sc = None
                if sr is not None:
                    # the title row holds text only in its anchor column
                    populated.add(f"{get_column_letter(sc)}{sr}")
            for rr in range(r1, r2 + 1):
                for cc in range(c1, c2 + 1):
                    populated.add(f"{get_column_letter(cc)}{rr}")

        for ti, table in enumerate(table_list):
            geo = _table_geometry(table)
            if geo is None:
                continue
            header, _first, _last, clo, chi = geo
            formula_texts: List[str] = []
            for row in (table.get("rows") or [])[:MAX_ROWS_PER_TABLE]:
                if isinstance(row, list):
                    formula_texts.extend(
                        v
                        for v in row
                        if isinstance(v, str) and v.lstrip().startswith("=")
                    )
            for v in table.get("total_row") or []:
                if isinstance(v, str) and v.lstrip().startswith("="):
                    formula_texts.append(v)
            if not formula_texts:
                continue
            bad_refs = set()
            for formula in formula_texts:
                for m in _FORMULA_RANGE_RE.finditer(formula):
                    full = m.group(0)
                    if "!" in full:  # cross-sheet ref — resolved elsewhere
                        continue
                    c1s, r1s, c2s, r2s = m.group(1), m.group(2), m.group(3), m.group(4)
                    for col_s, row_s in ((c1s, r1s), (c2s, r2s)):
                        if not col_s or not row_s:
                            continue
                        try:
                            ci = column_index_from_string(
                                col_s.replace("$", "").upper()
                            )
                        except ValueError:
                            continue
                        ri = int(row_s)
                        if ri < header and clo <= ci <= chi:
                            ref = f"{col_s.replace('$', '').upper()}{ri}"
                            if ref not in populated:
                                bad_refs.add(ref)
            if bad_refs:
                warnings.append(
                    f"{ctx}.tables[{ti}]: formulas reference cell(s) "
                    f"{', '.join(sorted(bad_refs)[:6])} above the table's "
                    f"header row {header} that nothing populates — they "
                    "render EMPTY. Likely model row-math off by N rows "
                    "(running-balance chain reading the wrong row)."
                )

    return errors, warnings, table_list


def _validate_table(ctx: str, table: dict, sheet_name: str) -> List[str]:
    errors: List[str] = []

    start = table.get("start_cell", "A1")
    if start is None:
        start = "A1"
    if not is_valid_cell(start):
        errors.append(f"{ctx}: invalid start_cell {start!r}")

    alignments = table.get("alignments")
    if alignments is not None:
        if not isinstance(alignments, dict):
            errors.append(f"{ctx}: 'alignments' must be an object")
        else:
            for col, align in alignments.items():
                if not re.match(r"^[A-Za-z]{1,3}$", str(col)):
                    errors.append(
                        f"{ctx}: alignments key {col!r} is not a column letter"
                    )
                if align not in ("left", "center", "right"):
                    errors.append(
                        f"{ctx}: alignments[{col!r}] must be left/center/right"
                    )

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


def _validate_data_validation(ctx: str, dv: Any) -> List[str]:
    """Validate one DATA_VALIDATION entry (dropdown list)."""
    errors: List[str] = []
    if not isinstance(dv, dict):
        return [f"{ctx}: must be an object"]

    rng = dv.get("range")
    if not isinstance(rng, str):
        errors.append(f"{ctx}: 'range' must be a string")
    else:
        try:
            parse_range(rng, "Sheet")
        except ValueError:
            errors.append(f"{ctx}: invalid range {rng!r}")

    values = dv.get("values")
    source_range = dv.get("source_range")
    has_source = isinstance(source_range, str) and bool(source_range.strip())
    has_values = isinstance(values, list) and bool(values)

    if has_source:
        if has_values:
            errors.append(f"{ctx}: set either 'values' or 'source_range', not both")
        ref = source_range.strip().lstrip("=")
        if not _SOURCE_RANGE_RE.match(ref):
            errors.append(
                f"{ctx}: invalid source_range {source_range!r} — expected a "
                'range reference like "Categories!$A$5:$A$24"'
            )
    elif not has_values:
        errors.append(
            f"{ctx}: 'values' must be a non-empty array (or provide a "
            "'source_range' range reference)"
        )
    else:
        joined = ",".join(str(v) for v in values)
        if len(joined) > 250:
            errors.append(f"{ctx}: values too long for an Excel list (>255)")
        for vi, v in enumerate(values):
            if isinstance(v, (dict, list)):
                errors.append(f"{ctx}.values[{vi}]: must be a scalar")
            elif isinstance(v, str) and ("," in v or '"' in v):
                errors.append(
                    f"{ctx}.values[{vi}]: commas/quotes are not allowed "
                    "inside list values"
                )

    style = dv.get("error_style")
    if style is not None and style not in ("stop", "warning", "information"):
        errors.append(f"{ctx}: error_style must be stop/warning/information")

    return errors


def _validate_conditional_format(ctx: str, cf: Any, sheet_name: str) -> List[str]:
    """Validate one CONDITIONAL_FORMATS entry."""
    errors: List[str] = []
    if not isinstance(cf, dict):
        return [f"{ctx}: must be an object"]

    rng = cf.get("range")
    if not isinstance(rng, str):
        errors.append(f"{ctx}: 'range' must be a string")
    else:
        try:
            parse_range(rng, sheet_name)
        except ValueError:
            errors.append(f"{ctx}: invalid range {rng!r}")

    rules = cf.get("rules")
    if not isinstance(rules, list) or not rules:
        errors.append(f"{ctx}: 'rules' must be a non-empty array")
        return errors
    if len(rules) > MAX_CF_RULES_PER_ENTRY:
        errors.append(f"{ctx}: {len(rules)} rules > {MAX_CF_RULES_PER_ENTRY}")

    for ri, rule in enumerate(rules[:MAX_CF_RULES_PER_ENTRY]):
        rctx = f"{ctx}.rules[{ri}]"
        if not isinstance(rule, dict):
            errors.append(f"{rctx}: must be an object")
            continue
        rtype = rule.get("type")
        if rtype not in ("cell_is", "formula"):
            errors.append(f"{rctx}: type must be cell_is or formula")
            continue
        if rtype == "cell_is":
            op = rule.get("operator", "equal")
            if op not in _CF_OPERATORS:
                errors.append(f"{rctx}: unknown operator {op!r}")
                continue
            value = rule.get("value")
            if op in ("between", "not_between"):
                if not isinstance(value, list) or len(value) < 2:
                    errors.append(f"{rctx}: between needs a [lo, hi] value")
            elif value is None or isinstance(value, (dict, list)):
                errors.append(f"{rctx}: 'value' must be a scalar")
        else:  # formula
            formula = rule.get("formula", rule.get("value"))
            if not isinstance(formula, str) or not formula.strip():
                errors.append(f"{rctx}: formula rules need a 'formula' string")
        # invalid fill/font_color are cosmetic — silently dropped during
        # normalization rather than erroring here

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


def _table_rect(table: dict) -> Optional[Tuple[int, int, int, int]]:
    """Rectangle a table occupies: (start_row, start_col, end_row, end_col).

    Includes the optional title row (above the header), header + data
    rows, fill_down extension and the total row — every cell the
    converter will write for this table. Returns None when the table's
    geometry can't be parsed (already reported as a validation error).
    """
    start = table.get("start_cell") or "A1"
    try:
        if not isinstance(start, str) or not is_valid_cell(start):
            return None
        r, c = cell_to_indices(start)
    except (ValueError, TypeError):
        return None
    ncols = max(len(table.get("headers") or []), 1)
    offset = 1 if table.get("title") else 0
    nrows = 1 + len(table.get("rows") or []) + (1 if table.get("total_row") else 0)
    fd = table.get("fill_down") or {}
    nrows += min(_as_int(fd.get("rows"), 0), MAX_FILL_DOWN_ROWS)
    return (r + offset, c, r + offset + nrows - 1, c + ncols - 1)


def _looks_like_title(block: dict) -> bool:
    """Heuristic: does a text block read like a table heading?

    Title-like blocks (bold, or font_size ≥ 13, non-empty string) that
    sit exactly on a table's start cell are promoted to the table's
    title instead of being dropped as colliding content.
    """
    text = block.get("text")
    if not isinstance(text, str) or not text.strip():
        return False
    return bool(block.get("bold")) or _as_int(block.get("font_size"), 11) >= 13


def _resolve_text_table_collisions(
    blocks: List[dict],
    tables: List[dict],
    sheet_spec: dict,
    sheet_name: str,
) -> Tuple[List[dict], List[str]]:
    """Auto-fix text blocks that collide with table rectangles.

    The converter writes tables first and text blocks afterwards, so a
    block landing inside a table rectangle would silently overwrite its
    header row or data cells (a very common small-model mistake: the
    heading label is placed on the table's start cell).

    Fix strategy (deterministic, the table is the structural backbone):
      1. PROMOTION — a title-like block (bold or font_size ≥ 13) whose
         cell equals a table's start_cell, where the table has no title
         yet, becomes that table's title. The converter renders the
         title above the header row, shifting the whole table down one
         row. Every same-sheet formula / chart range that referenced
         the table's OLD rows is shifted down one row too, so SUM()
         ranges and chart series stay aligned with the moved data.
      2. DROP — any remaining block whose cell falls inside a table
         rectangle (recomputed after promotions, so the extra title row
         is included, plus the title cell of titled tables) is removed.

    Mutates `tables`, `sheet_spec["formulas"]` and `sheet_spec["charts"]`
    in place for the row shifts. Returns (kept_blocks, fix_log).
    """
    fix_log: List[str] = []
    remaining = list(blocks)
    # (lo_row, hi_row, col_lo, col_hi) rows moved +1 — column limits
    # keep side columns (e.g. an inputs column next to the table)
    # exactly where the model put them.
    shift_intervals: List[Tuple[int, int, int, int]] = []

    # 1. Title promotion
    for t in tables:
        if t.get("title"):
            continue
        start = t.get("start_cell")
        if not isinstance(start, str):
            continue
        for i, block in enumerate(remaining):
            if block.get("cell") == start and _looks_like_title(block):
                title = str(block["text"]).strip()[:300]
                t["title"] = _sanitize_cell_text(title)
                remaining.pop(i)
                # Old (pre-shift) rows covered by the table — formulas
                # referencing these rows must move down with the table.
                try:
                    start_row, start_col = cell_to_indices(start)
                except ValueError:
                    break
                ncols = max(len(t.get("headers") or []), 1)
                nrows = 1 + len(t.get("rows") or [])
                fd = t.get("fill_down") or {}
                nrows += _as_int(fd.get("rows"), 0)
                if t.get("total_row"):
                    nrows += 1
                shift_intervals.append(
                    (start_row, start_row + nrows - 1, start_col, start_col + ncols - 1)
                )
                fix_log.append(
                    f"block {start!r} promoted to title of table at {start!r} "
                    "(table + formula refs shifted down 1 row)"
                )
                break

    # 2. Shift row references (formulas + chart ranges) for every
    #    promotion. Intervals are disjoint (tables never overlap — a
    #    hard validation error), so a reference matches at most one.
    if shift_intervals:

        def _shift_any(text: str, own_sheet: Optional[str] = None) -> str:
            for lo, hi, clo, chi in shift_intervals:
                text = _shift_formula_refs(text, lo, hi, 1, own_sheet, clo, chi)
            return text

        for t in tables:
            t["rows"] = [
                [
                    (_shift_any(v) if isinstance(v, str) and v.startswith("=") else v)
                    for v in row
                ]
                for row in t.get("rows") or []
            ]
            if t.get("total_row"):
                t["total_row"] = [
                    (_shift_any(v) if isinstance(v, str) and v.startswith("=") else v)
                    for v in t["total_row"]
                ]

        # Sheet-level formulas: shift both the reference rows and the
        # target cell when it lands inside the shifted table rect
        # (row AND column band — side cells outside the table stay).
        pairs = sheet_spec.get("formulas")
        if pairs:
            shifted_pairs = []
            for cell, formula in pairs:
                new_formula = _shift_any(formula)
                new_cell = cell
                try:
                    r, c = cell_to_indices(cell)
                    if any(
                        lo <= r <= hi and clo <= c <= chi
                        for (lo, hi, clo, chi) in shift_intervals
                    ):
                        new_cell = f"{get_column_letter(c)}{r + 1}"
                except (ValueError, TypeError):
                    pass
                shifted_pairs.append((new_cell, new_formula))
            sheet_spec["formulas"] = shifted_pairs

        # Chart categories/values ranges (own-sheet qualified or bare)
        for chart in sheet_spec.get("charts") or []:
            if isinstance(chart.get("categories_range"), str):
                chart["categories_range"] = _shift_any(
                    chart["categories_range"], own_sheet=sheet_name
                )
            for sr in chart.get("series") or []:
                if isinstance(sr.get("values_range"), str):
                    sr["values_range"] = _shift_any(
                        sr["values_range"], own_sheet=sheet_name
                    )

    # 3. Drop blocks inside table rectangles (recomputed — a promoted
    #    title extends the table downward by one row) or on a titled
    #    table's title cell.
    occupied: List[Tuple[int, int, int, int]] = []
    for t in tables:
        rect = _table_rect(t)
        if rect is not None:
            occupied.append(rect)
        if t.get("title") and isinstance(t.get("start_cell"), str):
            try:
                r, c = cell_to_indices(t["start_cell"])
                occupied.append((r, c, r, c))  # title cell itself
            except (ValueError, TypeError):
                pass
    kept: List[dict] = []
    for block in remaining:
        cell = block.get("cell")
        try:
            br, bc = cell_to_indices(cell)
        except (ValueError, TypeError):
            kept.append(block)  # invalid cells were already filtered
            continue
        inside = any(r1 <= br <= r2 and c1 <= bc <= c2 for (r1, c1, r2, c2) in occupied)
        if inside:
            fix_log.append(f"block {cell!r} dropped (inside a table)")
        else:
            kept.append(block)
    return kept, fix_log


_FORMULA_RANGE_RE = re.compile(
    # start must not be glued to a word/quoted string (defined names,
    # text literals)…
    r"(?<![A-Za-z0-9_'\"])"
    # …optional sheet qualifier: 'My Sheet'! or Sheet1!
    r"(?:(?:'[^']+'|[A-Za-z0-9_][A-Za-z0-9_. ]*)!)?"
    r"(\$?[A-Za-z]{1,3}\$?)(\d{1,7})"
    r"(?::(\$?[A-Za-z]{1,3}\$?)(\d{1,7}))?"
    # …and the token must not run into a letter/digit/"(" so function
    # names like LOG10( or defined names like Rate2 never match.
    r"(?![\dA-Za-z(])"
)


def _shift_formula_refs(
    text: str,
    lo_row: int,
    hi_row: int,
    delta: int,
    own_sheet: Optional[str] = None,
    col_lo: Optional[int] = None,
    col_hi: Optional[int] = None,
) -> str:
    """Shift row numbers in cell/range references inside a formula.

    Only references whose row numbers ALL fall in [lo_row, hi_row] are
    shifted by `delta`. When col_lo/col_hi are given, references must
    ALSO have both columns inside that band — the title-promotion
    shift uses this so side columns (an inputs column next to the
    table) never move with the table. Sheet-qualified references
    (Inputs!$B$4) are never shifted unless their sheet matches
    `own_sheet` (used for chart range strings that qualify their own
    sheet). Function names like LOG10( are excluded by the trailing
    lookahead.
    """

    def _col_ok(letters: Optional[str]) -> bool:
        if not letters:
            return True
        col = letters.replace("$", "").upper()
        try:
            return 1 <= column_index_from_string(col) <= MAX_COL_INDEX
        except ValueError:
            return False

    def repl(m: "re.Match") -> str:
        full = m.group(0)
        if "!" in full:
            sheet_part = full.split("!", 1)[0].strip().strip("'")
            if own_sheet is None or sheet_part.lower() != own_sheet.lower():
                return full
            prefix, rest = full.split("!", 1)
            prefix += "!"
        else:
            prefix, _ = "", full
        c1, r1, c2, r2 = m.group(1), m.group(2), m.group(3), m.group(4)
        if not _col_ok(c1) or not _col_ok(c2):
            return full
        rows = [int(r1)] + ([int(r2)] if r2 is not None else [])
        if not all(lo_row <= r <= hi_row for r in rows):
            return full
        if any(r + delta > MAX_ROW_INDEX for r in rows):
            return full
        if col_lo is not None:
            i1 = column_index_from_string(c1.replace("$", "").upper())
            i2 = column_index_from_string(c2.replace("$", "").upper()) if c2 else i1
            if not (col_lo <= i1 <= col_hi and col_lo <= i2 <= col_hi):
                return full
        out = f"{prefix}{c1}{int(r1) + delta}"
        if c2 is not None and r2 is not None:
            out += f":{c2}{int(r2) + delta}"
        return out

    return _FORMULA_RANGE_RE.sub(repl, text)


# ── Off-by-one formula row heal ──────────────────────────────────
#
# Small models anchor a table at start_cell, add a "title" field, and
# then write the table's formulas as if start_cell were the HEADER row
# (data starting one row below start_cell) — the no-title row math the
# schema example teaches. The converter renders the title ON start_cell
# and pushes the header/data rows one row lower, so every formula lands
# one row above its target: =B3-D3 on the first data row points at the
# header text (→ #VALUE!), =A3+1 for month 2 counts the "Month"
# header. Observed with every model tested (qwen, gpt-oss-120b).
#
# Detection is purely structural: a data-row or total-row formula that
# references the table's own header row (via the table's column band)
# is always wrong — header cells hold text. When that signature is
# found, the model's whole coordinate system for this table was one
# row low, so ALL references into the model's mistaken data-row band
# are shifted down one row (per endpoint): table rows, total rows,
# cross-sheet references, sheet-level formulas and chart ranges.
# Freeze panes computed against the model's layout move with it.

_HealBand = Tuple[str, int, int, int, int]  # (sheet, lo_row, hi_row, col_lo, col_hi)


def _table_geometry(table: dict) -> Optional[Tuple[int, int, int, int, int]]:
    """Actual layout of a normalized table.

    Returns (header_row, first_data_row, last_data_row, col_lo, col_hi)
    — last_data_row includes fill_down rows — or None when geometry
    can't be parsed (already reported as a validation error).
    """
    start = table.get("start_cell")
    if not isinstance(start, str) or not is_valid_cell(start):
        return None
    try:
        r, c = cell_to_indices(start)
    except (ValueError, TypeError):
        return None
    header = r + (1 if table.get("title") else 0)
    ncols = max(len(table.get("headers") or []), 1)
    n_data = len(table.get("rows") or [])
    fd = table.get("fill_down") or {}
    n_data += _as_int(fd.get("rows"), 0)
    return (header, header + 1, header + n_data, c, c + ncols - 1)


def _touches_row_in_band(
    text: str, row: int, col_lo: int, col_hi: int, own_sheet: str
) -> bool:
    """True when `text` references `row` through the table's column band.

    Single cells (=B3) and range endpoints (=SUM(B3:B38)) both count.
    References in columns OUTSIDE the band (a side inputs column like
    $G$3) never trigger — those cells are not part of the table.
    References explicitly qualified with ANOTHER sheet
    (Amortization!$B$4) never trigger either — they point at that
    sheet's rows, not this table's header row.
    """
    hit = False

    def check(m: "re.Match") -> str:
        nonlocal hit
        full = m.group(0)
        if "!" in full:
            prefix, _rest = full.split("!", 1)
            eff_sheet = prefix.strip().strip("'")
            if eff_sheet.lower() != (own_sheet or "").lower():
                return full  # foreign sheet — not this table's row
        c1, r1, c2, r2 = m.group(1), m.group(2), m.group(3), m.group(4)
        try:
            i1 = column_index_from_string(c1.replace("$", "").upper())
            i2 = column_index_from_string(c2.replace("$", "").upper()) if c2 else i1
        except ValueError:
            return full
        if col_lo <= i1 <= col_hi or col_lo <= i2 <= col_hi:
            if int(r1) == row or (r2 is not None and int(r2) == row):
                hit = True
        return full

    _FORMULA_RANGE_RE.sub(check, text)
    return hit


def _shift_band_refs(text: str, own_sheet: str, bands: List[_HealBand]) -> str:
    """Shift references inside heal bands down one row (per endpoint).

    An endpoint (cell, or one end of a range) matches when its column
    is inside the band's column range, its row inside the band's row
    range, and the reference's effective sheet — an explicit qualifier
    like Amortization!B3, else `own_sheet` — is the band's sheet.
    Endpoints shift individually, so a range whose end already points
    at the real last data row keeps it. Bands on one sheet are disjoint
    (tables never overlap), so an endpoint shifts at most once.
    """
    if not bands or not isinstance(text, str):
        return text

    def bump(n: int, col: int, sheet_l: str) -> int:
        target = sheet_l.lower() if sheet_l else ""
        for bsheet, lo, hi, clo, chi in bands:
            if bsheet.lower() != target:
                continue
            if clo <= col <= chi and lo <= n <= hi:
                return n + 1
        return n

    def repl(m: "re.Match") -> str:
        full = m.group(0)
        if "!" in full:
            prefix, _rest = full.split("!", 1)
            eff_sheet = prefix.strip().strip("'")
            prefix += "!"
        else:
            prefix, eff_sheet = "", own_sheet
        c1, r1, c2, r2 = m.group(1), m.group(2), m.group(3), m.group(4)
        try:
            i1 = column_index_from_string(c1.replace("$", "").upper())
            i2 = column_index_from_string(c2.replace("$", "").upper()) if c2 else i1
        except ValueError:
            return full
        n1 = int(r1)
        n2 = int(r2) if r2 is not None else None
        new1 = bump(n1, i1, eff_sheet)
        new2 = bump(n2, i2, eff_sheet) if n2 is not None else None
        if new1 == n1 and new2 == n2:
            return full
        out = f"{prefix}{c1}{new1}"
        if c2 is not None and r2 is not None:
            out += f":{c2}{new2}"
        return out

    return _FORMULA_RANGE_RE.sub(repl, text)


def _heal_off_by_one_formula_rows(spec: dict) -> None:
    """Detect and repair one-row-low formula references (see above).

    Mutates the normalized spec in place. Only fires when a table's own
    data-row/total-row formulas reference that table's header row — a
    guaranteed bug — so correctly written specs are untouched.
    """
    bands: List[_HealBand] = []
    anchors: Dict[str, List[int]] = {}  # sheet (lower) → healed tables' start rows

    # Pass 1 — detect, per table (its own sheet / column band)
    for s in spec.get("sheets") or []:
        sheet_name = str(s.get("name") or "Sheet")
        for t in s.get("tables") or []:
            geo = _table_geometry(t)
            if geo is None:
                continue
            header, _first, last, clo, chi = geo
            formulas = [
                v
                for row in (t.get("rows") or [])
                for v in row
                if isinstance(v, str) and v.startswith("=")
            ]
            formulas += [
                v
                for v in (t.get("total_row") or [])
                if isinstance(v, str) and v.startswith("=")
            ]
            if not formulas:
                continue
            if not any(
                _touches_row_in_band(f, header, clo, chi, sheet_name) for f in formulas
            ):
                continue
            bands.append((sheet_name, header, last - 1, clo, chi))
            try:
                start_row, _c = cell_to_indices(t["start_cell"])
            except (ValueError, TypeError):
                start_row = header
            anchors.setdefault(sheet_name.lower(), []).append(start_row)
            _log(
                "off-by-one heal: sheet %r table@%s — formulas referenced "
                "the header row %d; shifting in-band refs +1 "
                "(band rows %d-%d, cols %s-%s)",
                sheet_name,
                t.get("start_cell"),
                header,
                header,
                last - 1,
                get_column_letter(clo),
                get_column_letter(chi),
            )

    if not bands:
        return

    # Pass 2 — apply everywhere (rows, totals, sheet formulas, charts)
    for s in spec.get("sheets") or []:
        own_sheet = str(s.get("name") or "Sheet")
        for t in s.get("tables") or []:
            t["rows"] = [
                [
                    (
                        _shift_band_refs(v, own_sheet, bands)
                        if isinstance(v, str) and v.startswith("=")
                        else v
                    )
                    for v in row
                ]
                for row in (t.get("rows") or [])
            ]
            if t.get("total_row"):
                t["total_row"] = [
                    (
                        _shift_band_refs(v, own_sheet, bands)
                        if isinstance(v, str) and v.startswith("=")
                        else v
                    )
                    for v in t["total_row"]
                ]
        pairs = s.get("formulas")
        if pairs:
            s["formulas"] = [
                (cell, _shift_band_refs(f, own_sheet, bands)) for cell, f in pairs
            ]
        for chart in s.get("charts") or []:
            if isinstance(chart.get("categories_range"), str):
                chart["categories_range"] = _shift_band_refs(
                    chart["categories_range"], own_sheet, bands
                )
            for sr in chart.get("series") or []:
                if isinstance(sr.get("values_range"), str):
                    sr["values_range"] = _shift_band_refs(
                        sr["values_range"], own_sheet, bands
                    )
        # Freeze panes chosen against the model's one-row-low layout:
        # when the frozen region reaches a healed table, move it too.
        fp = s.get("freeze_panes")
        if isinstance(fp, str) and is_valid_cell(fp):
            try:
                fr, fc = cell_to_indices(fp)
            except (ValueError, TypeError):
                continue
            starts = anchors.get(own_sheet.lower()) or []
            if fr >= 2 and any(fr >= sr for sr in starts):
                s["freeze_panes"] = f"{get_column_letter(fc)}{fr + 1}"
                _log(
                    "off-by-one heal: sheet %r freeze_panes %s → %s",
                    own_sheet,
                    fp,
                    s["freeze_panes"],
                )


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
    - European decimal commas ("1,9", "3,50") → floats — conservative:
      only 1-2 digits after the comma qualify, so "1,234" (ambiguous
      thousands) stays text
    - currency strings ("$4.50", "1,234.56 €", "£9") → numbers with
      the symbol/thousands separators stripped — a very common
      small-model mistake that otherwise leaves SUM() totals at 0
      (Excel silently ignores text in SUM ranges)
    - ISO date strings "YYYY-MM-DD" → datetime.date (+ format later)
    - everything else sanitized for illegal characters
    """
    if isinstance(value, str):
        s = value.strip()
        if s and _INT_STR_RE.match(s) and len(s) < 16:
            return int(s)
        if s and _FLOAT_STR_RE.match(s):
            return float(s)
        if s and _DECIMAL_COMMA_RE.match(s):
            # European decimal comma ("1,9" / "3,50"). Only 1-2 digits
            # after the comma qualify — "1,234" could legitimately be
            # an English thousands number, so it stays text.
            return float(s.replace(",", ".", 1))
        m = _CURRENCY_STR_RE.match(s)
        if m:
            num = next(g for g in m.groups() if g is not None)
            num = num.replace(",", "")
            try:
                return int(num) if _INT_STR_RE.match(num) else float(num)
            except ValueError:
                pass
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


def _cf_operand(value: Any) -> str:
    """A cell_is comparison value → its formula-string form."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        f = float(value)
        return str(int(f)) if f.is_integer() else repr(f)
    if isinstance(value, str):
        return f'"{value}"'
    return '""'


def _normalize_data_validation(dv: Any, sheet_name: str) -> Optional[dict]:
    """Sanitize one data_validation entry; None when unusable."""
    if not isinstance(dv, dict):
        return None
    try:
        _sheet, r1, r2, c1, c2 = parse_range(str(dv.get("range", "")), sheet_name)
    except ValueError:
        return None
    single = r1 == r2 and c1 == c2
    range_str = (
        f"{get_column_letter(c1)}{r1}"
        if single
        else f"{get_column_letter(c1)}{r1}:{get_column_letter(c2)}{r2}"
    )

    values_raw = dv.get("values")

    # Range-sourced dropdown first: entries read live from a range
    # (e.g. the Categories sheet) so the list grows with its source.
    source_raw = dv.get("source_range")
    if isinstance(source_raw, str):
        ref = source_raw.strip().lstrip("=")
        if _SOURCE_RANGE_RE.match(ref):
            out: Dict[str, Any] = {
                "range": range_str,
                "source_range": ref,
                "allow_blank": bool(dv.get("allow_blank", True)),
                "error_style": (
                    dv.get("error_style")
                    if dv.get("error_style") in ("stop", "warning", "information")
                    else "stop"
                ),
            }
            for key, limit in (
                ("prompt_title", 32),
                ("prompt", 255),
                ("error_title", 32),
                ("error", 255),
            ):
                s = dv.get(key)
                if isinstance(s, str) and s.strip():
                    out[key] = _sanitize_cell_text(s.strip())[:limit]
            return out
        # unusable ref → fall through to the inline-values path

    if not isinstance(values_raw, list):
        return None
    values: List[str] = []
    for v in values_raw[:MAX_DV_VALUES]:
        if isinstance(v, bool):
            values.append("TRUE" if v else "FALSE")
        elif isinstance(v, (int, float)):
            f = float(v)
            values.append(str(int(f)) if f.is_integer() else repr(f))
        elif isinstance(v, str):
            s = v.strip()
            # Excel list syntax: entries are one comma-joined quoted
            # string — embedded commas or quotes would corrupt it.
            if not s or "," in s or '"' in s:
                continue
            values.append(s)
    if not values:
        return None
    if len('","'.join(values)) + 2 > 255:  # Excel's hard list limit
        return None

    out: Dict[str, Any] = {
        "range": range_str,
        "values": values,
        "allow_blank": bool(dv.get("allow_blank", True)),
        "error_style": (
            dv.get("error_style")
            if dv.get("error_style") in ("stop", "warning", "information")
            else "stop"
        ),
    }
    for key, limit in (
        ("prompt_title", 32),
        ("prompt", 255),
        ("error_title", 32),
        ("error", 255),
    ):
        s = dv.get(key)
        if isinstance(s, str) and s.strip():
            out[key] = _sanitize_cell_text(s.strip())[:limit]
    return out


def _normalize_conditional_format(cf: Any, sheet_name: str) -> Optional[dict]:
    """Sanitize one conditional_formats entry; None when unusable."""
    if not isinstance(cf, dict):
        return None
    try:
        _sheet, r1, r2, c1, c2 = parse_range(str(cf.get("range", "")), sheet_name)
    except ValueError:
        return None
    range_str = f"{get_column_letter(c1)}{r1}:{get_column_letter(c2)}{r2}"

    rules_raw = cf.get("rules")
    if not isinstance(rules_raw, list):
        return None
    rules: List[dict] = []
    for raw in rules_raw[:MAX_CF_RULES_PER_ENTRY]:
        if not isinstance(raw, dict):
            continue
        rtype = raw.get("type")
        if rtype not in ("cell_is", "formula"):
            continue
        rule: Dict[str, Any] = {
            "type": rtype,
            "stop_if_true": bool(raw.get("stop_if_true", False)),
        }
        if valid_hex_color(raw.get("fill")):
            rule["fill"] = str(raw["fill"]).upper()
        if valid_hex_color(raw.get("font_color")):
            rule["font_color"] = str(raw["font_color"]).upper()
        if raw.get("bold") is True:
            rule["bold"] = True

        if rtype == "formula":
            formula = raw.get("formula")
            if not isinstance(formula, str):
                formula = raw.get("value")  # ergonomic alias
            if not isinstance(formula, str) or not formula.strip():
                continue
            expression = formula.strip()
            if expression.startswith("="):
                expression = expression[1:]
            rule["value"] = expression
        else:
            op = raw.get("operator", "equal")
            if op not in _CF_OPERATORS:
                continue
            rule["operator"] = op
            value = raw.get("value")
            if op in ("between", "not_between"):
                if not isinstance(value, list) or len(value) < 2:
                    continue
                pair = []
                for v in value[:2]:
                    if isinstance(v, (bool, int, float)):
                        pair.append(v)
                    elif isinstance(v, str) and v.strip() and '"' not in v:
                        pair.append(v.strip())
                    else:
                        pair = []
                        break
                if len(pair) != 2:
                    continue
                rule["value"] = pair
            else:
                if isinstance(value, (bool, int, float)):
                    rule["value"] = value
                elif isinstance(value, str) and value.strip() and '"' not in value:
                    rule["value"] = value.strip()
                else:
                    continue
        rules.append(rule)

    if not rules:
        return None
    return {"range": range_str, "rules": rules}


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

        # Explicit opt-out of the freeze-below-first-header default:
        # sheets meant to be scrolled freely (budget planners…).
        if sheet.get("no_freeze") is True:
            s["no_freeze"] = True

        # Hidden helper sheets (calc engines the user never edits).
        if sheet.get("hidden") is True:
            s["hidden"] = True

        # Dropdown-list validations
        dvs = []
        for dv in (sheet.get("data_validation") or [])[:MAX_VALIDATIONS_PER_SHEET]:
            ndv = _normalize_data_validation(dv, s["name"])
            if ndv is not None:
                dvs.append(ndv)
        if dvs:
            s["data_validation"] = dvs

        # Conditional formatting
        cfs = []
        for cf in (sheet.get("conditional_formats") or [])[:MAX_CF_ENTRIES_PER_SHEET]:
            ncf = _normalize_conditional_format(cf, s["name"])
            if ncf is not None:
                cfs.append(ncf)
        if cfs:
            s["conditional_formats"] = cfs

        # Cell protection: everything locked except unlocked_ranges
        prot = sheet.get("protect")
        if prot is True:
            s["protect"] = {"unlocked": [], "password": None}
        elif isinstance(prot, dict):
            unlocked = []
            for rng in prot.get("unlocked_ranges") or []:
                try:
                    _sheet, r1, r2, c1, c2 = parse_range(str(rng), s["name"])
                except ValueError:
                    continue
                unlocked.append(
                    f"{get_column_letter(c1)}{r1}:{get_column_letter(c2)}{r2}"
                )
            password = prot.get("password")
            s["protect"] = {
                "unlocked": unlocked,
                "password": (
                    password.strip()
                    if isinstance(password, str) and password.strip()
                    else None
                ),
            }

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
                    "number_format": (
                        block["number_format"]
                        if valid_number_format(block.get("number_format"))
                        else None
                    ),
                }
            )
        # NOTE: s["text_blocks"] is assigned after the tables are built —
        # blocks colliding with a table are promoted/dropped below.

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

            # per-column horizontal alignment (marks, scores…)
            aligns: Dict[str, str] = {}
            al = table.get("alignments")
            if isinstance(al, dict):
                for key, align in al.items():
                    if re.match(r"^[A-Za-z]{1,3}$", str(key)) and align in (
                        "left",
                        "center",
                        "right",
                    ):
                        aligns[str(key).upper()] = align

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
                "alignments": aligns,
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
            if chart.get("show_values") is True:
                c["show_values"] = True
            if isinstance(chart.get("value_numfmt"), str) and valid_number_format(
                chart.get("value_numfmt")
            ):
                c["value_numfmt"] = chart["value_numfmt"]
            charts.append(c)
        if charts:
            s["charts"] = charts

        # Resolve text_block ↔ table collisions (auto-fix). Runs AFTER
        # tables/formulas/charts are normalized so a promotion can also
        # shift affected row references. The converter writes tables
        # first and text blocks after, so a block landing inside a
        # table rectangle would silently overwrite the header row /
        # data. Small models do this a lot (heading label placed on the
        # table's start cell).
        #   • title-like block on a table's start_cell & no title →
        #     PROMOTED to the table's title; the converter shifts the
        #     header row down one row, and every formula/chart range
        #     referencing the table's old rows is shifted with it
        #   • any other block inside a table rect (or on a table's
        #     title cell) → DROPPED
        if blocks and tables:
            blocks, fix_log = _resolve_text_table_collisions(
                blocks, tables, s, sheet.get("name", "Sheet")
            )
            for line in fix_log:
                _log("text/table collision fixed: %s", line)
            if blocks:
                s["text_blocks"] = blocks
            else:
                s.pop("text_blocks", None)
        elif blocks:
            s["text_blocks"] = blocks

        out["sheets"].append(s)

    # Off-by-one formula heal: models routinely write table formulas
    # in the "no title" coordinate system while the rendered title
    # pushes the data one row lower. Detect via header-row references
    # and shift the model's in-band refs down 1 row — including
    # cross-sheet refs, sheet formulas, charts and freeze panes.
    _heal_off_by_one_formula_rows(out)

    return out


# ── LLM call + JSON extraction ────────────────────────────────────────


# Characters that can begin a JSON value. Used by the repair walker to
# spot a missing comma between two array elements.
_JSON_VALUE_START = set('{["-0123456789tfn')


# Full Excel system prompt (loaded once at import; the file reads are
# lru_cached by the prompts package).
EXCEL_SYSTEM_PROMPT = get_prompt("excel_system")

# Radically simplified fallback prompt for the 3rd LLM attempt. After
# two failures the full schema prompt is clearly too much for the
# model — this compact contract keeps only what a tiny local model
# can reliably produce: one table, plain rows, an optional total row.
_SIMPLIFIED_EXCEL_PROMPT = get_prompt("excel_simplified")


def _repair_json_text(src: str) -> Optional[str]:
    """Repair structurally broken LLM JSON by re-walking the token stream.

    Small local models routinely emit JSON that is *almost* right: a
    missing ``]`` before a ``}``, a missing ``}`` before a ``]``, a
    missing comma between elements, a trailing comma before a closer, a
    string left unterminated (token-limit truncation), or prose after
    the root object closes. The walker re-emits the text while tracking
    the open-container stack and fixes exactly those failure modes:

    - ``}`` arriving while an array is open  → insert the missing ``]``
    - ``]`` arriving while an object is open → insert the missing ``}``
    - a value starting right after a finished value → insert ``,``
    - a trailing comma before a closer → dropped
    - everything after the root object closes → truncated
    - an unterminated string at end of input → closed
    - containers still open at end of input → closed in order

    Returns the repaired text, or None when there is nothing to walk.
    Never raises — the worst case is a repaired text that still fails
    to parse, and the caller falls back to its other salvage steps.
    """
    if not src or "{" not in src:
        return None

    out: List[str] = []
    stack: List[str] = []  # open containers, bottom → top ("{" or "[")
    in_string = False
    escape = False
    prev_sig = ""  # last significant char emitted outside strings

    def _value_end() -> bool:
        # Anything that is not a container-opener / ':' / ',' means a
        # value (or a key string) just finished.
        return prev_sig != "" and prev_sig not in "{[:,"

    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        i += 1

        if in_string:
            out.append(c)
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
                prev_sig = '"'
            continue

        if c in " \t\r\n":
            continue

        if c == '"':
            # A string directly after a finished value = a missing
            # comma (next array element, or the next object key).
            if stack and _value_end():
                out.append(",")
            out.append(c)
            in_string = True
            continue

        if c in "{[":
            # A value directly after a finished value inside an array =
            # a missing comma between elements.
            if stack and stack[-1] == "[" and _value_end():
                out.append(",")
            out.append(c)
            stack.append(c)
            prev_sig = c
            continue

        if c in "}]":
            # A trailing comma right before a closer is invalid JSON.
            while out and out[-1] == ",":
                out.pop()
            if c == "}":
                # The model closed an object while an array was still
                # open → the array closer went missing. Close it first.
                while stack and stack[-1] == "[":
                    out.append("]")
                    stack.pop()
            else:
                # Symmetric: closed an array while an object was open.
                while stack and stack[-1] == "{":
                    out.append("}")
                    stack.pop()
            if not stack:
                # The root just closed — anything after this point is
                # prose / extra data, so truncate here.
                out.append(c)
                break
            out.append(c)
            stack.pop()
            prev_sig = c
            continue

        # ':', ',', digits, literals (and stray prose) pass through.
        out.append(c)
        prev_sig = c

    if in_string:
        # Output was truncated mid-string (token limit) — close it so
        # the rest of the repair can produce parseable JSON.
        if escape and out and out[-1] == "\\":
            out.pop()  # a dangling escape would eat the closing quote
        out.append('"')

    while stack:
        out.append("]" if stack[-1] == "[" else "}")
        stack.pop()

    return "".join(out)


def _extract_json_object(content: str) -> Any:
    """Extract the first balanced JSON object from LLM output.

    Handles: markdown fences, leading/trailing prose, a single
    wrapper key like {"workbook": {...}}.

    Salvage ladder for structurally broken JSON (small local models
    emit JSON that is *almost* right, and a hard parse failure would
    waste the whole LLM call — the repairs feed the retry round
    instead):
      1. strict parse from the first ``{``;
      2. trailing-comma cleanup;
      3. structural repair walker (missing ``]``/``}`` closers,
         missing commas, truncated strings, extra data after the
         root object);
      4. strict parse from every subsequent ``{`` — the first brace
         may live in prose (``blah {oops} {"sheets": ...}``).

    Raises ValueError when no object can be extracted at all.
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
    body = text[start:]
    obj: Any = None
    first_error: Optional[json.JSONDecodeError] = None

    # 1. strict parse
    try:
        obj, _ = decoder.raw_decode(body)
    except json.JSONDecodeError as e:
        first_error = e

    # 2. common quick fix: trailing commas
    if obj is None:
        cleaned = re.sub(r",\s*([}\]])", r"\1", body)
        try:
            obj, _ = decoder.raw_decode(cleaned)
        except json.JSONDecodeError:
            pass

    # 3. structural repair walker
    if obj is None:
        repaired = _repair_json_text(body)
        if repaired is not None and repaired != body:
            try:
                obj, _ = decoder.raw_decode(repaired)
            except json.JSONDecodeError:
                pass
            else:
                _log("JSON repair walker recovered a broken spec")

    # 4. the first '{' may live in prose — try every other one
    if obj is None:
        for pos in [m.start() for m in re.finditer(r"\{", body)][:200]:
            if pos == 0:
                continue
            try:
                obj, _ = decoder.raw_decode(body[pos:])
            except json.JSONDecodeError:
                continue
            break

    if obj is None:
        detail = str(first_error) if first_error is not None else "no parseable object"
        raise ValueError(f"unparseable JSON from LLM: {detail}")

    # Unwrap {"workbook": {...}} / {"spec": {...}} single-key wrappers
    if isinstance(obj, dict) and "sheets" not in obj and len(obj) == 1:
        inner = next(iter(obj.values()))
        if isinstance(inner, dict) and "sheets" in inner:
            return inner
    return obj


# ── LLM shortcut expansion (post-processing) ──────────────────────────


def _sum_placeholder(col_letter: str) -> str:
    """=SUM() formula for a table column, with row placeholders.

    {first_row}/{last_row} are expanded by the converter at write
    time (the same contract as model-written total_row formulas), so
    the SUM range always covers exactly the rows that were rendered.
    """
    return f"=SUM({col_letter}{{first_row}}:{col_letter}{{last_row}})"


def _table_numeric_columns(headers: List[Any], rows: List[Any]) -> set:
    """Column indices (0-based) that carry numeric data in any row."""
    numeric: set = set()
    for row in rows:
        if not isinstance(row, list):
            row = [row]
        for i, v in enumerate(row[: len(headers)]):
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                numeric.add(i)
    return numeric


def _expand_total_row_shortcut(table: dict) -> None:
    """Expand `total_row` shortcuts into a concrete list (in place).

    - ``true`` → ["Total", =SUM(...) for every numeric column, None
      for the remaining non-numeric columns]
    - ``{"sum_columns": ["C", "Price"]}`` → sums only the listed
      columns (letters OR header names, case-insensitive)
    - a list → empty cells ("", null) in numeric columns are
      auto-filled with =SUM(...); non-empty values are preserved
      (a "Target"/"Budget" number must NOT be replaced by a sum)
    """
    tr = table.get("total_row")
    if tr is None or not isinstance(table, dict):
        return
    headers = table.get("headers")
    rows = table.get("rows")
    if not isinstance(headers, list) or not headers:
        return
    if not isinstance(rows, list) or not rows:
        return
    n = len(headers)
    numeric = _table_numeric_columns(headers, rows)

    if tr is True:
        total: List[Any] = []
        for i in range(n):
            if i in numeric:
                total.append(_sum_placeholder(get_column_letter(i + 1)))
            elif i == 0:
                total.append("Total")
            else:
                total.append(None)
        table["total_row"] = total
        return

    if isinstance(tr, dict):
        sum_idx: set = set()
        cols = tr.get("sum_columns")
        if isinstance(cols, list):
            for c in cols:
                cs = str(c).strip()
                if re.match(r"^[A-Za-z]{1,3}$", cs):
                    try:
                        idx = column_index_from_string(cs.upper()) - 1
                    except ValueError:
                        continue
                    if 0 <= idx < n:
                        sum_idx.add(idx)
                else:
                    # header-name lookup (case-insensitive)
                    for hi, h in enumerate(headers):
                        if str(h).strip().lower() == cs.lower():
                            sum_idx.add(hi)
                            break
        total = []
        for i in range(n):
            if i in sum_idx:
                total.append(_sum_placeholder(get_column_letter(i + 1)))
            elif i == 0:
                total.append("Total")
            else:
                total.append(None)
        table["total_row"] = total
        return

    if isinstance(tr, list):
        out = list(tr[:n]) + [None] * max(0, n - len(tr))
        for i in range(n):
            v = out[i]
            empty = v is None or (isinstance(v, str) and not v.strip())
            if empty and i in numeric:
                out[i] = _sum_placeholder(get_column_letter(i + 1))
        table["total_row"] = out


# ── data_validation shape healing ────────────────────────────────────
#
# Small models keep inventing a MAP form for data_validation (observed
# in production: gpt-oss-20b wrote {"Meal Type": {"list":
# "Breakfast,Lunch,Dinner,Snack"}} on a meal-planner brief). The intent
# — a dropdown on a column — is perfectly clear, so it is converted
# instead of failing the whole spec through three retry rounds.

# A pair key that already looks like "B4" / "B4:B31" / "B$4:$B$31".
_DV_RANGE_KEY_RE = re.compile(r"^[A-Za-z]{1,3}\$?\d+(?::[A-Za-z]{1,3}\$?\d+)?$")
# A pair key that is a bare column letter ("B").
_DV_COLUMN_KEY_RE = re.compile(r"^[A-Za-z]{1,3}$")


def _sheet_table_columns(sheet: dict) -> List[dict]:
    """Resolution map for data_validation keys: one record per table
    column — {"header": lower-case header, "col": letter, "r1": first
    data row, "r2": last data row} (r2 stretched over 10 rows when the
    table has no data rows yet, so blank templates get dropdowns too).
    """
    out: List[dict] = []
    tables = sheet.get("tables")
    if not isinstance(tables, list):
        return out
    for table in tables:
        if not isinstance(table, dict):
            continue
        headers = table.get("headers")
        if not isinstance(headers, list):
            continue
        start = table.get("start_cell")
        if not is_valid_cell(start):
            start = "A1"
        try:
            start_row, start_col = cell_to_indices(start)
        except ValueError:
            continue
        if isinstance(table.get("title"), str) and str(table.get("title")).strip():
            first_data = start_row + 2
        else:
            first_data = start_row + 1
        rows = table.get("rows")
        n_rows = len(rows) if isinstance(rows, list) else 0
        last_data = first_data + max(n_rows, 1) - 1
        if n_rows == 0:
            last_data = first_data + 9  # blank template — cover 10 rows
        for i, h in enumerate(headers):
            if isinstance(h, str) and h.strip():
                out.append(
                    {
                        "header": h.strip().lower(),
                        "col": get_column_letter(start_col + i),
                        "r1": first_data,
                        "r2": last_data,
                    }
                )
    return out


def _dv_scalar_values(raw: Any) -> Optional[List[str]]:
    """Model value shapes → a clean values list for a DV entry.

    Accepts a comma-joined string ("Yes,No"), an array of scalars, or
    None. Values containing commas/quotes are dropped (Excel list
    syntax cannot carry them); the joined list is trimmed to Excel's
    255-character limit. Returns None when nothing usable remains.
    """
    candidates: List[Any]
    if isinstance(raw, str):
        candidates = [p.strip() for p in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        candidates = list(raw)
    else:
        return None
    out: List[str] = []
    for v in candidates:
        if isinstance(v, bool):
            out.append("TRUE" if v else "FALSE")
        elif isinstance(v, (int, float)):
            f = float(v)
            out.append(str(int(f)) if f.is_integer() else str(f))
        elif isinstance(v, str):
            s = v.strip()
            if s and "," not in s and '"' not in s:
                out.append(s)
    while out and len('","'.join(out)) + 2 > 250:
        out.pop()
    return out or None


def _dv_source_ref(value: dict) -> Optional[str]:
    """A range-source dropdown ref from a model entry, when present."""
    for key in ("source_range", "source", "range_ref"):
        raw = value.get(key)
        if isinstance(raw, str):
            ref = raw.strip().lstrip("=")
            if _SOURCE_RANGE_RE.match(ref):
                return ref
    return None


def _dv_payload_values(payload: dict) -> Optional[List[str]]:
    """values / list / options keys of a payload dict → clean list."""
    raw = payload.get("values")
    if raw is None:
        raw = payload.get("list")
    if raw is None:
        raw = payload.get("options")
    return _dv_scalar_values(raw)


# Presentation fields copied verbatim when a healed entry carries them.
_DV_PRESENTATION_FIELDS = (
    "allow_blank",
    "error_style",
    "prompt_title",
    "prompt",
    "error_title",
    "error",
)


def _coerce_dv_pair(key: Any, value: Any, columns: List[dict]) -> Optional[dict]:
    """One (key, value) pair from a model → a proper DV entry or None.

    key   a header name ("Meal Type"), a bare column letter ("B"), or
          a cell/range ref ("B4", "B4:B31")
    value {"list": "a,b"} | {"values": [...]} | {"source_range": …} |
          ["a","b"] | "a,b" (plus optional presentation fields)
    """
    if not isinstance(key, str):
        return None
    k = key.strip()
    if not k:
        return None

    entry: Dict[str, Any] = {}
    inner_range: Optional[str] = None
    if isinstance(value, dict):
        source = _dv_source_ref(value)
        values = _dv_payload_values(value)
        if source:
            entry["source_range"] = source
        elif values:
            entry["values"] = values
        else:
            return None
        rng = value.get("range")
        if isinstance(rng, str) and _DV_RANGE_KEY_RE.match(rng.strip()):
            inner_range = rng.strip()
        for f in _DV_PRESENTATION_FIELDS:
            if f in value:
                entry[f] = value[f]
    else:
        values = _dv_scalar_values(value)
        if not values:
            return None
        entry["values"] = values

    # Resolve the target range: an explicit inner range wins, then a
    # header-name match, then range-shaped keys, then bare letters.
    if inner_range is not None:
        entry["range"] = inner_range
    else:
        hit = next((c for c in columns if c["header"] == k.lower()), None)
        if hit is not None:
            entry["range"] = f"{hit['col']}{hit['r1']}:{hit['col']}{hit['r2']}"
        elif _DV_RANGE_KEY_RE.match(k):
            if ":" in k:
                entry["range"] = k
            else:
                col = k.split("$")[0].upper()
                span = next((c for c in columns if c["col"] == col), None)
                r1, r2 = (span["r1"], span["r2"]) if span else (2, 51)
                entry["range"] = f"{col}{r1}:{col}{r2}"
        elif _DV_COLUMN_KEY_RE.match(k):
            col = k.upper()
            span = next((c for c in columns if c["col"] == col), None)
            r1, r2 = (span["r1"], span["r2"]) if span else (2, 51)
            entry["range"] = f"{col}{r1}:{col}{r2}"
        else:
            return None
    return entry


def _coerce_data_validation_shape(sheet: dict) -> None:
    """Heal model-invented data_validation shapes (in place).

    Handled forms — all resolve to the documented array of
    {"range", "values" | "source_range"} objects:

      {"Meal Type": {"list": "Breakfast,Lunch,Dinner,Snack"}}  ← map
      {"B": {"list": "Yes,No"}} / {"B4:B31": {"values": [...]}}}
      {"Meal Type": ["a", "b"]} or "a,b,c"                      ← values
      [{"Meal Type": {...}}]                                    ← list of maps
      [{"range": "B4:B31", "list": "a,b"}]                      ← list vs values
      [{"column": "Status", "values": [...]}]                   ← column key

    A correct array passes through untouched (idempotent). Entries
    whose key/value cannot be resolved are DROPPED with a log line —
    a missing dropdown beats a dead workbook (the alternative is a
    spec that fails validation three times over).
    """
    dv = sheet.get("data_validation")
    if dv is None:
        return

    columns = _sheet_table_columns(sheet)
    entries: List[dict] = []
    pairs: List[Tuple[Any, Any]] = []
    changed = False

    if isinstance(dv, dict):
        changed = True
        pairs = list(dv.items())
    elif isinstance(dv, list):
        for e in dv:
            if not isinstance(e, dict):
                changed = True  # garbage element — dropped
                continue
            rng = e.get("range")
            rng_ok = isinstance(rng, str) and bool(rng.strip())
            has_values = isinstance(e.get("values"), list) and bool(e.get("values"))
            has_source = isinstance(e.get("source_range"), str) and bool(
                e.get("source_range").strip()
            )
            if (
                rng_ok
                and (has_values or has_source)
                and "list" not in e
                and "options" not in e
            ):
                entries.append(e)  # already correct — keep untouched
                continue
            # Needs healing — find the range key.
            key: Optional[str] = rng if rng_ok else None
            if key is None:
                for f in ("cell", "column", "col", "header", "field"):
                    v = e.get(f)
                    if isinstance(v, str) and v.strip():
                        key = v.strip()
                        break
            if key is not None:
                pairs.append((key, e))
                changed = True
                continue
            if len(e) == 1:
                k, v = next(iter(e.items()))
                if isinstance(k, str) and isinstance(v, (dict, list, str)):
                    pairs.append((k.strip(), v))
                    changed = True
                    continue
            changed = True  # no way to resolve a range — dropped
    else:
        # Completely wrong type (string/number) — drop the field.
        _log("data_validation healing: dropped non-array/non-map value")
        sheet.pop("data_validation", None)
        return

    for key, value in pairs:
        entry = _coerce_dv_pair(key, value, columns)
        if entry is not None:
            entries.append(entry)
        else:
            _log("data_validation healing: dropped unresolvable entry %r", key)

    if changed:
        sheet["data_validation"] = entries[:MAX_VALIDATIONS_PER_SHEET]


def _drop_wrong_typed_optional_arrays(sheet: dict) -> None:
    """Optional array fields the model emitted in a non-array shape
    (observed: conditional_formats as a map) are dropped instead of
    failing validation — cosmetic features must never kill a workbook.
    `tables` is deliberately NOT dropped: a map-shaped tables field is
    a structural failure the repair round should fix (silently
    producing an empty sheet would be worse). Fields that are valid
    lists are left to the validator.
    """
    for field in (
        "conditional_formats",
        "charts",
        "text_blocks",
        "merged_cells",
        "formulas",
    ):
        v = sheet.get(field)
        if v is None:
            continue
        if not isinstance(v, list):
            # formulas may legitimately be a map {"B10": "=SUM(...)"} —
            # the normalizer accepts both; only the other fields are
            # strictly arrays. Leave maps for formulas untouched.
            if field == "formulas" and isinstance(v, dict):
                continue
            _log("%s healing: dropped non-array value", field)
            sheet.pop(field, None)


def _expand_freeze_header(sheet: dict) -> None:
    """Expand `freeze_header: true` → freeze_panes (in place).

    Freezes everything above the first table's header row (the title
    row when the table has one). An explicit `freeze_panes` always
    wins — the shortcut is then simply dropped.
    """
    if sheet.get("freeze_header") is not True:
        sheet.pop("freeze_header", None)
        return
    if sheet.get("freeze_panes"):
        sheet.pop("freeze_header", None)  # explicit freeze wins
        return
    tables = sheet.get("tables")
    if not isinstance(tables, list) or not tables or not isinstance(tables[0], dict):
        return
    first = tables[0]
    try:
        start_row, _ = cell_to_indices(
            first.get("start_cell") if is_valid_cell(first.get("start_cell")) else "A1"
        )
    except ValueError:
        return
    if isinstance(first.get("title"), str) and first["title"].strip():
        start_row += 1  # the title occupies its own row above the header
    freeze_row = start_row + 1  # below the header row
    if freeze_row > 1:
        sheet["freeze_panes"] = f"A{freeze_row}"
    sheet.pop("freeze_header", None)


def _post_process_spec(spec: Any) -> Any:
    """Expand LLM shortcut keys into concrete spec structures.

    Runs on EVERY LLM attempt, right after JSON extraction and BEFORE
    validation, so models can use ergonomic shortcuts that would
    otherwise surface as validation errors:

    - ``total_row: true`` / ``{"sum_columns": [...]}`` → a real total
      row with =SUM() formulas (see _expand_total_row_shortcut)
    - ``freeze_header: true`` → freeze_panes below the first table's
      title + header rows
    - model-invented ``data_validation`` shapes (maps keyed by
      column/header) → the documented array form
      (see _coerce_data_validation_shape)
    - non-array ``conditional_formats`` / ``charts`` / ``text_blocks``
      / ``merged_cells`` → dropped (cosmetic fields must never kill a
      workbook)

    Malformed input passes through unchanged so the validator can
    report the real problems. Idempotent on already-expanded specs.
    """
    if not isinstance(spec, dict):
        return spec
    sheets = spec.get("sheets")
    if not isinstance(sheets, list):
        return spec
    for sheet in sheets:
        if not isinstance(sheet, dict):
            continue
        _expand_freeze_header(sheet)
        _coerce_data_validation_shape(sheet)
        _drop_wrong_typed_optional_arrays(sheet)
        tables = sheet.get("tables")
        if not isinstance(tables, list):
            continue
        for table in tables:
            if isinstance(table, dict):
                _expand_total_row_shortcut(table)
    return spec


def _llm_limits() -> tuple:
    """The Excel LLM call limits: (timeout_s, num_predict).

    Priority: the persisted tool configuration (Brain ▸ Tools ▸ Excel
    Generation — custom.timeout_s / custom.max_tokens) → the
    EXCEL_GENERATION_* env settings → the hardcoded defaults.
    """
    timeout = float(getattr(settings, "EXCEL_GENERATION_TIMEOUT_SECONDS", 600) or 600)
    num_predict = int(getattr(settings, "EXCEL_GENERATION_MAX_TOKENS", 8192) or 8192)
    try:
        from app.agent.tools import config_store

        cfg = config_store.get_tool_config("use_excel_gen") or {}
        custom = cfg.get("custom") or {}
        if custom.get("timeout_s"):
            timeout = float(custom["timeout_s"])
        if custom.get("max_tokens"):
            num_predict = int(custom["max_tokens"])
    except Exception as e:  # config store unavailable — env defaults
        _log("tool-config limits unavailable (%s) — using env defaults", e)
    return timeout, num_predict


async def _call_llm(messages: List[dict], model: Optional[str] = None) -> str:
    """Single provider-routed chat call in JSON mode.

    ``model``: optional per-call override (the use_excel_gen tool passes
    its Brain ▸ Tools model override here). Falls back to the Excel
    task slot (Settings ▸ AI ▸ Models) when not provided.
    JSON mode maps to response_format=json_object for cloud providers.

    The generation timeout and max tokens are configurable in
    Brain ▸ Tools ▸ Excel Generation (custom.timeout_s /
    custom.max_tokens); the EXCEL_GENERATION_* env settings are the
    fallback defaults.
    """
    model = model or await model_prefs.resolve_task_model("excel")
    if not model:
        raise RuntimeError("No LLM model configured for Excel generation")

    timeout, num_predict = _llm_limits()

    data = await providers.chat_once(
        model,
        messages,
        think=False,
        format="json",  # JSON mode — constrains output to valid JSON
        options={"num_predict": num_predict, "temperature": 0.2},
        timeout=timeout,
    )
    _log(data)
    content = data.get("message", {}).get("content", "")

    if not content.strip():
        raise RuntimeError("LLM returned empty content for Excel generation")
    return content


async def _generate_workbook_json(
    brief: str, requirements: str, model: Optional[str] = None
) -> dict:
    """Specialized Excel AI call: brief → validated + normalized workbook spec.

    Three attempts, cheapest first:
      1. full schema prompt;
      2. repair round — the failed output + validation errors are fed
         back under the full prompt;
      3. simplified prompt — a compact schema-only contract that even
         tiny local models can follow.

    Shortcut post-processing (`total_row: true`, `freeze_header`) runs
    on EVERY attempt right after JSON extraction, before validation.
    Raises RuntimeError when the spec is still invalid after 3 attempts.
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
    for attempt in (1, 2, 3):
        _log("LLM call %d: brief=%r", attempt, brief[:60])
        raw = await _call_llm(messages, model=model)
        last_raw = raw
        try:
            parsed = _extract_json_object(raw)
        except ValueError as e:
            last_errors = [f"JSON parse failure: {e}"]
            parsed = None

        if parsed is not None:
            # Expand LLM shortcuts BEFORE validation — total_row: true
            # and freeze_header would otherwise surface as schema
            # errors even though the model's intent is perfectly clear.
            parsed = _post_process_spec(parsed)
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

        # Build the messages for the next attempt.
        _log("attempt %d invalid: %s", attempt, "; ".join(last_errors[:5]))
        if attempt == 1:
            # Repair round: full prompt + the failed output + errors.
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
        else:
            # Attempt 3: radically simplified prompt — a fresh start
            # with only the compact schema contract.
            messages = [
                {"role": "system", "content": _SIMPLIFIED_EXCEL_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Create an Excel workbook for this request:\n{brief}\n\n"
                        "Respond with the JSON object only."
                    ),
                },
            ]

    raise RuntimeError(
        f"Excel workbook spec failed validation after 3 attempts: "
        f"{'; '.join(last_errors[:5])} "
        f"(raw output started: {last_raw[:200]!r})"
    )


# ── Deterministic template routing (patterns) ─────────────────────

# Two-stage routing (scales to 30+ patterns without drowning the
# classifier in pattern noise — the enemy of small-model accuracy):
#
#   Stage 1  PURE PYTHON keyword matching over every pattern's
#            PATTERN_KEYWORDS (words/stems like "budget", "amortiz"):
#            the union regex is the cheap pre-gate, and the per-pattern
#            hit counts produce a SHORTLIST (best-scoring first, capped
#            at MAX_SHORTLIST) of the only patterns the brief can
#            plausibly be.
#   Stage 2  the classifier LLM call sees ONLY the shortlisted
#            stanzas from prompts/pattern_classifier.md (plus the
#            mandatory "none" stanza) — the prompt stays roughly the
#            size it was with six patterns, no matter how many are
#            registered.
#
# A brief with zero keyword hits cannot be a templated document → the
# classifier call is skipped entirely (saves a round trip; the AI spec
# path handles it). No extra LLM call, no embeddings, no latency.
MAX_SHORTLIST = 10

_ANY_DIGIT_RE = re.compile(r"\d")


def _keyword_regex(keyword: str) -> re.Pattern:
    """One routing keyword → a word-boundary regex.

    Single words / stems match any English suffix ("budget" →
    "budgets", "budgeting"; "amortiz" → "amortization"), so pattern
    files list stems. Multi-word phrases ("shopping list") match
    verbatim.
    """
    esc = re.escape(keyword.strip().lower())
    if " " in esc:
        return re.compile(rf"\b{esc}\b", re.IGNORECASE)
    return re.compile(rf"\b{esc}\w*", re.IGNORECASE)


# name → one compiled regex per routing keyword
_PATTERN_KEYWORD_RES: Dict[str, List[re.Pattern]] = {
    name: [_keyword_regex(kw) for kw in kws]
    for name, kws in sorted(PATTERN_KEYWORDS.items())
}

# Union of EVERY pattern's routing keywords — the cheap pre-gate.
# Built dynamically from the pattern registry (each pattern carries
# its own keywords now); with no keyword-bearing patterns it becomes
# a never-match regex instead of an accidental match-everything "".
_GATE_ALTS = [
    (
        rf"\b{re.escape(kw.strip().lower())}\w*"
        if " " not in kw.strip()
        else rf"\b{re.escape(kw.strip().lower())}\b"
    )
    for kws in PATTERN_KEYWORDS.values()
    for kw in kws
]
_PATTERN_GATE_RE = re.compile(
    "|".join(sorted(set(_GATE_ALTS))) or r"(?!)",
    re.IGNORECASE,
)


def _shortlist_patterns(brief: str) -> List[str]:
    """Patterns whose routing keywords hit the brief, best first.

    Score = number of distinct keywords that matched. Capped so the
    classifier prompt stays small; deterministic ordering (score desc,
    then name) keeps routing reproducible.
    """
    scored: List[Tuple[int, str]] = []
    for name, res in _PATTERN_KEYWORD_RES.items():
        hits = sum(1 for rx in res if rx.search(brief))
        if hits:
            scored.append((-hits, name))
    scored.sort()
    return [name for _neg, name in scored[:MAX_SHORTLIST]]


# ── Classifier prompt assembly (stanza markers in the .md) ──────────

_STANZA_MARKER_RE = re.compile(
    r"^[ \t]*<!--\s*stanza:\s*([A-Za-z0-9_]+)\s*-->[ \t]*\n(.*?)\n(?=^[ \t]*<!--|\Z)",
    re.MULTILINE | re.DOTALL,
)
_STANZA_END_RE = re.compile(r"^[ \t]*<!--\s*/stanzas\s*-->[ \t]*$", re.MULTILINE)


def _parse_stanza_prompt(prompt_name: str) -> Tuple[str, Dict[str, str], str]:
    """Split a stanza-marked prompt file → (header, {name: stanza}, footer).

    Shared by the classifier (pattern_classifier.md) and the fill-mode
    populator (pattern_populator.md): the .md file stays the single
    source of prompt text (hard rule: prompts live in prompts/*.md);
    this only SELECTS which stanzas a given call sees. A file without
    stanza markers is returned whole as the header (legacy flat
    prompt).
    """
    raw = get_prompt(prompt_name)
    matches = list(_STANZA_MARKER_RE.finditer(raw))
    end = _STANZA_END_RE.search(raw)
    if not matches or end is None:
        return raw, {}, ""
    stanzas: Dict[str, str] = {m.group(1): m.group(2).strip("\n") for m in matches}
    header = raw[: matches[0].start()].rstrip() + "\n\n"
    footer = "\n\n" + raw[end.end() :].lstrip("\n")
    return header, stanzas, footer


def _parse_classifier_prompt() -> Tuple[str, Dict[str, str], str]:
    """pattern_classifier.md → (header, stanzas, footer)."""
    return _parse_stanza_prompt("pattern_classifier")


def _classifier_system_prompt(shortlist: List[str]) -> str:
    """Header + shortlisted stanzas + the mandatory "none" + footer."""
    header, stanzas, footer = _parse_classifier_prompt()
    if not stanzas:
        return header  # legacy flat prompt — no markers in the file
    parts = [header]
    for name in shortlist:
        if name != "none" and name in stanzas:
            parts.append(stanzas[name].rstrip() + "\n\n")
    if "none" in stanzas:
        parts.append(stanzas["none"].rstrip() + "\n\n")
    parts.append(footer.lstrip("\n"))
    return "".join(parts)


# ── Fill mode (guidance → drafted starter content) ─────────────────


def _populator_system_prompt(pattern: str) -> str:
    """Header + the ONE routed pattern's stanza + footer.

    Same small-prompt discipline as the classifier: the populator
    call sees only the stanza of the pattern that was routed, never
    the whole pattern_populator.md. Empty string when the pattern has
    no stanza there (not fillable) — the caller skips the call.
    """
    header, stanzas, footer = _parse_stanza_prompt("pattern_populator")
    if not stanzas or pattern not in stanzas:
        return ""
    return "".join(
        (
            header,
            stanzas[pattern].rstrip() + "\n\n",
            footer.lstrip("\n"),
        )
    )


async def _populate_pattern_params(
    pattern: str,
    params: dict,
    brief: str,
    requirements: str,
    model: Optional[str] = None,
) -> dict:
    """Draft starter params for a fillable pattern from the user's
    guidance (raises on any failure — the caller falls back).

    One small LLM call with prompts/pattern_populator.md (ONLY the
    routed pattern's stanza): the request + today's date + the
    classifier's verbatim extraction go in; a COMPLETE params object
    comes back. The returned dict is the extraction MERGED with the
    drafts ({**params, **filled}): keys the populator restates win,
    keys it omits keep the extracted value — so a populator that
    drafts only meals can never lose an extracted shopping list.
    """
    system = _populator_system_prompt(pattern)
    if not system:
        raise ValueError(f"no populator stanza for pattern {pattern!r}")

    user_content = f"Today's date: {_date.today().isoformat()}\n\nRequest: {brief}"
    if requirements and requirements.strip():
        user_content += f"\n\nAdditional requirements: {requirements.strip()}"
    user_content += (
        "\n\nExtracted parameters (verbatim from the request):\n"
        + json.dumps(params, ensure_ascii=False, default=str)
    )
    user_content += "\n\nRespond with the JSON object now."

    raw = await _call_llm(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        model=model,
    )
    parsed = _extract_json_object(raw)
    if not isinstance(parsed, dict):
        raise ValueError("populator response is not a JSON object")
    filled = parsed.get("params")
    if not isinstance(filled, dict):
        raise ValueError("populator response has no params object")

    merged = dict(params)
    merged.update(filled)
    return merged


def _build_pattern_spec(
    builder,
    pattern: str,
    build_params: dict,
    fallback_params: Optional[dict] = None,
) -> Optional[dict]:
    """Run a pattern builder, with one fallback param set.

    The fill path passes the populated params as build_params and
    the classifier's own extraction as fallback_params: when the
    populated params fail to build, the extraction — which is what
    produced the blank/partial template so far — is the safety net.
    Returns the raw spec dict, or None (AI path fallback). Never
    raises: patterns must never break the tool.
    """
    attempts: List[dict] = [build_params]
    if fallback_params is not None and fallback_params is not build_params:
        attempts.append(fallback_params)
    last_error: Optional[BaseException] = None
    for attempt_params in attempts:
        try:
            return builder(attempt_params)
        except Exception as e:  # noqa: BLE001 — defensive by contract
            last_error = e
            if attempt_params is not attempts[-1]:
                _log(
                    "pattern %r params failed (%s) — retrying with extracted params",
                    pattern,
                    e,
                )
    _log("pattern %r not applied (%s) — falling back to AI spec", pattern, last_error)
    return None


async def _try_pattern_spec(
    brief: str, requirements: str, model: Optional[str] = None
) -> Optional[Tuple[dict, str]]:
    """Route the brief to a deterministic template when one matches.

    One small classifier call (JSON mode) picks the pattern and
    extracts scalar parameters — the part small models are reliable
    at. The workbook spec itself is then built in code by the
    patterns package (app/services/patterns/), where every formula
    reference is computed from the actual layout rows: off-by-N row
    math is impossible.

    FILL MODE: when the classifier answers a FILLABLE pattern
    (registry PATTERN_FILLABLE — the planner/tracker patterns whose
    content is meant to be drafted, never business records) with
    "fill": true, the request stated guidance (goals, preferences,
    constraints) instead of complete records: a SECOND small LLM
    call (prompts/pattern_populator.md) drafts starter list params
    from that guidance before the builder runs — "create a meal
    planner, my goal is weight loss, more protein" returns a
    POPULATED week, not a blank grid. Any populate failure (LLM
    down, unparseable, params the builder rejects) falls back to
    the classifier's own extraction, i.e. the blank/partial template
    fill mode replaced — never worse than before.

    Returns (normalized_spec, pattern_name), or None when no pattern
    matches / params are insufficient / anything fails — the caller
    then falls back to the AI-generated spec path. Never raises.
    """
    if not (_PATTERN_GATE_RE.search(brief) or _ANY_DIGIT_RE.search(brief)):
        return None

    shortlist = _shortlist_patterns(brief)
    if not shortlist:
        # Passed the gate only via a bare digit and no pattern's
        # routing vocabulary is present — not a templated document.
        return None

    user_content = f"Request: {brief}"
    if requirements and requirements.strip():
        user_content += f"\n\nAdditional requirements: {requirements.strip()}"
    user_content += "\n\nRespond with the JSON routing object now."

    try:
        _log("pattern shortlist: %s", ", ".join(shortlist))
        raw = await _call_llm(
            [
                {
                    "role": "system",
                    "content": _classifier_system_prompt(shortlist),
                },
                {"role": "user", "content": user_content},
            ],
            model=model,
        )
        parsed = _extract_json_object(raw)
    except Exception as e:  # unparseable / LLM down → AI path
        _log("pattern routing skipped (classify failed: %s)", e)
        return None

    if not isinstance(parsed, dict):
        return None
    pattern = parsed.get("pattern")
    builder = PATTERN_BUILDERS.get(pattern) if isinstance(pattern, str) else None
    if builder is None:
        return None

    params = parsed.get("params")
    if not isinstance(params, dict):
        params = {}

    # ── fill mode: guidance → drafted starter params ─────────────
    # "fill" arrives as a JSON boolean, but small models sometimes
    # stringify it — accept "true"/"yes"/"1" and reject everything
    # else (a stringified "false" must NOT read as truthy).
    fill_flag = parsed.get("fill")
    if isinstance(fill_flag, str):
        fill_flag = fill_flag.strip().lower() in ("true", "yes", "1")
    build_params = params
    if fill_flag and pattern in PATTERN_FILLABLE:
        try:
            _log("pattern %r fill requested — drafting starter content", pattern)
            build_params = await _populate_pattern_params(
                pattern, params, brief, requirements, model=model
            )
        except Exception as e:  # noqa: BLE001 — fill must never break routing
            _log(
                "pattern %r populate failed (%s) — building with extracted params",
                pattern,
                e,
            )
            build_params = params

    spec = _build_pattern_spec(builder, pattern, build_params, params)
    if spec is None:
        return None

    errors, warnings = validate_workbook_spec(spec)
    if errors:  # should be impossible — belt and suspenders
        _log(
            "pattern %r spec invalid — falling back: %s",
            pattern,
            "; ".join(errors[:4]),
        )
        return None

    normalized = _normalize_spec(spec)
    _log(
        "pattern %r applied: deterministic template spec, %d sheets",
        pattern,
        len(normalized["sheets"]),
    )
    return normalized, pattern


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
                number_format=block.get("number_format")
                or _default_date_format(block["text"]),
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
        alignments: Dict[str, str] = table.get("alignments", {}) or {}
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
        for ci, header in enumerate(headers):
            col_letter = get_column_letter(start_col + ci)
            self._put_styled(
                header_row,
                start_col + ci,
                header,
                font=h_font,
                fill=h_fill,
                border=_ALL_THIN,
                align=Alignment(
                    vertical="center",
                    wrap_text=True,
                    horizontal=alignments.get(col_letter),
                ),
                number_format=numfmts.get(col_letter),
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
                    align=Alignment(
                        vertical="center", horizontal=alignments.get(col_letter)
                    ),
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
            # Percent-only labels ("63%") — everything else OFF.
            # Unset OOXML flags render as ON in LibreOffice, which
            # produced cluttered "Category; Series; $1,800.00; 63%"
            # labels; the legend already carries the category names.
            chart.dataLabels.showPercent = True
            chart.dataLabels.showVal = False
            chart.dataLabels.showCatName = False
            chart.dataLabels.showSerName = False
            chart.dataLabels.showLegendKey = False
            chart.dataLabels.showBubbleSize = False
        elif chart_spec.get("show_values"):
            # Opt-in value labels on non-pie charts (e.g. a gain/loss
            # bar chart reading "+650 / -120" per bar). Same all-OFF
            # discipline as the pie: show ONLY the value.
            chart.dataLabels = DataLabelList()
            chart.dataLabels.showVal = True
            chart.dataLabels.showPercent = False
            chart.dataLabels.showCatName = False
            chart.dataLabels.showSerName = False
            chart.dataLabels.showLegendKey = False
            chart.dataLabels.showBubbleSize = False
        # Value-axis + label number format (e.g. "0%" for fraction
        # series) — applied after the label objects exist above.
        value_numfmt = chart_spec.get("value_numfmt")
        if value_numfmt:
            if chart.dataLabels is not None:
                chart.dataLabels.numFmt = value_numfmt
            if not isinstance(chart, PieChart):
                chart.y_axis.numFmt = value_numfmt
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

    # ── interactivity: dropdowns, conditional formats, protection ──

    def write_data_validation(self) -> None:
        """In-cell dropdown lists (openpyxl DataValidation, type=list).

        Two source modes: an inline value list (formula1 = "a,b,c") or
        a live RANGE reference (formula1 = Categories!$A$5:$A$24) —
        range-sourced dropdowns grow with their source sheet.
        """
        for dv in self.spec.get("data_validation", []):
            try:
                source = dv.get("source_range")
                if isinstance(source, str) and source:
                    formula1 = source
                else:
                    formula1 = '"' + ",".join(dv.get("values") or []) + '"'
                validation = DataValidation(
                    type="list",
                    formula1=formula1,
                    allow_blank=dv.get("allow_blank", True),
                    showErrorMessage=True,
                    errorStyle=dv.get("error_style", "stop"),
                )
                if dv.get("prompt"):
                    validation.showInputMessage = True
                    validation.promptTitle = dv.get("prompt_title") or "Pick a value"
                    validation.prompt = dv["prompt"]
                if dv.get("error"):
                    validation.errorTitle = dv.get("error_title") or "Invalid entry"
                    validation.error = dv["error"]
                validation.add(dv["range"])
                self.ws.add_data_validation(validation)
            except Exception as e:
                _log("data validation skipped (non-fatal): %s", e)

    def write_conditional_formats(self) -> None:
        """Add rules in spec order — earlier rules get priority."""
        for entry in self.spec.get("conditional_formats", []):
            for rule_spec in entry.get("rules", []):
                try:
                    rule = self._build_cf_rule(rule_spec)
                    if rule is not None:
                        self.ws.conditional_formatting.add(entry["range"], rule)
                except Exception as e:
                    _log("conditional format skipped (non-fatal): %s", e)

    @staticmethod
    def _build_cf_rule(rule_spec: dict):
        """One normalized CF rule → an openpyxl Rule object."""
        fill = None
        if rule_spec.get("fill"):
            fill = PatternFill(
                start_color=rule_spec["fill"],
                end_color=rule_spec["fill"],
                fill_type="solid",
            )
        font = None
        if rule_spec.get("font_color") or rule_spec.get("bold"):
            font = Font(
                color=rule_spec.get("font_color") or INK,
                bold=bool(rule_spec.get("bold")),
            )
        stop = bool(rule_spec.get("stop_if_true"))

        if rule_spec["type"] == "formula":
            return FormulaRule(
                formula=[rule_spec["value"]],
                stopIfTrue=stop,
                fill=fill,
                font=font,
            )

        operator = _CF_OPERATORS.get(rule_spec.get("operator", "equal"), "equal")
        value = rule_spec.get("value")
        if rule_spec.get("operator") in ("between", "not_between"):
            formulas = [_cf_operand(v) for v in value]
        else:
            formulas = [_cf_operand(value)]
        return CellIsRule(
            operator=operator,
            formula=formulas,
            stopIfTrue=stop,
            fill=fill,
            font=font,
        )

    def apply_sheet_state(self) -> None:
        if self.spec.get("hidden") is True:
            self.ws.sheet_state = "hidden"

    def apply_protection(self) -> None:
        """Lock every cell except the spec's unlocked ranges.

        No password by default — Review → Unprotect Sheet removes the
        lock, which keeps the template friendly while formulas can't
        be typed over by accident. Selection and cosmetic formatting
        stay allowed.
        """
        prot = self.spec.get("protect")
        if not prot:
            return
        self.ws.protection.sheet = True
        self.ws.protection.formatCells = False
        self.ws.protection.formatColumns = False
        self.ws.protection.formatRows = False
        if prot.get("password"):
            self.ws.protection.set_password(prot["password"])
        for rng in prot.get("unlocked", []):
            try:
                for row in self.ws[rng]:
                    for cell in row:
                        cell.protection = Protection(locked=False)
            except Exception:
                continue

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
        elif spec.get("no_freeze"):
            # explicit opt-out: no frozen rows/columns at all — the
            # sheet is meant to be scrolled freely
            pass
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


def _apply_print_setup(ws) -> None:
    """Write reader-friendly print defaults on a generated worksheet.

    The in-app XLSX preview converts the workbook to PDF with LibreOffice,
    which paginates strictly by the workbook's print setup. A default
    openpyxl sheet has no print setup at all, so a wide table gets sliced
    into narrow A4-portrait columns that read terribly in the viewer.

    Instead: landscape A4, fit to ONE page wide (as many pages tall as
    the data needs), and repeat the frozen header region (title + column
    headers — the same rows the freeze panes keep on screen) at the top
    of every printed page, exactly like a proper print preview.
    """
    # fitToWidth/fitToHeight only take effect with fitToPage enabled.
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1  # never slice columns across pages
    ws.page_setup.fitToHeight = 0  # 0 = as many pages tall as needed

    # Repeat the frozen header region (rows above the freeze anchor) on
    # every page so multi-page tables stay readable in the preview.
    # Freeze anchors look like "A4" / "B4" / "$A$4" / "B4:C9" — parse the
    # row from the first cell of the range.
    freeze = ws.freeze_panes
    if freeze:
        m = re.match(r"^[A-Za-z]+(\d+)", str(freeze).split(":")[0].replace("$", ""))
        if m:
            anchor_row = int(m.group(1))
            if anchor_row > 1:
                ws.print_title_rows = f"1:{anchor_row - 1}"


def _build_xlsx(spec: dict, output_path: Path) -> None:
    """Deterministic JSON spec → .xlsx (pure openpyxl, no LLM)."""
    wb = Workbook()
    default_ws = wb.active
    wb.remove(default_ws)

    writers = []
    for sheet_spec in spec["sheets"]:
        ws = wb.create_sheet(title=sheet_spec["name"])
        writer = _SheetWriter(ws, sheet_spec, sheet_spec["name"])
        writers.append(writer)
        writer.write_tables()  # tables first (structural backbone)
        writer.write_text_blocks()  # labels/headings
        writer.write_formulas()  # override anything beneath
        writer.write_notes()  # documentation under content
        writer.write_merged_cells()
        writer.write_data_validation()  # dropdown lists
        writer.write_conditional_formats()  # done/missed/today highlighting
        writer.apply_layout()
        writer.apply_sheet_state()  # hidden helper sheets
        writer.apply_protection()  # unlock the user-editable ranges LAST
        _apply_print_setup(ws)  # print-friendly pagination for the XLSX preview

    # Charts LAST, after every sheet exists: a chart may reference a
    # range on ANOTHER sheet (e.g. the portfolio overview's allocation
    # pie reading the Holdings sheet, which is built later), and an
    # openpyxl Reference needs the target worksheet object at build
    # time. Charts are floating objects — writing them after notes,
    # merges and layout changes nothing on the grid.
    for writer in writers:
        writer.write_charts()

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


def _workbook_stats(spec: dict) -> dict:
    """Summarize a (normalized) workbook spec for the agent's response.

    Counts sheets, tables, charts and live formulas and collects the
    sheet names — the same "what did I just build" context report /
    pptx generation hand back to the agent so it can tell the user
    what the deliverable contains.
    """
    sheets = spec.get("sheets") or []
    table_count = 0
    chart_count = 0
    formula_count = 0

    def _count_formulas(texts: Any) -> int:
        count = 0
        for v in texts or []:
            if isinstance(v, str) and v.lstrip().startswith("="):
                count += 1
        return count

    for sheet in sheets:
        tables = sheet.get("tables") or []
        table_count += len(tables)
        chart_count += len(sheet.get("charts") or [])
        for table in tables:
            for row in table.get("rows") or []:
                if isinstance(row, list):
                    formula_count += _count_formulas(row)
            formula_count += _count_formulas(table.get("total_row"))
        for block in sheet.get("text_blocks") or []:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str) and text.lstrip().startswith("="):
                    formula_count += 1
        formula_count += _count_formulas(
            [f for _cell, f in (_normalize_formula_pairs(sheet.get("formulas")) or [])]
        )

    return {
        "sheet_names": [str(s.get("name") or "") for s in sheets],
        "table_count": table_count,
        "chart_count": chart_count,
        "formula_count": formula_count,
    }


def _workbook_summary_text(spec: dict, pattern_name: Optional[str]) -> str:
    """Human-readable workbook summary for the agent / user message."""
    stats = _workbook_stats(spec)
    parts = [
        f"{max(len(stats['sheet_names']), 1)} sheets",
        f"{stats['table_count']} tables",
    ]
    if stats["chart_count"]:
        parts.append(f"{stats['chart_count']} charts")
    parts.append(f"{stats['formula_count']} live formulas")
    summary = ", ".join(parts)
    names = [n for n in stats["sheet_names"] if n]
    if names:
        summary += f" ({', '.join(names[:6])})"
    if pattern_name:
        summary += (
            f" — built from the built-in {pattern_name} template, so all "
            "formulas are code-generated and arithmetically correct"
        )
    return summary


async def generate_spreadsheet(
    brief: str,
    requirements: str = "",
    model: Optional[str] = None,
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

    # 1. Deterministic template first (amortization / invoice / budget
    #    planner): formulas code-generated from actual layout rows.
    #    No match → specialized Excel AI call → validated spec.
    pattern_name: Optional[str] = None
    pattern_result = await _try_pattern_spec(brief, requirements, model=model)
    if pattern_result is not None:
        spec, pattern_name = pattern_result
    else:
        spec = await _generate_workbook_json(brief, requirements, model=model)

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

    # 4. Workbook summary — same "what did I build" context report /
    #    pptx generation return, so the agent can describe the
    #    deliverable to the user (sheet/table/chart/formula counts).
    stats = _workbook_stats(spec)
    summary_text = _workbook_summary_text(spec, pattern_name)

    return {
        "type": "excel",
        "format": "xlsx",
        "filename": filename,
        "file_path": rel_path,
        "download_url": f"/api/reports/{report_id}/download",
        "report_id": report_id,
        "created_at": int(time.time()),
        "sheet_count": len(spec["sheets"]),
        "table_count": stats["table_count"],
        "chart_count": stats["chart_count"],
        "formula_count": stats["formula_count"],
        "sheet_names": stats["sheet_names"],
        "summary": summary_text,
        "pattern": pattern_name,
    }
