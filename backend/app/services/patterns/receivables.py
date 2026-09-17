"""
Receivables pattern — accounts-receivable tracker.

Deterministic: the classifier extracts the invoice list (customer,
number, dates, amount, paid); this module lays out the tracker and
computes every formula reference from the actual row numbers of the
layout it emits.

Layout (Receivables sheet):
  row 1      "Accounts Receivable" title (bold, 14)
  row 2      as-of anchor: A2 "As Of" + B2 = the as_of_date param, or
             =TODAY() when not stated. Every days-overdue / status
             formula reads $B$2, so the whole tracker stays live and a
             user can pin a date by overwriting B2.
  row 4      table headers (start_cell A4, no table title)
  rows 5..N  data: Customer | Invoice # | Date | Due Date | Amount |
             Paid | Outstanding | Days Overdue | Status
               Outstanding = Amount - Paid
               Days Overdue = IF(due="", 0, MAX(0, $B$2 - due))
                 (0 floor: not-yet-due invoices read 0, missing due
                 dates read 0 — never a bogus negative serial)
               Status = nested IF:
                 Paid | Partial | Overdue | Current
  row N+1    totals: SUM over the exact data rows for Amount / Paid /
             Outstanding.

Summary sheet (steel tab): aging buckets (Current / 1-30 / 31-60 /
61-90 / 90+) via COUNTIF(S)/SUMIF(S) over the Days Overdue column, a
per-customer rollup via COUNTIF/SUMIF, and two charts (outstanding by
aging bucket, outstanding by customer).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.services.patterns.utils import (
    MONEY_FMT,
    _currency_fmt,
    _pick,
    to_iso_date,
    to_number,
)

# Registry key — must match the pattern stanza in
# prompts/pattern_classifier.md.
PATTERN_NAME = "receivables"

PATTERN_DESCRIPTION = (
    "Accounts-receivable tracker: many invoices with paid/outstanding "
    "amounts, days overdue, status words and aging buckets."
)

# Routing keywords/stems — drive the cheap pre-gate and the classifier
# shortlist (see excel_gen._shortlist_patterns).
PATTERN_KEYWORDS = (
    "receivable",
    "outstanding",
    "unpaid",
    "owe",
    "debtor",
    "aging",
    "aged",
    "overdue",
)

MAX_INVOICES = 300

# Aging bucket edges (days overdue; Current = not past due).
_BUCKETS = (
    ("Current (not due)", 0, 0),
    ("1-30 days", 1, 30),
    ("31-60 days", 31, 60),
    ("61-90 days", 61, 90),
    ("Over 90 days", 91, None),  # None → open-ended "> 90"
)


def _clean_str(value: Any, cap: int) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s[:cap] if s else None


def coerce_receivables_params(params: dict) -> dict:
    """Validate + normalize classifier output. Raises ValueError when
    required params are missing (the invoice list, a customer, an
    amount).

    Accepts alias keys, quoted/currency numbers; ``paid_amount``
    defaults to 0 (nothing paid); ``as_of_date`` stays ``None`` when not
    stated (the workbook then anchors on a live =TODAY() cell).
    """
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    raw = None
    for key in ("invoices", "invoice_list", "invoices_list"):
        if isinstance(params.get(key), list):
            raw = params[key]
            break
    if raw is None:
        raise ValueError("invoices missing")

    invoices: List[Dict[str, Any]] = []
    for entry in raw[:MAX_INVOICES]:
        if not isinstance(entry, dict):
            continue
        customer = _clean_str(
            _pick(entry, "customer", "client", "customer_name", "debtor", "buyer"),
            60,
        )
        if customer is None:
            raise ValueError("invoice without a customer")
        amount = to_number(_pick(entry, "amount", "total", "invoice_amount", "value"))
        if amount is None:
            raise ValueError("invoice without an amount")
        paid = to_number(_pick(entry, "paid_amount", "paid", "amount_paid", "payments"))
        invoices.append(
            {
                "customer": customer,
                "invoice_number": _clean_str(
                    _pick(
                        entry,
                        "invoice_number",
                        "invoice_no",
                        "number",
                        "inv_no",
                    ),
                    24,
                ),
                "date": to_iso_date(_pick(entry, "date", "invoice_date")),
                "due_date": to_iso_date(_pick(entry, "due_date", "due", "payment_due")),
                "amount": max(amount, 0.0),
                "paid_amount": max(paid, 0.0) if paid is not None else 0.0,
            }
        )
    if not invoices:
        raise ValueError("no usable invoices")

    notes = _pick(params, "notes", "note")
    notes = notes.strip()[:1000] if isinstance(notes, str) and notes.strip() else None

    return {
        "as_of_date": to_iso_date(_pick(params, "as_of_date", "as_of")),
        "currency": _currency_fmt(_pick(params, "currency")),
        "invoices": invoices,
        "notes": notes,
    }


def _unique_customers(invoices: List[dict]) -> List[str]:
    seen: List[str] = []
    for inv in invoices:
        if inv["customer"] not in seen:
            seen.append(inv["customer"])
    return seen


def build_receivables_spec(params: dict) -> dict:
    """AR tracker — every formula code-generated from the emitted rows.

    No ROUND() in the formulas: display rounding belongs to the number
    formats. Date math is live ($B$2 - due date) so the tracker keeps
    computing as days pass or the user pins a different as-of date.
    """
    p = coerce_receivables_params(params)
    invoices: List[dict] = p["invoices"]
    as_of: Optional[str] = p["as_of_date"]
    money = p["currency"] or MONEY_FMT

    n = len(invoices)
    customers = _unique_customers(invoices)
    m = len(customers)

    # ── Layout math (Receivables sheet) ────────────────────────────────
    # title row 1, as-of row 2, blank 3, headers row 4, data 5..4+n,
    # total row 5+n.
    header_row = 4
    first_data = header_row + 1
    last_data = header_row + n
    total_row = last_data + 1  # noqa

    rows: List[List[Any]] = []
    for i, inv in enumerate(invoices):
        r = first_data + i
        rows.append(
            [
                inv["customer"],
                inv["invoice_number"],
                inv["date"],
                inv["due_date"],
                inv["amount"],
                inv["paid_amount"],
                f"=E{r}-F{r}",
                f'=IF(D{r}="",0,MAX(0,$B$2-D{r}))',
                (
                    f'=IF(F{r}>=E{r},"Paid",'
                    f'IF(F{r}>0,"Partial",'
                    f'IF(AND(D{r}<>"",D{r}<$B$2),"Overdue","Current")))'
                ),
            ]
        )

    blocks: List[dict] = [
        {"cell": "A1", "text": "Accounts Receivable", "bold": True, "font_size": 14},
        {"cell": "A2", "text": "As Of"},
        {
            "cell": "B2",
            "text": as_of if as_of else "=TODAY()",
            "bold": True,
            "number_format": "yyyy-mm-dd",
        },
    ]

    status_cf = {
        "range": f"I{first_data}:I{last_data}",
        "rules": [
            {
                "type": "cell_is",
                "operator": "equal",
                "value": "Overdue",
                "fill": "FFC7CE",
                "font_color": "9C0006",
                "stop_if_true": True,
            },
            {
                "type": "cell_is",
                "operator": "equal",
                "value": "Partial",
                "fill": "FFF2CC",
                "font_color": "7F6000",
                "stop_if_true": True,
            },
            {
                "type": "cell_is",
                "operator": "equal",
                "value": "Paid",
                "fill": "C6EFCE",
                "font_color": "1E4620",
            },
        ],
    }

    receivables_sheet: Dict[str, Any] = {
        "name": "Receivables",
        "tab_color": "16304F",
        "freeze_panes": f"A{first_data}",
        "column_widths": {
            "A": 26,
            "B": 14,
            "C": 13,
            "D": 13,
            "E": 14,
            "F": 14,
            "G": 15,
            "H": 13,
            "I": 12,
        },
        "text_blocks": blocks,
        "tables": [
            {
                "start_cell": f"A{header_row}",
                "headers": [
                    "Customer",
                    "Invoice #",
                    "Date",
                    "Due Date",
                    "Amount",
                    "Paid",
                    "Outstanding",
                    "Days Overdue",
                    "Status",
                ],
                "rows": rows,
                "number_formats": {
                    "C": "yyyy-mm-dd",
                    "D": "yyyy-mm-dd",
                    "E": money,
                    "F": money,
                    "G": money,
                    "H": "0",
                },
                "alignments": {"H": "center", "I": "center"},
                "total_row": [
                    "Total",
                    None,
                    None,
                    None,
                    f"=SUM(E{first_data}:E{last_data})",
                    f"=SUM(F{first_data}:F{last_data})",
                    f"=SUM(G{first_data}:G{last_data})",
                    None,
                    None,
                ],
            }
        ],
        "conditional_formats": [status_cf],
        "notes": (
            (f"{p['notes']} " if p["notes"] else "")
            + "Outstanding = Amount - Paid. Days Overdue = days past the due "
            "date as of the anchor cell B2 (live =TODAY() unless the request "
            "pinned an as-of date — overwrite B2 to freeze a date). Status: "
            "Paid when the paid amount covers the invoice, Partial when "
            "something was paid, Overdue when the due date passed with "
            "nothing paid, Current otherwise. The Summary sheet carries the "
            "aging buckets and per-customer rollups."
        ),
    }

    # ── Summary sheet ──────────────────────────────────────────────────
    # Aging table: title row 1, blank 2, headers row 3, data 4..8 (five
    # buckets), total row 9. Customer table: two blank rows below →
    # headers row 12, data 13..12+m, total row 13+m.
    days_col = f"Receivables!$H${first_data}:$H${last_data}"
    out_col = f"Receivables!$G${first_data}:$G${last_data}"
    cust_col = f"Receivables!$A${first_data}:$A${last_data}"

    aging_header_row = 3
    aging_first = aging_header_row + 1
    aging_last = aging_first + len(_BUCKETS) - 1
    aging_total = aging_last + 1

    aging_rows: List[List[Any]] = []
    for k, (label, lo, hi) in enumerate(_BUCKETS):
        r = aging_first + k
        if hi is None:  # open-ended bucket
            crit = ">90"
            count = f'=COUNTIF({days_col},"{crit}")'
            amount = f'=SUMIF({days_col},"{crit}",{out_col})'
        elif hi == 0:  # not due yet / due today
            count = f"=COUNTIF({days_col},0)"
            amount = f"=SUMIF({days_col},0,{out_col})"
        else:
            count = f'=COUNTIFS({days_col},">={lo}",{days_col},"<={hi}")'
            amount = f'=SUMIFS({out_col},{days_col},">={lo}",{days_col},"<={hi}")'
        aging_rows.append([label, count, amount])

    cust_header_row = aging_total + 3
    cust_first = cust_header_row + 1
    cust_last = cust_header_row + m
    cust_total = cust_last + 1

    cust_rows: List[List[Any]] = []
    for j, cust in enumerate(customers):
        r = cust_first + j
        cust_rows.append(
            [
                cust,
                f"=COUNTIF({cust_col},$A{r})",
                f"=SUMIF({cust_col},$A{r},"
                f"Receivables!$E${first_data}:$E${last_data})",
                f"=SUMIF({cust_col},$A{r},"
                f"Receivables!$F${first_data}:$F${last_data})",
                f"=SUMIF({cust_col},$A{r},{out_col})",
            ]
        )

    charts: List[dict] = [
        {
            "type": "bar",
            "title": "Outstanding by Aging Bucket",
            "anchor": "G3",
            "width": 15,
            "height": 9,
            "categories_range": f"Summary!A{aging_first}:A{aging_last}",
            "series": [
                {
                    "name": "Outstanding",
                    "values_range": f"Summary!C{aging_first}:C{aging_last}",
                }
            ],
            "value_numfmt": money,
        }
    ]
    if m >= 2:
        charts.append(
            {
                "type": "bar_h",
                "title": "Outstanding by Customer",
                # below the aging chart AND the customer table
                "anchor": f"G{max(24, cust_total + 3)}",
                "width": 15,
                "height": 9,
                "categories_range": f"Summary!A{cust_first}:A{cust_last}",
                "series": [
                    {
                        "name": "Outstanding",
                        "values_range": f"Summary!E{cust_first}:E{cust_last}",
                    }
                ],
                "value_numfmt": money,
            }
        )

    summary_sheet: Dict[str, Any] = {
        "name": "Summary",
        "tab_color": "1B3A5C",
        "column_widths": {"A": 24, "B": 12, "C": 15, "D": 15, "E": 15},
        "text_blocks": [
            {
                "cell": "A1",
                "text": "AR Summary",
                "bold": True,
                "font_size": 12,
                "font_color": "16304F",
            }
        ],
        "tables": [
            {
                "start_cell": f"A{aging_header_row}",
                "headers": ["Bucket", "Invoices", "Outstanding"],
                "rows": aging_rows,
                "number_formats": {"B": "0", "C": money},
                "total_row": [
                    "Total",
                    f"=SUM(B{aging_first}:B{aging_last})",
                    f"=SUM(C{aging_first}:C{aging_last})",
                ],
            },
            {
                "start_cell": f"A{cust_header_row}",
                "headers": ["Customer", "Invoices", "Billed", "Paid", "Outstanding"],
                "rows": cust_rows,
                "number_formats": {
                    "B": "0",
                    "C": money,
                    "D": money,
                    "E": money,
                },
                "total_row": [
                    "Total",
                    f"=SUM(B{cust_first}:B{cust_last})",
                    f"=SUM(C{cust_first}:C{cust_last})",
                    f"=SUM(D{cust_first}:D{cust_last})",
                    f"=SUM(E{cust_first}:E{cust_last})",
                ],
            },
        ],
        "charts": charts,
        "notes": (
            "Aging buckets count and sum the Outstanding column of the "
            "Receivables sheet by Days Overdue (Current = not past due as "
            "of Receivables!B2). The per-customer rollup uses COUNTIF/SUMIF "
            "over the same table — everything updates when the tracker "
            "changes."
        ),
    }

    return {
        "filename": "receivables.xlsx",
        "sheets": [receivables_sheet, summary_sheet],
    }


# ── Standard pattern entry points (used by the dynamic registry) ──────

coerce_params = coerce_receivables_params
build_spec = build_receivables_spec
