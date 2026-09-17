"""
Net worth pattern — assets vs liabilities statement.

Deterministic: the classifier extracts the line items (the thing small
models are good at); this module builds the workbook spec in code with
every formula reference computed from the actual layout rows it emits.

Layout (NetWorth sheet):
  row 1      title
  row 2      as-of date (given date, or a live =TODAY() formula)
  Assets table: Category | Item | Value + SUM total row
  Liabilities table: same shape
  Net Worth Summary table: totals, net worth, debt-to-asset ratio
  bar chart of the three headline numbers

Breakdown sheet (secondary, steel tab):
  Assets by Category / Liabilities by Category rollups — SUMIF over
  the main sheet's category column, live % shares, pie / bar charts.
  The main sheet's Category columns carry dropdowns sourced from these
  lists, so re-categorizing an item updates the rollups instantly.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.services.patterns.utils import (
    MONEY_FMT,
    PCT_FMT,
    _currency_fmt,
    _pick,
    to_iso_date,
    to_number,
)

# Registry key — must match the pattern stanza in
# prompts/pattern_classifier.md.
PATTERN_NAME = "net_worth"

PATTERN_DESCRIPTION = (
    "Personal / family / company net worth statement: assets vs "
    "liabilities with live totals, net worth, debt ratio and category "
    "breakdowns."
)

# Routing keywords/stems — drive the cheap pre-gate and the classifier
# shortlist (see excel_gen._shortlist_patterns).
PATTERN_KEYWORDS = (
    "net worth",
    "networth",
    "balance sheet",
    "asset",
    "liabilit",
    "what i own",
    "what i owe",
    "wealth statement",
    "financial position",
)

MAX_NET_WORTH_LINES = 200  # per side

# (category, item, value) tuples after coercion
_Entry = Tuple[str, str, float]


def _coerce_entry_lines(params: dict, list_keys: Tuple[str, ...]) -> List[_Entry]:
    """Classifier line items → [(category, item, value), ...].

    Accepts dicts (category/item/value with the usual alias keys) and
    bare [item, value] / [category, item, value] lists. Entries without
    an item name are skipped — never invent items. A missing value
    counts as 0; a missing category falls back to "Other".
    """
    raw = None
    for key in list_keys:
        if isinstance(params.get(key), list):
            raw = params[key]
            break
    if raw is None:
        return []

    lines: List[_Entry] = []
    for entry in raw[:MAX_NET_WORTH_LINES]:
        category: Optional[str] = None
        item: Optional[str] = None
        value: Optional[float] = None
        if isinstance(entry, dict):
            category = _pick(entry, "category", "type", "group")
            item = _pick(entry, "item", "name", "description", "label")
            value = to_number(_pick(entry, "value", "amount"))
        elif isinstance(entry, list):
            if len(entry) >= 3:
                category, item = entry[0], entry[1]
                value = to_number(entry[2])
            elif len(entry) == 2:
                item = entry[0]
                value = to_number(entry[1])
        else:
            continue
        if item is None or not str(item).strip():
            continue
        item = str(item).strip()[:120]
        category = str(category).strip()[:60] if category else ""
        category = category.strip() or "Other"
        if value is None:
            value = 0.0
        value = min(max(float(value), 0.0), 1e12)
        lines.append((category, item, value))
    return lines


def _distinct_categories(lines: List[_Entry]) -> List[str]:
    """First-appearance category order (stable rollup rows)."""
    seen: List[str] = []
    for category, _item, _value in lines:
        if category not in seen:
            seen.append(category)
    return seen


def coerce_net_worth_params(params: dict) -> dict:
    """Validate + normalize classifier params; raises ValueError.

    Accepts alias keys ("debts" for liabilities), quoted/currency
    values and string categories. Both sides may be empty individually
    — but at least one line must exist to build a statement.
    """
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    assets = _coerce_entry_lines(params, ("assets",))
    liabilities = _coerce_entry_lines(params, ("liabilities", "debts"))

    if not assets and not liabilities:
        raise ValueError("no asset or liability lines")

    as_of = to_iso_date(_pick(params, "as_of_date", "date"))

    notes = _pick(params, "notes", "note")
    notes = notes.strip()[:1000] if isinstance(notes, str) and notes.strip() else None

    return {
        "as_of_date": as_of,
        "assets": assets,
        "liabilities": liabilities,
        "currency": _currency_fmt(_pick(params, "currency")),
        "notes": notes,
    }


def build_net_worth_spec(params: dict) -> dict:
    """Net worth statement — every formula code-generated."""
    p = coerce_net_worth_params(params)
    assets: List[_Entry] = p["assets"]
    liabilities: List[_Entry] = p["liabilities"]
    as_of = p["as_of_date"]
    money = p["currency"] or MONEY_FMT

    asset_cats = _distinct_categories(assets)
    liab_cats = _distinct_categories(liabilities)

    # ── NetWorth sheet layout math ───────────────────────────────────
    blocks: List[dict] = [
        {
            "cell": "A1",
            "text": "Net Worth Statement",
            "bold": True,
            "font_size": 14,
        },
        {"cell": "A2", "text": "As of"},
        {
            "cell": "B2",
            # no date given → a LIVE today marker, never a fabricated date
            "text": as_of or "=TODAY()",
            "number_format": "yyyy-mm-dd",
        },
    ]

    tables: List[dict] = []
    data_validations: List[dict] = []

    row = 4
    assets_first = assets_last = assets_total = None
    if assets:
        first = row + 2
        last = first + len(assets) - 1
        total = last + 1
        assets_first, assets_last, assets_total = first, last, total
        tables.append(
            {
                "start_cell": f"A{row}",
                "title": "Assets",
                "headers": ["Category", "Item", "Value"],
                "rows": [[c, i, v] for c, i, v in assets],
                "number_formats": {"C": money},
                "total_row": [
                    "Total Assets",
                    "",
                    "=SUM(C{first_row}:C{last_row})",
                ],
            }
        )
        row = total + 2
    else:
        blocks.append({"cell": f"A{row}", "text": "No assets provided", "italic": True})
        row += 2

    liab_first = liab_last = liab_total = None
    if liabilities:
        first = row + 2
        last = first + len(liabilities) - 1
        total = last + 1
        liab_first, liab_last, liab_total = first, last, total
        tables.append(
            {
                "start_cell": f"A{row}",
                "title": "Liabilities",
                "headers": ["Category", "Item", "Value"],
                "rows": [[c, i, v] for c, i, v in liabilities],
                "number_formats": {"C": money},
                "total_row": [
                    "Total Liabilities",
                    "",
                    "=SUM(C{first_row}:C{last_row})",
                ],
            }
        )
        row = total + 2
    else:
        blocks.append(
            {"cell": f"A{row}", "text": "No liabilities provided", "italic": True}
        )
        row += 2

    assets_cell = f"C{assets_total}" if assets_total else "0"
    liab_cell = f"C{liab_total}" if liab_total else "0"

    summary_start = row
    summary_first = summary_start + 2
    net_worth_cell = f"B{summary_first + 2}"
    tables.append(
        {
            "start_cell": f"A{summary_start}",
            "title": "Net Worth Summary",
            "headers": ["Item", "Value"],
            "rows": [
                ["Total Assets", f"={assets_cell}"],
                ["Total Liabilities", f"={liab_cell}"],
                ["Net Worth", f"={assets_cell}-{liab_cell}"],
                [
                    "Debt-to-Asset Ratio",
                    (
                        f"=IF({assets_cell}>0,"
                        f'TEXT({liab_cell}/{assets_cell},"0.0%"),"n/a")'
                    ),
                ],
            ],
            "number_formats": {"B": money},
        }
    )

    # Headline bar chart: the three numbers that define the statement.
    charts: List[dict] = [
        {
            "type": "bar",
            "title": "Assets vs Liabilities",
            "anchor": "E4",
            "width": 13,
            "height": 9,
            "categories_range": (f"NetWorth!A{summary_first}:A{summary_first + 2}"),
            "series": [
                {
                    "name": "Amount",
                    "values_range": (f"NetWorth!B{summary_first}:B{summary_first + 2}"),
                }
            ],
            "value_numfmt": money,
        }
    ]

    # Status semantics: positive net worth = green, negative = red.
    conditional_formats: List[dict] = [
        {
            "range": net_worth_cell,
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
        "Edit any value or category and the totals, net worth and the "
        "Breakdown rollups recalculate. Net Worth = Total Assets - Total "
        "Liabilities; the debt-to-asset ratio is liabilities as a share "
        "of assets. Category dropdowns read the Breakdown sheet's lists."
    )

    networth_sheet: Dict[str, Any] = {
        "name": "NetWorth",
        "tab_color": "16304F",
        "column_widths": {"A": 26, "B": 34, "C": 16},
        "no_freeze": True,
        "text_blocks": blocks,
        "tables": tables,
        "charts": charts,
        "conditional_formats": conditional_formats,
        "notes": sheet_notes,
    }

    # ── Breakdown sheet layout math ──────────────────────────────────
    bd_blocks: List[dict] = [
        {
            "cell": "A1",
            "text": "Category Breakdown",
            "bold": True,
            "font_size": 14,
        },
    ]
    bd_tables: List[dict] = []
    bd_charts: List[dict] = []

    b_row = 3
    asset_cat_first = asset_cat_last = asset_cat_total = None
    if asset_cats:
        first = b_row + 2
        last = first + len(asset_cats) - 1
        total = last + 1
        asset_cat_first, asset_cat_last, asset_cat_total = first, last, total  # noqa
        bd_tables.append(
            {
                "start_cell": f"A{b_row}",
                "title": "Assets by Category",
                "headers": ["Category", "Total", "% of Assets"],
                "rows": [
                    [
                        cat,
                        (
                            f"=SUMIF(NetWorth!$A${assets_first}:$A${assets_last},"
                            f"$A{r},NetWorth!$C${assets_first}:$C${assets_last})"
                        ),
                        f'=IF($B${total}>0,B{r}/$B${total},"n/a")',
                    ]
                    for r, cat in zip(range(first, last + 1), asset_cats)
                ],
                "number_formats": {"B": money, "C": PCT_FMT},
                "total_row": [
                    "Total Assets",
                    "=SUM(B{first_row}:B{last_row})",
                    "",
                ],
            }
        )
        # category dropdown sourced from this list (grows with edits)
        data_validations.append(
            {
                "range": f"A{assets_first}:A{assets_last}",
                "source_range": (f"Breakdown!$A${asset_cat_first}:$A${asset_cat_last}"),
                "error_style": "warning",
            }
        )
        if len(asset_cats) >= 2:  # one slice is not a mix
            bd_charts.append(
                {
                    "type": "pie",
                    "title": "Asset Mix",
                    "anchor": "E3",
                    "width": 12,
                    "height": 9,
                    "categories_range": (
                        f"Breakdown!A{asset_cat_first}:A{asset_cat_last}"
                    ),
                    "series": [
                        {
                            "name": "Assets",
                            "values_range": (
                                f"Breakdown!B{asset_cat_first}:" f"B{asset_cat_last}"
                            ),
                        }
                    ],
                }
            )
        b_row = total + 2

    liab_cat_first = liab_cat_last = liab_cat_total = None
    if liab_cats:
        first = b_row + 2
        last = first + len(liab_cats) - 1
        total = last + 1
        liab_cat_first, liab_cat_last, liab_cat_total = first, last, total  # noqa
        bd_tables.append(
            {
                "start_cell": f"A{b_row}",
                "title": "Liabilities by Category",
                "headers": ["Category", "Total", "% of Liabilities"],
                "rows": [
                    [
                        cat,
                        (
                            f"=SUMIF(NetWorth!$A${liab_first}:$A${liab_last},"
                            f"$A{r},NetWorth!$C${liab_first}:$C${liab_last})"
                        ),
                        f'=IF($B${total}>0,B{r}/$B${total},"n/a")',
                    ]
                    for r, cat in zip(range(first, last + 1), liab_cats)
                ],
                "number_formats": {"B": money, "C": PCT_FMT},
                "total_row": [
                    "Total Liabilities",
                    "=SUM(B{first_row}:B{last_row})",
                    "",
                ],
            }
        )
        data_validations.append(
            {
                "range": f"A{liab_first}:A{liab_last}",
                "source_range": (f"Breakdown!$A${liab_cat_first}:$A${liab_cat_last}"),
                "error_style": "warning",
            }
        )
        if len(liab_cats) >= 2:
            bd_charts.append(
                {
                    "type": "bar_h",
                    "title": "Liabilities by Category",
                    "anchor": "E22",
                    "width": 12,
                    "height": 9,
                    "categories_range": (
                        f"Breakdown!A{liab_cat_first}:A{liab_cat_last}"
                    ),
                    "series": [
                        {
                            "name": "Liabilities",
                            "values_range": (
                                f"Breakdown!B{liab_cat_first}:" f"B{liab_cat_last}"
                            ),
                        }
                    ],
                }
            )

    if data_validations:
        networth_sheet["data_validation"] = data_validations

    breakdown_sheet: Dict[str, Any] = {
        "name": "Breakdown",
        "tab_color": "1B3A5C",
        "column_widths": {"A": 26, "B": 16, "C": 18},
        "no_freeze": True,
        "text_blocks": bd_blocks,
        "tables": bd_tables,
        "notes": (
            "Live rollups: each Total is a SUMIF over the NetWorth sheet's "
            "category column, so re-categorizing an item there updates "
            "these tables and the charts. The % column guards a zero total."
        ),
    }
    if bd_charts:
        breakdown_sheet["charts"] = bd_charts

    return {
        "filename": "net_worth.xlsx",
        "sheets": [networth_sheet, breakdown_sheet],
    }


# ── Standard pattern entry points (used by the dynamic registry) ──────

coerce_params = coerce_net_worth_params
build_spec = build_net_worth_spec
