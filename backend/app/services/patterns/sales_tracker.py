"""
Sales tracker pattern — a sales LOG with live analysis (deterministic).

Creates a sales tracker workbook for users who list individual sales
they already made and want them analyzed:

  Sales         Date | Product | Channel | Customer | Quantity |
                Unit Price | Unit Cost | Revenue | Profit | Margin %
                — Revenue = Quantity x Unit Price and Profit =
                Revenue - Quantity x Unit Cost are LIVE formulas
                (Profit/Margin stay blank until a Unit Cost is typed,
                so half-known sales never show fake profit); negative
                profit rows turn red; header frozen + auto-filter.
  Analysis      topline stats (sales logged, units sold, total revenue,
                total profit, average order value) + three SUMIF
                breakdowns — revenue by product, revenue by channel
                (only when channels were given) and revenue by month
                (SUMIF over date ranges, only when dates were given) —
                plus a revenue-by-product bar chart and a
                revenue-by-month bar chart.

Every formula reference is computed from the actual layout rows this
module emits, so off-by-N row math is impossible by construction.
No ROUND() anywhere — display rounding is the number format's job.

TEMPLATE MODE: a request with no sales yet ("create a sales log")
builds the BLANK log — 8 empty scaffold rows with live guarded
Revenue / Profit / Margin formulas, never invented sales (hard rule),
never a refusal for lack of data. The Analysis sheet carries the
topline stats (legitimately 0 over blank rows) and a Revenue-by-Product
scaffold table: type product names into its blank label rows and the
SUMIFs aggregate the logged sales live. Charts and the channel/month
breakdowns appear only when real sales exist.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from app.services.patterns.utils import (
    MONEY_FMT,
    PCT_FMT,
    QTY_FMT,
    _currency_fmt,
    _pick,
    next_month,
    to_iso_date,
    to_number,
)

# Registry key — must match the pattern stanza in
# prompts/pattern_classifier.md.
PATTERN_NAME = "sales_tracker"

PATTERN_DESCRIPTION = (
    "Creates a sales log spreadsheet with one row per sale (date, "
    "product, channel, customer, quantity, unit price, optional unit "
    "cost) and computed revenue, profit and margin, plus an analysis "
    "sheet with revenue by product, by channel and by month. Use when "
    "the user lists actual sales made and wants them tracked or "
    "analyzed. A request with no sales yet still gets a blank sales-log "
    "template with live analysis formulas. Do NOT use it for CRM deal "
    "pipelines that haven't closed or KPI target-vs-actual reports."
)

# Routing keywords/stems — drive the cheap pre-gate and the classifier
# shortlist (see excel_gen._shortlist_patterns).
PATTERN_KEYWORDS = (
    "sale",
    "sold",
    "sell",
    "revenue",
    "units sold",
    "sales log",
    "sales report",
)

MAX_SALES = 500  # explicit log rows (matches MAX_ROWS_PER_TABLE)
MAX_MONTHS = 36  # month buckets in the by-month breakdown
MIN_ROWS = 8  # blank scaffold log rows in template mode
MIN_BREAKDOWN_ROWS = 6  # blank scaffold rows in the product breakdown

_DATE_FMT = "yyyy-mm-dd"
_INT_FMT = "#,##0"


# ── Param coercion ────────────────────────────────────────────────────


def _clean_text(value: Any, limit: int = 200) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s[:limit] if s else None


def _normalize_sale(entry: Any) -> Optional[dict]:
    """One sales entry → {date, product, channel, customer, quantity,
    unit_price, unit_cost}, or None when unusable.

    product and unit_price are required (the stanza's contract);
    quantity defaults to 1 when not stated; unit_cost is kept only
    when given; channel/customer/date only when stated.
    """
    if not isinstance(entry, dict):
        return None

    product = _clean_text(
        _pick(entry, "product", "product_name", "item", "description")
    )
    if product is None:
        return None  # never invent a sale

    unit_price = to_number(
        _pick(entry, "unit_price", "price", "each", "rate", "amount", "sale_price")
    )
    if unit_price is None or unit_price < 0:
        return None  # a sale without a price cannot be analyzed

    quantity = to_number(
        _pick(entry, "quantity", "qty", "units", "count", "amount_sold")
    )
    if quantity is None or quantity <= 0:
        quantity = 1.0  # stanza: quantity 1 when not stated

    unit_cost = to_number(_pick(entry, "unit_cost", "cost", "cogs", "unit_costs"))
    if unit_cost is not None and unit_cost < 0:
        unit_cost = None  # a negative cost is garbage input

    return {
        "date": to_iso_date(
            _pick(entry, "date", "sale_date", "when", "sold_on", "transaction_date")
        ),
        "product": product,
        "channel": _clean_text(
            _pick(entry, "channel", "source", "medium", "sales_channel")
        ),
        "customer": _clean_text(_pick(entry, "customer", "client", "buyer", "account")),
        "quantity": quantity,
        "unit_price": unit_price,
        "unit_cost": unit_cost,
    }


def coerce_sales_tracker_params(params: dict) -> dict:
    """Validate + normalize classifier params; raises ValueError for
    STRUCTURALLY wrong input only (params not an object, sales not an
    array, a non-empty sales array with no usable entries).

    Template mode: sales missing or an empty array is FINE — the
    builder emits the blank sales log with scaffold rows (the "create
    a sales log" case); sales are never invented.
    """
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    raw = None
    for key in (
        "sales",
        "sale_log",
        "sales_log",
        "entries",
        "records",
        "transactions",
        "orders",
        "items",
    ):
        if key in params:
            if not isinstance(params[key], list):
                raise ValueError("sales must be an array")
            raw = params[key]
            break
    # raw None (or []) → template mode — no sales to lay out yet.

    sales: List[dict] = []
    if raw is not None:
        for entry in raw[:MAX_SALES]:
            normalized = _normalize_sale(entry)
            if normalized is not None:
                sales.append(normalized)

    if raw and not sales:
        # entries were given but none were usable — structural garbage,
        # not the blank-template case.
        raise ValueError("no usable sales provided")

    period = _pick(params, "period_label", "period", "title", "label")
    period = _clean_text(period, 100)

    notes = _pick(params, "notes", "note")
    notes = notes.strip()[:1000] if isinstance(notes, str) and notes.strip() else None

    return {
        "period_label": period,
        "currency": _currency_fmt(_pick(params, "currency")),
        "sales": sales,
        "notes": notes,
    }


# ── Builder ───────────────────────────────────────────────────────────


def _distinct(values: List[Optional[str]]) -> List[str]:
    """First-appearance distinct labels (case-insensitive dedupe —
    SUMIF criteria are case-insensitive, so 'Widget' and 'widget' are
    the same product as far as Excel is concerned)."""
    out: List[str] = []
    seen = set()
    for v in values:
        if not v:
            continue
        key = v.lower()
        if key not in seen:
            seen.add(key)
            out.append(v)
    return out


def _month_months(sales: List[dict]) -> List[Tuple[int, int]]:
    """(year, month) buckets spanning the dated sales, capped."""
    dated = [s for s in sales if s["date"]]
    if not dated:
        return []
    dmin = min(date.fromisoformat(s["date"]) for s in dated)
    dmax = max(date.fromisoformat(s["date"]) for s in dated)
    months: List[Tuple[int, int]] = []
    y, m = dmin.year, dmin.month
    while (y, m) <= (dmax.year, dmax.month) and len(months) < MAX_MONTHS:
        months.append((y, m))
        y, m = next_month(y, m)
    return months


def build_sales_tracker_spec(params: dict) -> dict:
    """Sales log + analysis workbook — every formula code-generated.

    Layout (rows computed here, never guessed by a model):

    Sales sheet:
      row 1     title text block
      row 3     table headers (start_cell A3, no table title)
      rows 4..  one row per sale; Revenue/Profit/Margin are live
                formulas (Profit/Margin blank until a cost is given)
      row N+1   totals (SUM over the exact data rows)

    Analysis sheet:
      rows 3..  topline text blocks (live cross-sheet formulas)
      then     'Revenue by Product', 'Revenue by Channel' (only when
               channels were given) and 'Revenue by Month' (only when
               dates were given) tables — every aggregation a SUMIF
               over the Sales sheet's exact row range — plus the
               revenue-by-product and revenue-by-month bar charts.
    """
    p = coerce_sales_tracker_params(params)
    sales: List[dict] = p["sales"]
    money = p["currency"] or MONEY_FMT

    has_cost = any(s["unit_cost"] is not None for s in sales)
    products = _distinct([s["product"] for s in sales])
    channels = _distinct([s["channel"] for s in sales])
    months = _month_months(sales)

    title = f"Sales Log — {p['period_label']}" if p["period_label"] else "Sales Log"

    # ── Sales log sheet ───────────────────────────────────────────────
    n = len(sales)
    template_mode = n == 0
    n_rows = n if n else MIN_ROWS  # scaffold rows in template mode
    first = 4  # header on row 3 (A3, no table title)
    last = first + n_rows - 1
    total = last + 1

    log_rows: List[List[Any]] = []
    for i, s in enumerate(sales):
        r = first + i
        log_rows.append(
            [
                s["date"],
                s["product"],
                s["channel"],
                s["customer"],
                s["quantity"],
                s["unit_price"],
                s["unit_cost"],
                f"=E{r}*F{r}",  # Revenue = qty x price
                f'=IF(G{r}="","",H{r}-E{r}*G{r})',  # Profit (only when cost given)
                f'=IF(OR(G{r}="",H{r}=0),"",I{r}/H{r})',  # Margin % (guarded)
            ]
        )
    if template_mode:
        # Blank scaffold rows: Revenue / Profit / Margin are LIVE
        # guarded formulas that stay blank until the row carries the
        # inputs they read (never invented sales — hard rule).
        for i in range(MIN_ROWS):
            r = first + i
            log_rows.append(
                [
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    f'=IF(OR($E{r}="",$F{r}=""),"",$E{r}*$F{r})',
                    f'=IF(OR($G{r}="",$H{r}=""),"",$H{r}-$E{r}*$G{r})',
                    f'=IF(OR($G{r}="",$H{r}="",$H{r}=0),"",$I{r}/$H{r})',
                ]
            )

    log_notes = (
        "Revenue = Quantity x Unit Price. Profit = Revenue - Quantity x "
        "Unit Cost and Margin = Profit / Revenue — both stay blank until a "
        "Unit Cost is typed in column G, so sales without a cost never show "
        "a fake profit (type one in and the row lights up). Rows with "
        "negative profit are highlighted red. The Analysis sheet "
        "recalculates every breakdown automatically as rows are edited or "
        "added inside the logged range."
    )
    if template_mode:
        log_notes = (
            "Blank sales-log template — type sales into the empty rows and "
            "Revenue / Profit / Margin compute themselves. " + log_notes
        )
    if p["notes"]:
        log_notes = f"{p['notes']}\n{log_notes}"

    sales_sheet: Dict[str, Any] = {
        "name": "Sales",
        "tab_color": "16304F",
        "freeze_panes": "A4",
        "column_widths": {
            "A": 12,
            "B": 24,
            "C": 15,
            "D": 20,
            "E": 10,
            "F": 12,
            "G": 12,
            "H": 14,
            "I": 14,
            "J": 10,
        },
        "text_blocks": [
            {"cell": "A1", "text": title, "bold": True, "font_size": 14},
        ],
        "tables": [
            {
                "start_cell": "A3",
                "headers": [
                    "Date",
                    "Product",
                    "Channel",
                    "Customer",
                    "Quantity",
                    "Unit Price",
                    "Unit Cost",
                    "Revenue",
                    "Profit",
                    "Margin %",
                ],
                "rows": log_rows,
                "number_formats": {
                    "A": _DATE_FMT,
                    "E": QTY_FMT,
                    "F": money,
                    "G": money,
                    "H": money,
                    "I": money,
                    "J": PCT_FMT,
                },
                "total_row": [
                    "Total",
                    None,
                    None,
                    None,
                    f"=SUM(E{first}:E{last})",
                    None,
                    None,
                    f"=SUM(H{first}:H{last})",
                    f"=SUM(I{first}:I{last})",
                    f'=IF(OR(H{total}=0,I{total}=0),"",I{total}/H{total})',
                ],
                "auto_filter": True,
            }
        ],
        "conditional_formats": [
            {
                # losing-money rows jump out
                "range": f"I{first}:I{last}",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "less_than",
                        "value": 0,
                        "fill": "FFC7CE",
                        "font_color": "9C0006",
                    }
                ],
            }
        ],
        "notes": log_notes,
    }

    # ── Analysis sheet ─────────────────────────────────────────────────
    blocks: List[dict] = [
        {"cell": "A1", "text": "Sales Analysis", "bold": True, "font_size": 14},
        {"cell": "A3", "text": "Sales Logged"},
        {
            "cell": "B3",
            "text": f"=COUNTA(Sales!$B${first}:$B${last})",
            "number_format": _INT_FMT,
        },
        {"cell": "A4", "text": "Units Sold"},
        {
            "cell": "B4",
            "text": f"=SUM(Sales!$E${first}:$E${last})",
            "number_format": QTY_FMT,
        },
        {"cell": "A5", "text": "Total Revenue"},
        {
            "cell": "B5",
            "text": f"=SUM(Sales!$H${first}:$H${last})",
            "number_format": money,
            "bold": True,
        },
    ]
    row = 6
    if has_cost:
        blocks += [
            {"cell": "A6", "text": "Total Profit"},
            {
                "cell": "B6",
                "text": f"=SUM(Sales!$I${first}:$I${last})",
                "number_format": money,
                "bold": True,
            },
        ]
        row = 7
    blocks += [
        {"cell": f"A{row}", "text": "Average Order Value"},
        {
            "cell": f"B{row}",
            "text": "=IF($B$3=0,0,$B$5/$B$3)",
            "number_format": money,
        },
    ]
    topline_end = row

    if template_mode:
        # ── Template-mode Analysis ──────────────────────────────────
        # Topline stats (live over the blank log rows → 0) + a guarded
        # Revenue-by-Product scaffold: blank label rows whose SUMIFs
        # aggregate the logged sales as soon as a product name is typed
        # into column A. No charts, no channel/month breakdowns (there
        # is nothing to chart and no channel/date vocabulary given).
        anchor = topline_end + 2
        prod_first = anchor + 2  # title row anchor, header anchor+1
        prod_last = prod_first + MIN_BREAKDOWN_ROWS - 1
        prod_total = prod_last + 1
        crit = f"Sales!$B${first}:$B${last}"
        units_rng = f"Sales!$E${first}:$E${last}"
        rev_rng = f"Sales!$H${first}:$H${last}"

        prod_rows: List[List[Any]] = []
        for i in range(MIN_BREAKDOWN_ROWS):
            r = prod_first + i
            prod_rows.append(
                [
                    None,
                    f'=IF($A{r}="","",SUMIF({crit},$A{r},{units_rng}))',
                    f'=IF($A{r}="","",SUMIF({crit},$A{r},{rev_rng}))',
                    f'=IF($A{r}="","",IF($C${prod_total}=0,0,C{r}/$C${prod_total}))',
                ]
            )

        analysis_sheet: Dict[str, Any] = {
            "name": "Analysis",
            "tab_color": "1B3A5C",
            "column_widths": {"A": 24, "B": 12, "C": 15, "D": 15, "E": 15},
            "text_blocks": blocks,
            "tables": [
                {
                    "start_cell": f"A{anchor}",
                    "title": "Revenue by Product",
                    "headers": [
                        "Product",
                        "Units Sold",
                        "Revenue",
                        "Share of Revenue",
                    ],
                    "rows": prod_rows,
                    "number_formats": {
                        "B": QTY_FMT,
                        "C": money,
                        "D": PCT_FMT,
                    },
                    "total_row": [
                        "Total",
                        f"=SUM(B{prod_first}:B{prod_last})",
                        f"=SUM(C{prod_first}:C{prod_last})",
                        f"=SUM(D{prod_first}:D{prod_last})",
                    ],
                }
            ],
            "notes": (
                "Blank-template mode: the topline stats read 0 until sales "
                "are logged on the Sales sheet. Type product names into the "
                "blank rows of Revenue by Product and the live SUMIFs "
                "aggregate every matching logged sale (units, revenue, "
                "share). Log sales with a Unit Cost filled in and profit "
                "columns appear on the Sales sheet; re-run with your sales "
                "listed and the full channel / month breakdowns and charts "
                "are generated."
            ),
        }
        return {
            "filename": "sales_tracker.xlsx",
            "sheets": [sales_sheet, analysis_sheet],
        }

    tables: List[dict] = []
    charts: List[dict] = []

    def _label_table(
        anchor: int,
        table_title: str,
        label_header: str,
        labels: List[str],
        crit_col: str,
    ) -> Tuple[dict, int, int]:
        """Product/channel breakdown: Label | Units | Revenue | [Profit]
        | Share — every aggregation a SUMIF over the log's exact rows."""
        first_r = anchor + 2  # title row anchor, header anchor+1
        last_r = first_r + len(labels) - 1
        total_r = last_r + 1
        crit = f"Sales!${crit_col}${first}:${crit_col}${last}"
        units_rng = f"Sales!$E${first}:$E${last}"
        rev_rng = f"Sales!$H${first}:$H${last}"
        profit_rng = f"Sales!$I${first}:$I${last}"

        share_col = "E" if has_cost else "D"
        rows: List[List[Any]] = []
        for i, label in enumerate(labels):
            r = first_r + i
            row_vals: List[Any] = [
                label,
                f"=SUMIF({crit},$A{r},{units_rng})",
                f"=SUMIF({crit},$A{r},{rev_rng})",
            ]
            if has_cost:
                row_vals.append(f"=SUMIF({crit},$A{r},{profit_rng})")
            row_vals.append(f"=IF($C${total_r}=0,0,C{r}/$C${total_r})")
            rows.append(row_vals)

        total_vals: List[Any] = [
            "Total",
            f"=SUM(B{first_r}:B{last_r})",
            f"=SUM(C{first_r}:C{last_r})",
        ]
        if has_cost:
            total_vals.append(f"=SUM(D{first_r}:D{last_r})")
        total_vals.append(f"=SUM({share_col}{first_r}:{share_col}{last_r})")

        numfmts = {"B": QTY_FMT, "C": money, share_col: PCT_FMT}
        if has_cost:
            numfmts["D"] = money

        table = {
            "start_cell": f"A{anchor}",
            "title": table_title,
            "headers": (
                [label_header, "Units Sold", "Revenue", "Profit", "Share of Revenue"]
                if has_cost
                else [label_header, "Units Sold", "Revenue", "Share of Revenue"]
            ),
            "rows": rows,
            "number_formats": numfmts,
            "total_row": total_vals,
        }
        return table, first_r, last_r

    anchor = topline_end + 2
    product_table, prod_first, prod_last = _label_table(
        anchor, "Revenue by Product", "Product", products, "B"
    )
    tables.append(product_table)

    chart_anchor_row = 3
    charts.append(
        {
            "type": "bar",
            "title": "Revenue by Product",
            "anchor": f"G{chart_anchor_row}",
            "width": 16,
            "height": 9,
            "categories_range": f"Analysis!$A${prod_first}:$A${prod_last}",
            "series": [
                {
                    "name": "Revenue",
                    "values_range": f"Analysis!$C${prod_first}:$C${prod_last}",
                }
            ],
            "value_numfmt": money,
        }
    )

    anchor = prod_last + 3  # total row + one blank row
    if channels:
        channel_table, _ch_first, _ch_last = _label_table(
            anchor, "Revenue by Channel", "Channel", channels, "C"
        )
        tables.append(channel_table)
        anchor = _ch_last + 3

    if months:
        first_r = anchor + 2
        last_r = first_r + len(months) - 1
        total_r = last_r + 1
        date_rng = f"Sales!$A${first}:$A${last}"
        rev_rng = f"Sales!$H${first}:$H${last}"
        profit_rng = f"Sales!$I${first}:$I${last}"
        share_col = "D" if has_cost else "C"  # Month | Revenue | [Profit] | Share

        month_rows: List[List[Any]] = []
        for i, (y, m) in enumerate(months):
            r = first_r + i
            ny, nm = next_month(y, m)
            label = date(y, m, 1).strftime("%b %Y")
            row_vals: List[Any] = [
                label,
                f'=SUMIF({date_rng},">="&DATE({y},{m},1),{rev_rng})'
                f'-SUMIF({date_rng},">="&DATE({ny},{nm},1),{rev_rng})',
            ]
            if has_cost:
                row_vals.append(
                    f'=SUMIF({date_rng},">="&DATE({y},{m},1),{profit_rng})'
                    f'-SUMIF({date_rng},">="&DATE({ny},{nm},1),{profit_rng})'
                )
            row_vals.append(f"=IF($B${total_r}=0,0,B{r}/$B${total_r})")
            month_rows.append(row_vals)

        total_vals: List[Any] = ["Total", f"=SUM(B{first_r}:B{last_r})"]
        if has_cost:
            total_vals.append(f"=SUM(C{first_r}:C{last_r})")
        total_vals.append(f"=SUM({share_col}{first_r}:{share_col}{last_r})")

        numfmts = {"B": money, share_col: PCT_FMT}
        if has_cost:
            numfmts["C"] = money

        tables.append(
            {
                "start_cell": f"A{anchor}",
                "title": "Revenue by Month",
                "headers": (
                    ["Month", "Revenue", "Profit", "Share of Revenue"]
                    if has_cost
                    else ["Month", "Revenue", "Share of Revenue"]
                ),
                "rows": month_rows,
                "number_formats": numfmts,
                "total_row": total_vals,
            }
        )
        charts.append(
            {
                "type": "bar",
                "title": "Revenue by Month",
                "anchor": "G22",
                "width": 16,
                "height": 9,
                "categories_range": f"Analysis!$A${first_r}:$A${last_r}",
                "series": [
                    {
                        "name": "Revenue",
                        "values_range": f"Analysis!$B${first_r}:$B${last_r}",
                    }
                ],
                "value_numfmt": money,
            }
        )

    analysis_notes = (
        "Every breakdown is a live SUMIF over the Sales sheet's exact "
        "logged rows — edit, add or remove sales inside that range and "
        "these tables and charts update. Share of Revenue is each row's "
        "share of that table's total. Sales without a channel (or "
        "without a date) are not counted in the channel (or month) "
        "breakdown; their totals therefore cover only the categorized "
        "sales."
    )

    analysis_sheet: Dict[str, Any] = {
        "name": "Analysis",
        "tab_color": "1B3A5C",
        "column_widths": {"A": 24, "B": 12, "C": 15, "D": 15, "E": 15},
        "text_blocks": blocks,
        "tables": tables,
        "notes": analysis_notes,
    }
    if charts:
        analysis_sheet["charts"] = charts

    return {
        "filename": "sales_tracker.xlsx",
        "sheets": [sales_sheet, analysis_sheet],
    }


# ── Standard pattern entry points (used by the dynamic registry) ──────

coerce_params = coerce_sales_tracker_params
build_spec = build_sales_tracker_spec
