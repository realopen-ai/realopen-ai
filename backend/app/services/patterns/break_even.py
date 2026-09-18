"""
Break-even pattern — ONE product/service: fixed costs, price, variable
cost per unit → break-even units/revenue.

Deterministic: the classifier extracts the three core scalars (never
invented — the pattern refuses to build without them); this module
emits the whole workbook with every formula reference computed from
the layout rows it renders.

Layout (BreakEven sheet):
  row 1      title
  rows 2-4/5 inputs: B2 fixed costs, B3 price, B4 variable cost,
             B5 target profit (only when the user stated one)
  section    "Break-Even Results" header + label/value text blocks
             (contribution margin, ratio, break-even units/revenue,
             target-profit units/revenue) — mixed value types, so each
             block carries its own number format
  table      Sensitivity Analysis — units stepped around the break-even
             point with live cost/revenue/profit formulas and red/green
             profit status
  chart      line chart of revenue, total costs and profit vs units —
             the crossing IS the break-even point

All division formulas carry IF guards: a user editing the inputs to
price <= variable cost sees "n/a" instead of #DIV/0!.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List

from app.services.patterns.utils import (
    MONEY_FMT,
    PCT_FMT,
    QTY_FMT,
    _currency_fmt,
    _pick,
    to_number,
)

# Registry key — must match the pattern stanza in
# prompts/pattern_classifier.md.
PATTERN_NAME = "break_even"

PATTERN_DESCRIPTION = (
    "Break-even analysis for one product: fixed costs, price and "
    "variable cost per unit → break-even units/revenue, target-profit "
    "units, sensitivity table and cost/revenue chart."
)

# Routing keywords/stems — drive the cheap pre-gate and the classifier
# shortlist (see excel_gen._shortlist_patterns).
PATTERN_KEYWORDS = (
    "break even",
    "break-even",
    "breakeven",
    "break-even point",
    "break even point",
    "fixed cost",
    "fixed costs",
    "variable cost",
    "variable costs",
    "contribution margin",
    "margin of safety",
    "units to break even",
)

MAX_SENSITIVITY_ROWS = 20  # units stepped around the break-even point


def _sensitivity_units(break_even_units: float) -> List[int]:
    """Integer unit volumes bracketing the break-even point.

    From ~50% to ~150% of break-even, capped at MAX_SENSITIVITY_ROWS
    rows via a coarser step when the volumes get large. Layout input
    only — the cells themselves are live formulas.
    """
    lo = max(0, int(break_even_units * 0.5))
    hi = int(break_even_units * 1.5) + 1
    if hi - lo < 10:
        hi = lo + 10
    span = hi - lo + 1
    step = math.ceil(span / MAX_SENSITIVITY_ROWS)
    units = list(range(lo, hi + 1, step))
    return units


def coerce_break_even_params(params: dict) -> dict:
    """Validate + normalize classifier params; raises ValueError.

    The three core numbers are REQUIRED (the stanza: if any is missing
    the whole params are insufficient). Also refuses nonsensical data —
    price must exceed variable cost, or no break-even exists to chart.
    Accepts alias keys and quoted/currency numbers.
    """
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    fixed = to_number(_pick(params, "fixed_costs", "fixed_cost", "fixed", "overhead"))
    if fixed is None or fixed < 0:
        raise ValueError("fixed_costs missing or negative")
    fixed = min(float(fixed), 1e12)

    price = to_number(
        _pick(
            params,
            "price_per_unit",
            "price",
            "unit_price",
            "selling_price",
        )
    )
    if price is None or price <= 0:
        raise ValueError("price_per_unit missing or not positive")
    price = min(float(price), 1e12)

    variable = to_number(
        _pick(
            params,
            "variable_cost_per_unit",
            "variable_cost",
            "unit_variable_cost",
            "unit_cost",
            "cost_per_unit",
        )
    )
    if variable is None or variable < 0:
        raise ValueError("variable_cost_per_unit missing or negative")
    variable = min(float(variable), 1e12)

    if price <= variable:
        raise ValueError("price_per_unit must exceed variable_cost_per_unit")

    target_profit = to_number(
        _pick(params, "target_profit", "profit_target", "desired_profit")
    )
    if target_profit is not None and target_profit <= 0:
        target_profit = None  # only meaningful when positive
    if target_profit is not None:
        target_profit = min(float(target_profit), 1e12)

    product_name = _pick(params, "product_name", "product", "name")
    product_name = (
        str(product_name).strip()[:100]
        if product_name is not None and str(product_name).strip()
        else "Product"
    )

    notes = _pick(params, "notes", "note")
    notes = notes.strip()[:1000] if isinstance(notes, str) and notes.strip() else None

    return {
        "product_name": product_name,
        "fixed_costs": fixed,
        "price_per_unit": price,
        "variable_cost_per_unit": variable,
        "target_profit": target_profit,
        "currency": _currency_fmt(_pick(params, "currency")),
        "notes": notes,
    }


def build_break_even_spec(params: dict) -> dict:
    """Break-even analysis workbook — every formula code-generated."""
    p = coerce_break_even_params(params)
    product_name = p["product_name"]
    fixed, price, variable = (
        p["fixed_costs"],
        p["price_per_unit"],
        p["variable_cost_per_unit"],
    )
    target_profit = p["target_profit"]
    money = p["currency"] or MONEY_FMT

    break_even_units = fixed / (price - variable)
    units = _sensitivity_units(break_even_units)

    # ── layout math ──────────────────────────────────────────────────
    fixed_row, price_row, var_row = 2, 3, 4
    tp_row = 5 if target_profit is not None else None

    blocks: List[dict] = [
        {
            "cell": "A1",
            "text": f"Break-Even Analysis — {product_name}",
            "bold": True,
            "font_size": 14,
        },
        {"cell": f"A{fixed_row}", "text": "Fixed Costs"},
        {"cell": f"B{fixed_row}", "text": fixed, "number_format": money},
        {"cell": f"A{price_row}", "text": "Price per Unit"},
        {"cell": f"B{price_row}", "text": price, "number_format": money},
        {"cell": f"A{var_row}", "text": "Variable Cost per Unit"},
        {"cell": f"B{var_row}", "text": variable, "number_format": money},
    ]
    if tp_row is not None:
        blocks += [
            {"cell": f"A{tp_row}", "text": "Target Profit"},
            {
                "cell": f"B{tp_row}",
                "text": target_profit,
                "number_format": money,
            },
        ]

    input_end = tp_row or var_row
    results_header = input_end + 2

    margin = f"B{price_row}-B{var_row}"
    cm_row = results_header + 1
    ratio_row = cm_row + 1
    beu_row = ratio_row + 1
    ber_row = beu_row + 1

    blocks.append(
        {
            "cell": f"A{results_header}",
            "text": "Break-Even Results",
            "bold": True,
            "font_size": 12,
            "font_color": "C9A227",
        }
    )
    blocks += [
        {"cell": f"A{cm_row}", "text": "Contribution Margin per Unit"},
        {
            "cell": f"B{cm_row}",
            "text": f"={margin}",
            "number_format": money,
        },
        {"cell": f"A{ratio_row}", "text": "Contribution Margin Ratio"},
        {
            "cell": f"B{ratio_row}",
            "text": (f'=IF(B{price_row}>0,({margin})/B{price_row},"n/a")'),
            "number_format": PCT_FMT,
        },
        {"cell": f"A{beu_row}", "text": "Break-Even Units"},
        {
            "cell": f"B{beu_row}",
            "text": (f'=IF(({margin})>0,B{fixed_row}/({margin}),"n/a")'),
            "bold": True,
            "number_format": QTY_FMT,
        },
        {"cell": f"A{ber_row}", "text": "Break-Even Revenue"},
        {
            "cell": f"B{ber_row}",
            "text": (f'=IF(ISNUMBER(B{beu_row}),B{beu_row}*B{price_row},"n/a")'),
            "number_format": money,
        },
    ]
    results_end = ber_row

    tpu_row = tpr_row = None
    if tp_row is not None:
        tpu_row = ber_row + 1
        tpr_row = tpu_row + 1
        blocks += [
            {"cell": f"A{tpu_row}", "text": "Units for Target Profit"},
            {
                "cell": f"B{tpu_row}",
                "text": (
                    f"=IF(({margin})>0," f'(B{fixed_row}+B{tp_row})/({margin}),"n/a")'
                ),
                "number_format": QTY_FMT,
            },
            {"cell": f"A{tpr_row}", "text": "Revenue for Target Profit"},
            {
                "cell": f"B{tpr_row}",
                "text": (
                    f"=IF(ISNUMBER(B{tpu_row})," f'B{tpu_row}*B{price_row},"n/a")'
                ),
                "number_format": money,
            },
        ]
        results_end = tpr_row

    sens_start = results_end + 2
    sens_header = sens_start + 1
    sens_first = sens_header + 1
    sens_last = sens_first + len(units) - 1

    sens_rows: List[List[Any]] = []
    for i, u in enumerate(units):
        r = sens_first + i
        sens_rows.append(
            [
                u,
                f"=$B${fixed_row}",
                f"=A{r}*$B${var_row}",
                f"=B{r}+C{r}",
                f"=A{r}*$B${price_row}",
                f"=E{r}-D{r}",
            ]
        )

    tables: List[dict] = [
        {
            "start_cell": f"A{sens_start}",
            "title": "Sensitivity Analysis",
            "headers": [
                "Units Sold",
                "Fixed Costs",
                "Variable Costs",
                "Total Costs",
                "Revenue",
                "Profit / (Loss)",
            ],
            "rows": sens_rows,
            "number_formats": {
                "A": "#,##0",
                "B": money,
                "C": money,
                "D": money,
                "E": money,
                "F": money,
            },
            "alignments": {"A": "center"},
        }
    ]

    # The classic break-even picture: revenue and total-cost lines
    # cross at the break-even units; profit crosses zero there.
    charts: List[dict] = [
        {
            "type": "line",
            "title": "Cost, Revenue & Profit vs Units",
            "anchor": "H2",
            "width": 16,
            "height": 10,
            "categories_range": f"BreakEven!A{sens_first}:A{sens_last}",
            "series": [
                {
                    "name": "Revenue",
                    "values_range": f"BreakEven!E{sens_first}:E{sens_last}",
                },
                {
                    "name": "Total Costs",
                    "values_range": f"BreakEven!D{sens_first}:D{sens_last}",
                },
                {
                    "name": "Profit / (Loss)",
                    "values_range": f"BreakEven!F{sens_first}:F{sens_last}",
                },
            ],
            "value_numfmt": money,
        }
    ]

    # Status semantics: below break-even = red loss, above = green
    # profit (rules in priority order on the profit column).
    conditional_formats: List[dict] = [
        {
            "range": f"F{sens_first}:F{sens_last}",
            "rules": [
                {
                    "type": "cell_is",
                    "operator": "less_than",
                    "value": 0,
                    "fill": "FFC7CE",
                    "font_color": "9C0006",
                },
                {
                    "type": "cell_is",
                    "operator": "greater_than_or_equal",
                    "value": 0,
                    "fill": "C6EFCE",
                    "font_color": "1E4620",
                },
            ],
        }
    ]

    sheet_notes = p["notes"] or (
        "Edit B2 (fixed costs), B3 (price), B4 (variable cost) or B5 "
        "(target profit) and every result recalculates. Break-even units "
        "= fixed costs / (price - variable cost) — round up to the next "
        "whole unit in practice. The sensitivity table steps unit volumes "
        "around the break-even point; profit turns green once revenue "
        "covers total costs. Guards show n/a if price falls to or below "
        "the variable cost."
    )

    sheet: Dict[str, Any] = {
        "name": "BreakEven",
        "tab_color": "16304F",
        "column_widths": {
            "A": 12,
            "B": 14,
            "C": 15,
            "D": 14,
            "E": 14,
            "F": 16,
        },
        "no_freeze": True,
        "text_blocks": blocks,
        "tables": tables,
        "charts": charts,
        "conditional_formats": conditional_formats,
        "notes": sheet_notes,
    }

    return {
        "filename": "break_even.xlsx",
        "sheets": [sheet],
    }


# ── Standard pattern entry points (used by the dynamic registry) ──────

coerce_params = coerce_break_even_params
build_spec = build_break_even_spec
