"""
Cash flow forecast pattern — month-by-month projection with an
opening/closing balance chain over a horizon.

Contract (stanza "cashflow_forecast" in prompts/pattern_classifier.md):
  business_name, start_month ("YYYY-MM"), opening_balance, months,
  currency, monthly_income [{source, amount}], monthly_expenses
  [{category, amount}], extra_items [{month, description, amount,
  direction "in"|"out"}], notes.
  months null -> 12; opening_balance 0 when not stated; monthly lines
  repeat every month; extra_items are one-off amounts in a specific
  month (1 = first projected month); direction "in" adds cash, "out"
  removes it.

Layout (Forecast sheet):
  row 1      title text block
  rows 3-5   inputs: B3 opening balance, B4 start month, B5 horizon
  row 7      Monthly Income table (title) -> data -> total row
  below      Monthly Expenses table -> data -> total row
  below      One-Off Items table (month date via EDATE, description,
             amount, direction dropdown) when extras exist
  below      Cash Flow grid: Month | Income | Expenses | Extra In |
             Extra Out | Net | Opening | Closing, one row per month
  Summary sheet (steel tab) mirrors the key numbers via cross-sheet
  references.

Every formula reference is computed from the row numbers emitted here:
  Income column  -> the income table's total row cell (recurs monthly)
  Extra In/Out   -> SUMIFS over the one-off table keyed by month date
                    + direction
  Opening        -> =$B$3 on the first month, previous Closing after
  Closing        -> Opening + Net
  Month labels   -> =EDATE($B$4, i) so the whole grid shifts when the
                    start month input changes.
No ROUND() anywhere — display rounding is the number format's job.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from app.services.patterns.utils import (
    MONTH_FMT,
    MONEY_FMT,
    _currency_fmt,
    _first_of_next_month,
    _pick,
    to_int,
    to_number,
)

# Registry key — must match the pattern stanza in
# prompts/pattern_classifier.md.
PATTERN_NAME = "cashflow_forecast"

PATTERN_DESCRIPTION = (
    "Month-by-month cash flow projection with a running opening/closing "
    "balance, recurring monthly income & expenses and one-off items."
)

# Routing keywords/stems — drive the cheap pre-gate and the classifier
# shortlist (see excel_gen._shortlist_patterns).
PATTERN_KEYWORDS = (
    "cash flow",
    "cashflow",
    "cash forecast",
    "cash projection",
    "forecast",
    "projection",
    "projected",
    "runway",
    "burn rate",
    "opening balance",
    "closing balance",
    "monthly projection",
)

MAX_FORECAST_MONTHS = 36
MAX_FLOW_LINES = 100
MAX_EXTRA_ITEMS = 100
MAX_ABS_MONEY = 1e12

# Status palette (Excel classic) — shared visual identity.
CF_RED_FILL = "FFC7CE"
CF_RED_TEXT = "9C0006"
CF_AMBER_FILL = "FFF2CC"
CF_AMBER_TEXT = "7F6000"

_START_MONTH_RE = re.compile(r"^(\d{4})-(\d{1,2})")


def _clean_text(value: Any, limit: int = 200) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()[:limit]
    return ""


def _coerce_start_month(value: Any) -> Optional[Tuple[int, int]]:
    """start_month param ("YYYY-MM", ISO date, or None) -> (year, month)."""
    if not isinstance(value, str):
        return None
    m = _START_MONTH_RE.match(value.strip())
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    if not (1 <= mo <= 12) or not (1990 <= y <= 2999):
        return None
    return y, mo


def _coerce_flow_lines(
    params: dict, aliases: Tuple[str, ...], label_keys: Tuple[str, ...]
) -> List[Tuple[str, float]]:
    """Recurring monthly lines -> [(label, amount)]."""
    raw = None
    for key in aliases:
        if isinstance(params.get(key), list):
            raw = params[key]
            break
    if raw is None:
        return []
    lines: List[Tuple[str, float]] = []
    for entry in raw[:MAX_FLOW_LINES]:
        if isinstance(entry, dict):
            label = _pick(entry, *label_keys)
            amount = to_number(_pick(entry, "amount", "value", "monthly_amount"))
        elif isinstance(entry, list) and len(entry) >= 2:
            label = entry[0]
            amount = to_number(entry[1])
        else:
            continue
        label = _clean_text(label, 120)
        if not label or amount is None:
            continue
        if amount < 0:
            amount = 0.0
        if amount > MAX_ABS_MONEY:
            amount = MAX_ABS_MONEY
        lines.append((label, amount))
    return lines


def _normalize_direction(value: Any) -> str:
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("in", "income", "inflow", "receipt", "credit", "add"):
            return "in"
        if s in ("out", "expense", "outflow", "payment", "debit", "spend"):
            return "out"
    return "out"


def _coerce_extra_items(params: dict, months: int) -> List[Tuple[int, str, float, str]]:
    """One-off items -> [(month_1based, label, amount, direction)]."""
    raw = _pick(params, "extra_items", "one_off_items", "one_offs", "extra")
    if not isinstance(raw, list):
        return []
    items: List[Tuple[int, str, float, str]] = []
    for entry in raw[:MAX_EXTRA_ITEMS]:
        if isinstance(entry, dict):
            month = to_int(_pick(entry, "month", "month_number", "month_index"))
            label = _clean_text(
                _pick(entry, "description", "desc", "item", "name", "label"), 120
            )
            amount = to_number(_pick(entry, "amount", "value", "cost"))
            direction = _pick(entry, "direction", "type", "flow")
        elif isinstance(entry, list) and len(entry) >= 3:
            month = to_int(entry[0])
            label = _clean_text(entry[1], 120)
            amount = to_number(entry[2])
            direction = entry[3] if len(entry) >= 4 else None
        else:
            continue
        if amount is None:
            continue  # nothing to book — drop rather than invent
        if month is None:
            month = 1  # unspecified month -> first projected month
        month = max(1, min(months, month))
        if not label:
            label = "One-off item"
        amount = abs(amount)
        if amount > MAX_ABS_MONEY:
            amount = MAX_ABS_MONEY
        items.append((month, label, amount, _normalize_direction(direction)))
    return items


def coerce_cashflow_forecast_params(params: dict) -> dict:
    """Validate + normalize classifier params; raises ValueError."""
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    business_name = _clean_text(
        _pick(params, "business_name", "name", "company", "business"), 120
    )

    start = _coerce_start_month(
        _pick(params, "start_month", "first_month", "start", "start_date")
    )
    if start is None:
        y, m = _first_of_next_month()
        start = (y, m)

    opening = to_number(
        _pick(
            params,
            "opening_balance",
            "start_balance",
            "starting_balance",
            "opening_cash",
            "cash_on_hand",
        )
    )
    if opening is None:
        opening = 0.0  # stanza: 0 when not stated
    if abs(opening) > MAX_ABS_MONEY:
        opening = MAX_ABS_MONEY if opening > 0 else -MAX_ABS_MONEY

    months = to_int(
        _pick(params, "months", "horizon", "horizon_months", "num_months", "duration")
    )
    if months is None:
        months = 12  # stanza: months null -> 12
    months = max(1, min(months, MAX_FORECAST_MONTHS))

    income = _coerce_flow_lines(
        params,
        ("monthly_income", "income", "income_sources", "revenue"),
        ("source", "name", "label"),
    )
    expenses = _coerce_flow_lines(
        params,
        ("monthly_expenses", "expenses", "expense_categories", "costs"),
        ("category", "name", "label", "type"),
    )
    extras = _coerce_extra_items(params, months)

    if not income and not expenses and not extras:
        raise ValueError("no monthly income, expense or one-off lines")

    notes = _pick(params, "notes", "note")
    notes = notes.strip()[:1000] if isinstance(notes, str) and notes.strip() else ""

    return {
        "business_name": business_name,
        "start_month": start,
        "opening_balance": opening,
        "months": months,
        "monthly_income": income,
        "monthly_expenses": expenses,
        "extra_items": extras,
        "currency": _currency_fmt(_pick(params, "currency")),
        "notes": notes,
    }


def build_cashflow_forecast_spec(params: dict) -> dict:
    """Cash flow forecast workbook — every formula code-generated from
    the layout rows emitted below (refs can never drift)."""
    p = coerce_cashflow_forecast_params(params)
    name = p["business_name"]
    sy, sm = p["start_month"]
    opening = p["opening_balance"]
    months = p["months"]
    income = p["monthly_income"]
    expenses = p["monthly_expenses"]
    extras = p["extra_items"]
    money = p["currency"] or MONEY_FMT

    blocks: List[dict] = [
        {
            "cell": "A1",
            "text": f"{name} Cash Flow Forecast" if name else "Cash Flow Forecast",
            "bold": True,
            "font_size": 14,
        },
        {"cell": "A3", "text": "Opening Balance"},
        {"cell": "B3", "text": opening, "bold": True, "number_format": money},
        {"cell": "A4", "text": "Start Month"},
        {
            "cell": "B4",
            "text": f"{sy:04d}-{sm:02d}-01",
            "number_format": MONTH_FMT,
        },
        {"cell": "A5", "text": "Horizon (Months)"},
        {"cell": "B5", "text": months},
    ]

    tables: List[dict] = []

    # ── Recurring monthly lines ───────────────────────────────────────
    row = 7
    income_total_cell = "0"
    if income:
        first = row + 2
        last = first + len(income) - 1
        inc_total = last + 1
        income_total_cell = f"B{inc_total}"
        tables.append(
            {
                "start_cell": f"A{row}",
                "title": "Monthly Income (repeats every month)",
                "headers": ["Source", "Amount"],
                "rows": [[label, amount] for label, amount in income],
                "number_formats": {"B": money},
                "total_row": [
                    "Total Monthly Income",
                    "=SUM(B{first_row}:B{last_row})",
                ],
            }
        )
        row = inc_total + 2
    else:
        blocks.append(
            {
                "cell": f"A{row}",
                "text": "No monthly income lines provided",
                "italic": True,
            }
        )
        row += 2

    expense_total_cell = "0"
    if expenses:
        first = row + 2
        last = first + len(expenses) - 1
        exp_total = last + 1
        expense_total_cell = f"B{exp_total}"
        tables.append(
            {
                "start_cell": f"A{row}",
                "title": "Monthly Expenses (repeat every month)",
                "headers": ["Category", "Amount"],
                "rows": [[label, amount] for label, amount in expenses],
                "number_formats": {"B": money},
                "total_row": [
                    "Total Monthly Expenses",
                    "=SUM(B{first_row}:B{last_row})",
                ],
            }
        )
        row = exp_total + 2
    else:
        blocks.append(
            {
                "cell": f"A{row}",
                "text": "No monthly expense lines provided",
                "italic": True,
            }
        )
        row += 2

    # ── One-off items (extra_items) ───────────────────────────────────
    # Month date is a live EDATE off the start-month input, so editing
    # the month number or B4 keeps the SUMIFS keys in sync.
    ex_first: Optional[int] = None
    ex_last: Optional[int] = None
    if extras:
        first = row + 2
        last = first + len(extras) - 1
        ex_first, ex_last = first, last
        ex_rows: List[List[Any]] = [
            [f"=EDATE($B$4,{m - 1})", label, amount, direction]
            for m, label, amount, direction in extras
        ]
        tables.append(
            {
                "start_cell": f"A{row}",
                "title": "One-Off Items (specific months only)",
                "headers": ["Month", "Description", "Amount", "Direction"],
                "rows": ex_rows,
                "number_formats": {"A": MONTH_FMT, "C": money},
                "alignments": {"D": "center"},
            }
        )
        extra_in_formula = (
            f"=SUMIFS($C${first}:$C${last},$A${first}:$A${last},"
            f'$A{{r}},$D${first}:$D${last},"in")'
        )
        extra_out_formula = (
            f"=SUMIFS($C${first}:$C${last},$A${first}:$A${last},"
            f'$A{{r}},$D${first}:$D${last},"out")'
        )
        row = last + 2
    else:
        extra_in_formula = "=0"
        extra_out_formula = "=0"
        row += 1

    # ── Cash flow grid (the forecast itself) ─────────────────────────
    fc_anchor = row
    fc_header = fc_anchor + 1  # table carries a title
    fc_first = fc_header + 1
    fc_last = fc_first + months - 1
    fc_total = fc_last + 1

    fc_rows: List[List[Any]] = []
    for i in range(months):
        r = fc_first + i
        fc_rows.append(
            [
                f"=EDATE($B$4,{i})",
                f"={income_total_cell}",
                f"={expense_total_cell}",
                extra_in_formula.format(r=r),
                extra_out_formula.format(r=r),
                f"=B{r}+D{r}-C{r}-E{r}",
                "=$B$3" if i == 0 else f"=H{r - 1}",
                f"=G{r}+F{r}",
            ]
        )

    tables.append(
        {
            "start_cell": f"A{fc_anchor}",
            "title": "Cash Flow by Month",
            "headers": [
                "Month",
                "Income",
                "Expenses",
                "Extra In",
                "Extra Out",
                "Net Cash Flow",
                "Opening Balance",
                "Closing Balance",
            ],
            "rows": fc_rows,
            "number_formats": {
                "A": MONTH_FMT,
                "B": money,
                "C": money,
                "D": money,
                "E": money,
                "F": money,
                "G": money,
                "H": money,
            },
            "total_row": [
                "Total",
                "=SUM(B{first_row}:B{last_row})",
                "=SUM(C{first_row}:C{last_row})",
                "=SUM(D{first_row}:D{last_row})",
                "=SUM(E{first_row}:E{last_row})",
                "=SUM(F{first_row}:F{last_row})",
                "",
                "",
            ],
        }
    )

    notes = p["notes"] or (
        "Template-built forecast — everything is live. Edit B3 (opening "
        "balance), B4 (start month) or B5 (horizon) and the whole grid "
        "recalculates: months come from EDATE(B4, n), income/expenses "
        "repeat every month from their tables, one-off items match by "
        "their month date + direction, opening balance chains from the "
        "previous closing balance."
    )

    sheet_spec: Dict[str, Any] = {
        "name": "Forecast",
        "tab_color": "16304F",
        "freeze_panes": f"A{fc_first}",
        "column_widths": {
            "A": 13,
            "B": 14,
            "C": 14,
            "D": 12,
            "E": 12,
            "F": 15,
            "G": 16,
            "H": 16,
        },
        "text_blocks": blocks,
        "tables": tables,
        "charts": [
            {
                "type": "line",
                "title": "Closing Balance by Month",
                "anchor": "J3",
                "width": 16,
                "height": 9,
                "categories_range": f"Forecast!A{fc_first}:A{fc_last}",
                "series": [
                    {
                        "name": "Closing Balance",
                        "values_range": f"Forecast!H{fc_first}:H{fc_last}",
                    }
                ],
                "value_numfmt": money,
            }
        ],
        "conditional_formats": [
            # overdrawn closing balance — red
            {
                "range": f"H{fc_first}:H{fc_last}",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "less_than",
                        "value": 0,
                        "fill": CF_RED_FILL,
                        "font_color": CF_RED_TEXT,
                        "bold": True,
                    }
                ],
            },
            # net-negative months (spending above inflows) — amber
            {
                "range": f"F{fc_first}:F{fc_last}",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "less_than",
                        "value": 0,
                        "fill": CF_AMBER_FILL,
                        "font_color": CF_AMBER_TEXT,
                    }
                ],
            },
        ],
        "notes": notes,
    }

    if ex_first is not None:
        sheet_spec["data_validation"] = [
            {
                "range": f"D{ex_first}:D{ex_last}",
                "values": ["in", "out"],
                "allow_blank": False,
                "prompt_title": "Direction",
                "prompt": "in = adds cash this month, out = removes it.",
                "error_title": "Invalid direction",
                "error": "Use in or out (the forecast SUMIFS keys on it).",
                "error_style": "stop",
            }
        ]

    # ── Summary sheet (steel tab, cross-sheet references) ────────────
    summary: Dict[str, Any] = {
        "name": "Summary",
        "tab_color": "1B3A5C",
        "column_widths": {"A": 30, "B": 16},
        "tables": [
            {
                "start_cell": "A3",
                "title": "Forecast Summary",
                "headers": ["Item", "Value"],
                "rows": [
                    ["Opening Balance", "=Forecast!$B$3"],
                    ["Total Income (horizon)", f"=Forecast!$B${fc_total}"],
                    ["Total Expenses (horizon)", f"=Forecast!$C${fc_total}"],
                    ["Total One-Off In", f"=Forecast!$D${fc_total}"],
                    ["Total One-Off Out", f"=Forecast!$E${fc_total}"],
                    ["Net Change", f"=Forecast!$F${fc_total}"],
                    ["Final Closing Balance", f"=Forecast!$H${fc_last}"],
                    ["Lowest Balance", f"=MIN(Forecast!$H${fc_first}:$H${fc_last})"],
                    [
                        "Negative-Balance Months",
                        (
                            f'=COUNTIF(Forecast!$H${fc_first}:$H${fc_last},"<0")'
                            f'&" of "&Forecast!$B$5&" months"'
                        ),
                    ],
                    [
                        "Average Monthly Net",
                        (
                            f"=IF(Forecast!$B$5>0,Forecast!$F${fc_total}/"
                            f'Forecast!$B$5,"n/a")'
                        ),
                    ],
                ],
                "number_formats": {"B": money},
            }
        ],
        "notes": (
            "All values reference the Forecast sheet — change the inputs "
            "or the recurring lines there and this summary updates."
        ),
    }

    return {
        "filename": "cashflow_forecast.xlsx",
        "sheets": [sheet_spec, summary],
    }


# ── Standard pattern entry points (used by the dynamic registry) ──────
coerce_params = coerce_cashflow_forecast_params
build_spec = build_cashflow_forecast_spec
