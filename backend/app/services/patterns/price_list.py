"""
Price list pattern — product pricing catalog with markup & margin.

Deterministic: the classifier extracts the products (sku/name/category/
cost/price); this module lays out the catalog and computes every formula
reference from the actual row numbers of the layout it emits.

Layout (Pricing sheet):
  row 1        {list_name} title (bold, 14)
  row 2        optional VAT assumption: A2 "VAT Rate" + B2 rate (the
               gross-price column reads $B$2, so editing B2 reprices)
  row 4        table headers (start_cell A4, no table title)
  rows 5..N    data: SKU | Product | Category | Cost | Net Price |
               Markup % | Margin % [| Gross Price]
               Markup %  = IF(AND(cost>0, price<>""),
                              (price-cost)/cost, "n/a")
               Margin %  = IF(price>0, (price-cost)/price, "n/a")
               Gross     = IF(price>0, price*(1+$B$2), "n/a")   (VAT only)
               cost/price may be null individually — the guards turn
               those rows into "n/a" instead of #DIV/0!.
  row N+1      total row: SUMs over the exact data rows for the money
               columns, guarded AVERAGEs for the percent columns (the
               label reads "Total / Average" — see the sheet notes).

Categories sheet (steel tab): one row per category with live
COUNTIF/SUMIF/AVERAGEIF formulas over the Pricing table + a cost-vs-
price bar chart; its column A doubles as the dropdown source for the
Pricing category column.

TEMPLATE MODE: a request with no products yet ("create a price list
template") builds the BLANK pricing catalog — 8 empty scaffold rows
with live guarded markup/margin formulas, never invented products
(hard rule), never a refusal for lack of data. The Categories sheet is
emitted as a default-free scaffold (blank category cells + guarded
COUNTIF/SUMIF/AVERAGEIF rows) whose column A feeds the Pricing category
dropdown live — type category names there and both the dropdown and the
subtotals light up. The total row's guarded averages legitimately read
"n/a" over blank rows; charts are skipped until real products exist.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.services.patterns.utils import (
    MONEY_FMT,
    PCT_FMT,
    _currency_fmt,
    _fmt_num,
    _pick,
    col_letter,
    to_number,
    to_rate,
)

# Registry key — must match the pattern stanza in
# prompts/pattern_classifier.md.
PATTERN_NAME = "price_list"

PATTERN_DESCRIPTION = (
    "Product price list / pricing catalog with cost, price, markup and "
    "gross margin per product, optional VAT and category subtotals. A "
    "request with no products yet still gets a blank pricing template "
    "with live guarded formulas and a category scaffold sheet."
)

# Routing keywords/stems — drive the cheap pre-gate and the classifier
# shortlist (see excel_gen._shortlist_patterns).
PATTERN_KEYWORDS = (
    "price list",
    "price",
    "pricing",
    "catalog",
    "catalogue",
    "markup",
    "margin",
    "sku",
    "product",
)

MAX_PRODUCTS = 300
MAX_CATEGORIES = 60
MIN_ROWS = 8  # blank scaffold rows in template mode (no products given)

# Margin traffic lights (design-system status colors).
_MARGIN_LOW = 0.20  # below → amber, negative → red


def _clean_str(value: Any, cap: int) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s[:cap] if s else None


def coerce_price_list_params(params: dict) -> dict:
    """Validate + normalize classifier output. Raises ValueError for
    STRUCTURALLY wrong input only (params not an object, products not
    an array, a product entry without a name).

    Template mode: products missing or an empty array is FINE — the
    builder emits the blank pricing template with scaffold rows (the
    "create a price list" case); products are never invented.

    Accepts alias keys, quoted/currency numbers and percent strings;
    ``cost``/``price`` stay ``None`` individually when only one side is
    known (the workbook then shows "n/a" for that row's margins).
    """
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    raw = None
    for key in ("products", "items", "product_list", "catalog"):
        if key in params:
            if not isinstance(params[key], list):
                raise ValueError("products must be an array")
            raw = params[key]
            break
    # raw None (or []) → template mode — no products to lay out yet.

    products: List[Dict[str, Any]] = []
    for entry in (raw or [])[:MAX_PRODUCTS]:
        if isinstance(entry, str):
            entry = {"name": entry}
        if not isinstance(entry, dict):
            continue
        name = _clean_str(
            _pick(entry, "name", "product", "product_name", "item", "description"),
            80,
        )
        if name is None:
            raise ValueError("product without a name")
        cost = to_number(_pick(entry, "cost", "unit_cost", "cost_price"))
        price = to_number(
            _pick(
                entry,
                "price",
                "sale_price",
                "selling_price",
                "retail_price",
                "unit_price",
            )
        )
        products.append(
            {
                "sku": _clean_str(_pick(entry, "sku", "code", "item_code"), 20),
                "name": name,
                "category": _clean_str(_pick(entry, "category", "cat", "group"), 40),
                "cost": max(cost, 0.0) if cost is not None else None,
                "price": max(price, 0.0) if price is not None else None,
            }
        )
    if raw and not products:
        # entries were given but none were usable — structural garbage,
        # not the blank-template case.
        raise ValueError("no usable products")

    vat_rate = to_rate(_pick(params, "vat_rate", "vat", "tax_rate", "tax"))
    if vat_rate is not None:
        vat_rate = min(max(vat_rate, 0.0), 1.0)

    list_name = (
        _clean_str(_pick(params, "list_name", "name", "title"), 80) or "Price List"
    )

    notes = _pick(params, "notes", "note")
    notes = notes.strip()[:1000] if isinstance(notes, str) and notes.strip() else None

    return {
        "list_name": list_name,
        "currency": _currency_fmt(_pick(params, "currency")),
        "vat_rate": vat_rate,
        "products": products,
        "notes": notes,
    }


def _unique_categories(products: List[dict]) -> List[str]:
    seen: List[str] = []
    for p in products:
        cat = p["category"] or "Uncategorized"
        if cat not in seen:
            seen.append(cat)
    return seen[:MAX_CATEGORIES]


def _filename(list_name: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", list_name)[:40].strip().lower()
    slug = re.sub(r"[\s_-]+", "_", slug).strip("_")
    if not slug or slug == "price_list":
        return "price_list.xlsx"
    if slug.endswith("_price_list") or slug.endswith("_pricelist"):
        return f"{slug}.xlsx"
    return f"{slug}_price_list.xlsx"


def build_price_list_spec(params: dict) -> dict:
    """Product pricing catalog — every formula code-generated.

    No ROUND() in the formulas: display rounding belongs to the number
    formats. Division-by-zero (cost or price missing/zero) is guarded
    with "n/a" so the sheet never shows #DIV/0!.
    """
    p = coerce_price_list_params(params)
    products: List[dict] = p["products"]
    vat_rate: Optional[float] = p["vat_rate"]
    money = p["currency"] or MONEY_FMT

    n = len(products)
    template_mode = n == 0
    n_rows = n if n else MIN_ROWS  # scaffold rows in template mode
    categories = _unique_categories(products)
    m = len(categories)

    # ── Layout math (Pricing sheet) ────────────────────────────────────
    # title row 1, optional VAT assumption row 2, blank 3, headers row 4,
    # data 5..4+n_rows (8 blank scaffold rows in template mode), total
    # row after. Computed here, never hand-typed.
    header_row = 4
    first_data = header_row + 1
    last_data = header_row + n_rows
    total_row = last_data + 1  # noqa

    headers = [
        "SKU",
        "Product",
        "Category",
        "Cost",
        "Net Price",
        "Markup %",
        "Margin %",
    ]
    numfmts: Dict[str, str] = {"D": money, "E": money, "F": PCT_FMT, "G": PCT_FMT}
    if vat_rate is not None:
        headers.append("Gross Price")
        numfmts["H"] = money
    ncols = len(headers)

    rows: List[List[Any]] = []
    for i, prod in enumerate(products):
        r = first_data + i
        row: List[Any] = [
            prod["sku"],
            prod["name"],
            prod["category"],
            prod["cost"],
            prod["price"],
            f'=IF(AND(D{r}>0,E{r}<>""),(E{r}-D{r})/D{r},"n/a")',
            f'=IF(E{r}>0,(E{r}-D{r})/E{r},"n/a")',
        ]
        if vat_rate is not None:
            row.append(f'=IF(E{r}>0,E{r}*(1+$B$2),"n/a")')
        rows.append(row)
    if template_mode:
        # Blank scaffold rows: markup/margin/gross are LIVE guarded
        # formulas that show "n/a" until the user types a cost/price
        # (never invented products — hard rule).
        for i in range(MIN_ROWS):
            r = first_data + i
            row = [
                None,
                None,
                None,
                None,
                None,
                f'=IF(OR($D{r}="",$D{r}=0,$E{r}=""),"n/a",($E{r}-$D{r})/$D{r})',
                f'=IF(OR($E{r}="",$E{r}=0),"n/a",($E{r}-$D{r})/$E{r})',
            ]
            if vat_rate is not None:
                row.append(f'=IF(OR($E{r}="",$E{r}=0),"n/a",$E{r}*(1+$B$2))')
            rows.append(row)

    total_values: List[Any] = [
        "Total / Average",
        None,
        None,
        f"=SUM(D{first_data}:D{last_data})",
        f"=SUM(E{first_data}:E{last_data})",
        f"=IF(COUNT(F{first_data}:F{last_data})>0,"
        f'AVERAGE(F{first_data}:F{last_data}),"n/a")',
        f"=IF(COUNT(G{first_data}:G{last_data})>0,"
        f'AVERAGE(G{first_data}:G{last_data}),"n/a")',
    ]
    if vat_rate is not None:
        total_values.append(
            f"=IF(COUNT(H{first_data}:H{last_data})>0,"
            f'SUM(H{first_data}:H{last_data}),"n/a")'
        )

    blocks: List[dict] = [
        {"cell": "A1", "text": p["list_name"], "bold": True, "font_size": 14},
    ]
    if vat_rate is not None:
        blocks += [
            {"cell": "A2", "text": "VAT Rate"},
            {
                "cell": "B2",
                "text": vat_rate,
                "number_format": PCT_FMT,
                "bold": True,
            },
        ]

    # Margin traffic lights on the margin column (formula rules so the
    # "n/a" text cells never light up).
    margin_cf = {
        "range": f"G{first_data}:G{last_data}",
        "rules": [
            {
                "type": "formula",
                "formula": f"AND(ISNUMBER($G{first_data}),$G{first_data}<0)",
                "fill": "FFC7CE",
                "font_color": "9C0006",
                "stop_if_true": True,
            },
            {
                "type": "formula",
                "formula": (
                    f"AND(ISNUMBER($G{first_data}),"
                    f"$G{first_data}<{_fmt_num(_MARGIN_LOW)})"
                ),
                "fill": "FFF2CC",
                "font_color": "7F6000",
                "stop_if_true": True,
            },
            {
                "type": "formula",
                "formula": (
                    f"AND(ISNUMBER($G{first_data}),"
                    f"$G{first_data}>={_fmt_num(_MARGIN_LOW)})"
                ),
                "fill": "C6EFCE",
                "font_color": "1E4620",
            },
        ],
    }

    sheet_notes = ""
    if p["notes"]:
        sheet_notes += f"{p['notes']} "
    if template_mode:
        sheet_notes += (
            "Blank pricing template — type products into the empty rows "
            "and the guarded formulas compute themselves. "
        )
    sheet_notes += (
        "Markup % = (Net Price - Cost) / Cost and Margin % = (Net Price - "
        "Cost) / Net Price — both guarded, so a missing cost or price shows "
        '"n/a" instead of an error. '
    )
    if vat_rate is not None:
        sheet_notes += (
            "Gross Price = Net Price x (1 + VAT rate); the rate lives in B2 — "
            "edit it and every gross price reprices. "
        )
    sheet_notes += (
        "The total row sums the money columns and averages Markup/Margin "
        f"(margins under {int(_MARGIN_LOW * 100)}% show amber, negative red). "
        "Category cells use a dropdown fed by the Categories sheet, whose "
        "subtotals are live COUNTIF/SUMIF/AVERAGEIF formulas over this table."
    )

    # Dropdown source: derived categories in data mode, the blank
    # scaffold rows in template mode (same sheet, same mechanism).
    cat_n = m if m else MIN_ROWS

    pricing_sheet: Dict[str, Any] = {
        "name": "Pricing",
        "tab_color": "16304F",
        "freeze_panes": f"A{first_data}",
        "column_widths": {
            "A": 12,
            "B": 32,
            "C": 18,
            "D": 13,
            "E": 13,
            "F": 12,
            "G": 12,
        },
        "text_blocks": blocks,
        "tables": [
            {
                "start_cell": f"A{header_row}",
                "headers": headers,
                "rows": rows,
                "number_formats": numfmts,
                "alignments": {"A": "center"},
                "total_row": total_values,
            }
        ],
        "conditional_formats": [margin_cf],
        "data_validation": [
            {
                "range": f"C{first_data}:C{last_data}",
                "source_range": f"Categories!$A$4:$A${3 + cat_n}",
                "error_style": "warning",
            }
        ],
        "notes": sheet_notes,
    }
    if not template_mode:
        # No chart over 8 blank product names — it appears as soon as
        # real products exist.
        pricing_sheet["charts"] = [
            {
                "type": "bar",
                "title": "Gross Margin by Product",
                "anchor": f"{col_letter(ncols + 2)}{header_row}",
                "width": 16,
                "height": 10,
                "categories_range": f"Pricing!B{first_data}:B{last_data}",
                "series": [
                    {
                        "name": "Margin %",
                        "values_range": f"Pricing!G{first_data}:G{last_data}",
                    }
                ],
                "value_numfmt": "0.0%",
            }
        ]
    if vat_rate is not None:
        pricing_sheet["column_widths"]["H"] = 14

    # ── Categories sheet ───────────────────────────────────────────────
    # title row 1, blank 2, headers row 3, data 4..3+cat_n, total row
    # after (blank scaffold rows in template mode).
    cat_header_row = 3
    cat_first = cat_header_row + 1
    cat_last = cat_header_row + cat_n

    pricing_cat = f"Pricing!$C${first_data}:$C${last_data}"
    cat_rows: List[List[Any]] = []
    if template_mode:
        # Default-free scaffold: blank category cells + guarded live
        # subtotals — type a category name in column A and the row (and
        # the Pricing dropdown) lights up. No category is invented.
        for i in range(MIN_ROWS):
            r = cat_first + i
            cat_rows.append(
                [
                    None,
                    f'=IF($A{r}="","",COUNTIF({pricing_cat},$A{r}))',
                    f'=IF($A{r}="","",'
                    f"SUMIF({pricing_cat},$A{r},"
                    f"Pricing!$D${first_data}:$D${last_data}))",
                    f'=IF($A{r}="","",'
                    f"SUMIF({pricing_cat},$A{r},"
                    f"Pricing!$E${first_data}:$E${last_data}))",
                    f'=IF($A{r}="","",'
                    f"IFERROR(AVERAGEIF({pricing_cat},$A{r},"
                    f'Pricing!$G${first_data}:$G${last_data}),"n/a"))',
                ]
            )
    else:
        for j, cat in enumerate(categories):
            r = cat_first + j
            cat_rows.append(
                [
                    cat,
                    f"=COUNTIF({pricing_cat},$A{r})",
                    f"=SUMIF({pricing_cat},$A{r},Pricing!$D${first_data}:$D${last_data})",
                    f"=SUMIF({pricing_cat},$A{r},Pricing!$E${first_data}:$E${last_data})",
                    f"=IFERROR(AVERAGEIF({pricing_cat},$A{r},"
                    f'Pricing!$G${first_data}:$G${last_data}),"n/a")',
                ]
            )

    categories_sheet: Dict[str, Any] = {
        "name": "Categories",
        "tab_color": "1B3A5C",
        "column_widths": {"A": 24, "B": 12, "C": 15, "D": 15, "E": 14},
        "text_blocks": [
            {
                "cell": "A1",
                "text": "Category Summary",
                "bold": True,
                "font_size": 12,
                "font_color": "16304F",
            }
        ],
        "tables": [
            {
                "start_cell": f"A{cat_header_row}",
                "headers": [
                    "Category",
                    "Products",
                    "Total Cost",
                    "Total Price",
                    "Avg Margin %",
                ],
                "rows": cat_rows,
                "number_formats": {
                    "B": "0",
                    "C": money,
                    "D": money,
                    "E": PCT_FMT,
                },
                "total_row": [
                    "Total",
                    f"=SUM(B{cat_first}:B{cat_last})",
                    f"=SUM(C{cat_first}:C{cat_last})",
                    f"=SUM(D{cat_first}:D{cat_last})",
                    f"=IF(COUNT(E{cat_first}:E{cat_last})>0,"
                    f'AVERAGE(E{cat_first}:E{cat_last}),"n/a")',
                ],
            }
        ],
        "notes": (
            "Every value is a live COUNTIF / SUMIF / AVERAGEIF formula over "
            "the Pricing sheet — add products or change categories there and "
            "this summary updates. Column A is the dropdown source for the "
            + (
                "Pricing category column: type your category names into the "
                "blank column-A rows and both the dropdown and the subtotals "
                "light up (nothing is pre-filled — categories are yours to "
                "define)."
                if template_mode
                else "Pricing category column."
            )
        ),
    }
    if m >= 2:
        categories_sheet["charts"] = [
            {
                "type": "bar",
                "title": "Cost vs Price by Category",
                "anchor": "G3",
                "width": 15,
                "height": 9,
                "categories_range": f"Categories!A{cat_first}:A{cat_last}",
                "series": [
                    {
                        "name": "Total Cost",
                        "values_range": f"Categories!C{cat_first}:C{cat_last}",
                    },
                    {
                        "name": "Total Price",
                        "values_range": f"Categories!D{cat_first}:D{cat_last}",
                    },
                ],
                "value_numfmt": money,
            }
        ]

    return {
        "filename": _filename(p["list_name"]),
        "sheets": [pricing_sheet, categories_sheet],
    }


# ── Standard pattern entry points (used by the dynamic registry) ──────

coerce_params = coerce_price_list_params
build_spec = build_price_list_spec
