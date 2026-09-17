"""
Savings goal pattern — ONE savings target with monthly deposits.

Deterministic: the classifier extracts scalar parameters (target, what's
saved so far, monthly deposit, optional annual return); this module
builds the whole workbook spec in code, with every formula reference
computed from the actual row numbers of the layout it emits —
off-by-N row math is impossible by construction.

Layout (Savings sheet):
  row 1      title
  rows 2-6   inputs: B2 target, B3 current savings, B4 monthly deposit,
             B5 annual return, B6 start date
  row 8      table headers (start_cell A8, no table title)
  rows 9..   data: Month | Date | Opening Balance | Deposit | Interest |
             Closing Balance | % of Goal | Target
  row N+1    totals (SUM over deposits + interest)
  blocks     months-to-goal / goal-reached date / starting progress
  table      Summary (money rows referencing the schedule's totals)

The balance chain references the PREVIOUS row's closing balance (first
month pins to the current-savings input), interest = opening x
annual-return / 12 — monthly compounding on the opening balance with
the deposit added at month end. Months-to-goal is a live COUNTIF over
the closing-balance column (the series is monotonic, so the count of
closings below the target + 1 is the first month that reaches it).
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.services.patterns.utils import (
    MONEY_FMT,
    MONTH_FMT,
    PCT_FMT,
    _currency_fmt,
    _first_of_next_month,
    _next_month,
    _pick,
    to_iso_date,
    to_number,
    to_rate,
)

# Registry key — must match the pattern stanza in
# prompts/pattern_classifier.md.
PATTERN_NAME = "savings_goal"

PATTERN_DESCRIPTION = (
    "Single savings goal projection: monthly deposits with optional "
    "annual-return compounding, months-to-goal, live totals and a "
    "growth chart."
)

# Routing keywords/stems — drive the cheap pre-gate and the classifier
# shortlist (see excel_gen._shortlist_patterns).
PATTERN_KEYWORDS = (
    "savings goal",
    "save for",
    "saving up",
    "savings target",
    "savings plan",
    "savings projection",
    "savings calculator",
    "goal amount",
    "target amount",
    "monthly deposit",
    "monthly saving",
    "emergency fund",
    "nest egg",
    "rainy day fund",
    "down payment",
    "goal",
)

# Hard cap on the projection horizon (30 years) — keeps the sheet a
# projection, not an infinite schedule.
MAX_PROJECTION_MONTHS = 360


def _simulate_months_to_goal(
    current: float, monthly: float, rate: float, target: float, cap: int
) -> int | None:
    """First projected month (1-based) whose closing balance >= target.

    Closing(m) = opening x (1 + rate/12) + deposit, opening(m) =
    closing(m-1), opening(1) = current savings. Identical to the
    workbook's formula chain — used ONLY to size the table's horizon
    (layout math); the cells themselves stay live formulas.
    """
    balance = current
    for month in range(1, cap + 1):
        balance = balance * (1.0 + rate / 12.0) + monthly
        if balance >= target:
            return month
    return None


def coerce_savings_goal_params(params: dict) -> dict:
    """Validate + normalize classifier params; raises ValueError.

    Accepts alias keys, quoted/currency numbers and percent strings.
    Required: a positive target_amount and a positive monthly
    contribution — without deposits there is no projection to build
    (the caller falls back to the AI spec path).
    """
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    target = to_number(_pick(params, "target_amount", "goal_amount", "target"))
    if target is None or target <= 0:
        raise ValueError("target_amount missing or not positive")
    target = min(float(target), 1e12)

    current = to_number(
        _pick(params, "current_saved", "saved_so_far", "current_savings", "saved")
    )
    if current is None or current < 0:
        current = 0.0
    current = min(float(current), 1e12)

    monthly = to_number(
        _pick(
            params,
            "monthly_contribution",
            "monthly_deposit",
            "monthly_savings",
            "contribution",
        )
    )
    if monthly is None or monthly <= 0:
        raise ValueError("monthly_contribution missing or not positive")
    monthly = min(float(monthly), 1e9)

    rate = to_rate(
        _pick(
            params,
            "annual_return",
            "return_rate",
            "annual_interest_rate",
            "interest_rate",
        )
    )
    if rate is None:
        rate = 0.0
    rate = min(max(float(rate), 0.0), 1.0)

    start = to_iso_date(_pick(params, "start_date", "date"))
    if start is None:
        y, m = _first_of_next_month()
        start = f"{y:04d}-{m:02d}-01"

    goal_name = _pick(params, "goal_name", "goal", "name")
    goal_name = (
        str(goal_name).strip()[:100]
        if goal_name is not None and str(goal_name).strip()
        else "Savings Goal"
    )

    notes = _pick(params, "notes", "note")
    notes = notes.strip()[:1000] if isinstance(notes, str) and notes.strip() else None

    return {
        "goal_name": goal_name,
        "target_amount": target,
        "current_saved": current,
        "monthly_contribution": monthly,
        "annual_return": rate,
        "start_date": start,
        "currency": _currency_fmt(_pick(params, "currency")),
        "notes": notes,
    }


def build_savings_goal_spec(params: dict) -> dict:
    """Savings goal projection — every formula code-generated.

    No ROUND() in the formulas on purpose: display rounding is the
    number format's job; rounding inside formulas accumulates
    decimal-level drift.
    """
    p = coerce_savings_goal_params(params)
    goal_name = p["goal_name"]
    target, current = p["target_amount"], p["current_saved"]
    monthly, rate = p["monthly_contribution"], p["annual_return"]
    start_iso = p["start_date"]
    money = p["currency"] or MONEY_FMT

    # Layout math — horizon sized from the simulated chain (rows only;
    # every cell stays a live formula).
    months_to_goal = _simulate_months_to_goal(
        current, monthly, rate, target, MAX_PROJECTION_MONTHS
    )
    if months_to_goal is None:
        horizon = 24  # unreachable within the cap — show "Not reached"
    else:
        horizon = max(12, min(months_to_goal + 6, MAX_PROJECTION_MONTHS))

    first_data = 9
    last_data = first_data + horizon - 1
    total_row = last_data + 1

    # Input rows (text blocks above the table — the schedule's formulas
    # reference these cells, so edits recalculate the whole sheet).
    target_row, current_row, deposit_row, rate_row, start_row = 2, 3, 4, 5, 6

    sy, sm, _sd = (int(x) for x in start_iso.split("-"))
    rows: List[List[Any]] = []
    y, mo = sy, sm
    for i in range(horizon):
        r = first_data + i
        rows.append(
            [
                i + 1,
                f"{y:04d}-{mo:02d}-01",
                (f"=$B${current_row}" if i == 0 else f"=F{r - 1}"),
                f"=$B${deposit_row}",
                f"=C{r}*$B${rate_row}/12",
                f"=C{r}+D{r}+E{r}",
                f'=IF($B${target_row}>0,F{r}/$B${target_row},"n/a")',
                f"=$B${target_row}",
            ]
        )
        y, mo = _next_month(y, mo)

    blocks: List[dict] = [
        {
            "cell": "A1",
            "text": f"{goal_name} — Savings Goal",
            "bold": True,
            "font_size": 14,
        },
        {"cell": f"A{target_row}", "text": "Target Amount"},
        {"cell": f"B{target_row}", "text": target, "number_format": money},
        {"cell": f"A{current_row}", "text": "Current Savings"},
        {"cell": f"B{current_row}", "text": current, "number_format": money},
        {"cell": f"A{deposit_row}", "text": "Monthly Deposit"},
        {"cell": f"B{deposit_row}", "text": monthly, "number_format": money},
        {"cell": f"A{rate_row}", "text": "Annual Return"},
        {"cell": f"B{rate_row}", "text": rate, "number_format": PCT_FMT},
        {"cell": f"A{start_row}", "text": "Start Date"},
        {
            "cell": f"B{start_row}",
            "text": start_iso,
            "number_format": "yyyy-mm-dd",
        },
    ]

    # Goal readouts — mixed value types (count / date / percent) live as
    # text blocks so each keeps its own number format.
    months_row = total_row + 2
    date_row = months_row + 1
    progress_row = months_row + 2
    blocks += [
        {"cell": f"A{months_row}", "text": "Months to Goal", "bold": True},
        {
            "cell": f"B{months_row}",
            "text": (
                f'=IF(COUNTIF(F{first_data}:F{last_data},">="&$B${target_row})=0,'
                f'"Not reached",'
                f'COUNTIF(F{first_data}:F{last_data},"<"&$B${target_row})+1)'
            ),
            "bold": True,
            "number_format": "0",
        },
        {"cell": f"A{date_row}", "text": "Goal Reached Date"},
        {
            "cell": f"B{date_row}",
            "text": (
                f"=IF(ISNUMBER(B{months_row}),"
                f'INDEX(B{first_data}:B{last_data},B{months_row}),"—")'
            ),
            "number_format": "yyyy-mm-dd",
        },
        {"cell": f"A{progress_row}", "text": "Starting Progress"},
        {
            "cell": f"B{progress_row}",
            "text": f'=IF($B${target_row}>0,$B${current_row}/$B${target_row},"n/a")',
            "number_format": PCT_FMT,
        },
    ]

    summary_start = progress_row + 2

    tables: List[dict] = [
        {
            "start_cell": "A8",
            "headers": [
                "Month",
                "Date",
                "Opening Balance",
                "Deposit",
                "Interest",
                "Closing Balance",
                "% of Goal",
                "Target",
            ],
            "rows": rows,
            "number_formats": {
                "A": "0",
                "B": MONTH_FMT,
                "C": money,
                "D": money,
                "E": money,
                "F": money,
                "G": PCT_FMT,
                "H": money,
            },
            "alignments": {"A": "center"},
            "total_row": [
                "Total",
                "",
                "",
                "=SUM(D{first_row}:D{last_row})",
                "=SUM(E{first_row}:E{last_row})",
                "",
                "",
                "",
            ],
        },
        {
            "start_cell": f"A{summary_start}",
            "title": "Summary",
            "headers": ["Item", "Value"],
            "rows": [
                ["Total Deposits", f"=D{total_row}"],
                ["Total Interest Earned", f"=E{total_row}"],
                ["Final Projected Balance", f"=F{last_data}"],
            ],
            "number_formats": {"B": money},
        },
    ]

    # Growth chart: closing balance climbing toward the flat target
    # line — the crossing IS the goal being reached.
    charts: List[dict] = [
        {
            "type": "line",
            "title": f"{goal_name} Growth",
            "anchor": "J2",
            "width": 16,
            "height": 9,
            "categories_range": f"Savings!B{first_data}:B{last_data}",
            "series": [
                {
                    "name": "Closing Balance",
                    "values_range": f"Savings!F{first_data}:F{last_data}",
                },
                {
                    "name": "Target",
                    "values_range": f"Savings!H{first_data}:H{last_data}",
                },
            ],
            "value_numfmt": money,
        }
    ]

    # Status semantics: closing balance at/above target turns green;
    # less than half-way turns amber.
    conditional_formats: List[dict] = [
        {
            "range": f"F{first_data}:F{last_data}",
            "rules": [
                {
                    "type": "formula",
                    "formula": f"F{first_data}>=$B${target_row}",
                    "fill": "C6EFCE",
                    "font_color": "1E4620",
                    "bold": True,
                }
            ],
        },
        {
            "range": f"G{first_data}:G{last_data}",
            "rules": [
                {
                    "type": "formula",
                    "formula": f"G{first_data}<0.5",
                    "fill": "FFF2CC",
                    "font_color": "7F6000",
                }
            ],
        },
    ]

    sheet_notes = p["notes"] or (
        "Edit B2 (target), B3 (current savings), B4 (monthly deposit), B5 "
        "(annual return) or B6 (start date) and the whole projection "
        "recalculates: opening = previous closing (first month = current "
        "savings), interest = opening x annual return / 12, closing = "
        "opening + deposit + interest. Months to Goal counts the first "
        "projected month whose closing balance reaches the target; the "
        "interest assumes deposits land at month end."
    )

    sheet: Dict[str, Any] = {
        "name": "Savings",
        "tab_color": "16304F",
        "freeze_panes": "A9",
        "column_widths": {
            "A": 10,
            "B": 12,
            "C": 16,
            "D": 14,
            "E": 14,
            "F": 17,
            "G": 12,
            "H": 16,
        },
        "text_blocks": blocks,
        "tables": tables,
        "charts": charts,
        "conditional_formats": conditional_formats,
        "notes": sheet_notes,
    }

    return {
        "filename": "savings_goal.xlsx",
        "sheets": [sheet],
    }


# ── Standard pattern entry points (used by the dynamic registry) ──────

coerce_params = coerce_savings_goal_params
build_spec = build_savings_goal_spec
