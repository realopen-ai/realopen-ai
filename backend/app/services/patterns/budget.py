"""
Budget pattern — income & expense plan / planner.

Deterministic: totals are SUM() over the exact data rows; the summary
references the two total-row cells; savings rate is a live TEXT()
formula. A pie chart visualizing the expense mix floats NEXT TO the
tables (anchored in column D). No freeze panes on purpose — the user
scrolls the budget freely; nothing stays pinned.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.services.patterns.utils import (
    MONEY_FMT,
    _currency_fmt,
    _pick,
    to_number,
)

# Registry key — must match the pattern stanza in
# prompts/pattern_classifier.md.
PATTERN_NAME = "budget"

PATTERN_DESCRIPTION = (
    "Income & expense plan / planner with live totals, net and savings "
    "rate, plus an expense-mix pie chart."
)

MAX_BUDGET_LINES = 300


def _coerce_budget_lines(
    params: dict, aliases: Tuple[str, ...], label_keys: Tuple[str, ...]
) -> List[Tuple[str, float]]:
    raw = None
    for key in aliases:
        if isinstance(params.get(key), list):
            raw = params[key]
            break
    if raw is None:
        return []  # missing side is simply empty (builder validates the total)
    lines: List[Tuple[str, float]] = []
    for entry in raw[:MAX_BUDGET_LINES]:
        if isinstance(entry, dict):
            label = _pick(entry, *label_keys)
            amount = to_number(
                _pick(entry, "amount", "value", "monthly_amount", "planned")
            )
        elif isinstance(entry, list) and len(entry) >= 2:
            label = entry[0]
            amount = to_number(entry[1])
        else:
            continue
        if label is None or amount is None:
            continue
        label = str(label).strip()
        if not label:
            continue
        if amount < 0:
            amount = 0.0
        lines.append((label, amount))
    if not lines:
        raise ValueError("no usable budget lines")
    return lines


def build_budget_spec(params: dict) -> dict:
    """Budget planner: income + expenses tables + live summary."""
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    income = _coerce_budget_lines(
        params,
        ("income", "income_sources", "revenue"),
        ("source", "name", "label", "category"),
    )

    expenses = _coerce_budget_lines(
        params,
        ("expenses", "expense_categories", "costs", "spending"),
        ("category", "name", "label", "type"),
    )

    if not income and not expenses:
        raise ValueError("no income or expense lines")

    period = _pick(params, "period", "frequency", "timeframe")
    period = str(period).strip().capitalize() if period else "Monthly"
    money = _currency_fmt(_pick(params, "currency")) or MONEY_FMT
    notes = _pick(params, "notes", "note")
    sheet_notes = (
        notes.strip()[:1000]
        if isinstance(notes, str) and notes.strip()
        else (
            "Template-built budget — totals and the summary are live formulas. "
            "Edit any amount to plan scenarios."
        )
    )

    blocks: List[dict] = [
        {"cell": "A1", "text": f"{period} Budget", "bold": True, "font_size": 14},
    ]

    # Layout math — tables carry a "title" field, so for a table
    # anchored at row R: title R, header R+1, data R+2.., total row
    # directly after the data. All refs below are computed from THESE
    # numbers (the whole point of templates: refs can never drift
    # from the rendered layout).
    tables: List[dict] = []
    income_total_cell = "0"
    expense_total_cell = "0"
    exp_first_data: Optional[int] = None
    exp_last_data: Optional[int] = None
    row = 3
    if income:
        first = row + 2
        last = first + len(income) - 1
        inc_total = last + 1
        income_total_cell = f"B{inc_total}"
        tables.append(
            {
                "start_cell": f"A{row}",
                "title": "Income",
                "headers": ["Source", "Amount"],
                "rows": [[label, amount] for label, amount in income],
                "number_formats": {"B": money},
                "total_row": ["Total Income", "=SUM(B{first_row}:B{last_row})"],
            }
        )
        row = inc_total + 2
    else:
        blocks.append(
            {"cell": f"A{row}", "text": "No income lines provided", "italic": True}
        )
        row += 2

    if expenses:
        first = row + 2
        last = first + len(expenses) - 1
        exp_total = last + 1
        exp_first_data, exp_last_data = first, last
        expense_total_cell = f"B{exp_total}"
        tables.append(
            {
                "start_cell": f"A{row}",
                "title": "Expenses",
                "headers": ["Category", "Amount"],
                "rows": [[label, amount] for label, amount in expenses],
                "number_formats": {"B": money},
                "total_row": ["Total Expenses", "=SUM(B{first_row}:B{last_row})"],
            }
        )
        row = exp_total + 2
    else:
        blocks.append(
            {"cell": f"A{row}", "text": "No expense lines provided", "italic": True}
        )
        row += 2

    tables.append(
        {
            "start_cell": f"A{row}",
            "title": "Summary",
            "headers": ["Item", "Value"],
            "rows": [
                ["Total Income", f"={income_total_cell}"],
                ["Total Expenses", f"={expense_total_cell}"],
                [
                    "Net (Income - Expenses)",
                    f"={income_total_cell}-{expense_total_cell}",
                ],
                [
                    "Savings Rate",
                    (
                        f"=IF({income_total_cell}>0,"
                        f"TEXT(({income_total_cell}-{expense_total_cell})/"
                        f'{income_total_cell},"0.0%"),"n/a")'
                    ),
                ],
            ],
            "number_formats": {"B": money},
        }
    )

    # Pie chart of the expense mix, floating NEXT TO the tables
    # (column D, clear of the A/B table band). One slice per expense
    # category, sized by its amount — the classic budget overview.
    charts: List[dict] = []
    if exp_first_data is not None:
        charts.append(
            {
                "type": "pie",
                "title": f"{period} Expenses",
                "anchor": "D3",
                "width": 12,
                "height": 9,
                "categories_range": f"Budget!A{exp_first_data}:A{exp_last_data}",
                "series": [
                    {
                        "name": "Expenses",
                        "values_range": f"Budget!B{exp_first_data}:B{exp_last_data}",
                    }
                ],
            }
        )

    sheet_spec: Dict[str, Any] = {
        "name": "Budget",
        "tab_color": "16304F",
        "column_widths": {"A": 30, "B": 16},
        "text_blocks": blocks,
        "tables": tables,
        "notes": sheet_notes,
        # Explicit no-freeze opt-out: the converter's default freezes
        # below the first table's header, but budgets are meant to be
        # scrolled freely — nothing stays pinned.
        "no_freeze": True,
    }
    if charts:
        sheet_spec["charts"] = charts

    return {
        "filename": f"{period.lower()}_budget.xlsx",
        "sheets": [sheet_spec],
    }


# ── Standard pattern entry points (used by the dynamic registry) ──────

build_spec = build_budget_spec
