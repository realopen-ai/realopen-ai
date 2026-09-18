"""
Amortization pattern — loan / mortgage / credit repayment schedule.

Deterministic: the classifier extracts scalar parameters (the thing
small models are good at); this module builds the whole workbook spec
in code, with every formula reference computed from the actual row
numbers of the layout it emits — off-by-N row math is impossible by
construction.

Layout (Amortization sheet):
  row 1      title
  rows 2-5   inputs: B2 loan, B3 rate, B4 term, B5 payment (PMT)
  row 7      table headers (start_cell A7, no table title)
  rows 8..   data: Month | Payment | Principal | Interest | Balance
  row N+1    totals (SUM over the exact data rows)

The interest chain references the PREVIOUS row's balance, the first
row references the loan input — rows computed here, never guessed by
a model.
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.services.patterns.utils import (
    MONEY_FMT,
    MONTH_FMT,
    _currency_fmt,
    _first_of_next_month,
    _next_month,
    _pick,
    to_int,
    to_iso_date,
    to_number,
    to_rate,
)

# Registry key — must match the pattern stanza in
# prompts/pattern_classifier.md.
PATTERN_NAME = "amortization"

PATTERN_DESCRIPTION = (
    "Loan / mortgage / credit repayment schedule: monthly payment split "
    "into interest + principal over time with a running balance."
)

# Routing keywords/stems — drive the cheap pre-gate and the classifier
# shortlist (see excel_gen._shortlist_patterns).
PATTERN_KEYWORDS = (
    "loan",
    "mortgage",
    "amortiz",
    "repay",
    "interest",
    "installment",
    "credit",
    "debt",
    "refund",
    "payment schedule",
)

MAX_TERM_MONTHS = 600  # 50 years


def coerce_amortization_params(params: dict) -> dict:
    """Validate + normalize classifier params; raises ValueError."""
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    loan = to_number(_pick(params, "loan_amount", "principal", "amount", "loan"))
    if loan is None or loan <= 0:
        raise ValueError("loan_amount missing or not positive")
    if loan > 1e12:
        loan = 1e12

    rate = to_rate(_pick(params, "annual_rate", "interest_rate", "rate"))
    if rate is None:
        raise ValueError("annual_rate missing")
    if rate < 0:
        rate = 0.0
    if rate > 1.0:
        rate = 1.0

    months = to_int(_pick(params, "term_months", "months", "duration_months"))
    if months is None:
        years = to_number(_pick(params, "term_years", "years"))
        if years is not None:
            months = int(round(years * 12))
    if months is None or months < 1:
        raise ValueError("term (months or years) missing or invalid")
    months = min(months, MAX_TERM_MONTHS)

    start = to_iso_date(_pick(params, "start_date", "first_payment_date"))
    if start is None:
        y, m = _first_of_next_month()
        start = f"{y:04d}-{m:02d}-01"

    payment = to_number(_pick(params, "payment", "monthly_payment"))
    if payment is not None and payment <= 0:
        payment = None

    return {
        "loan_amount": loan,
        "annual_rate": rate,
        "term_months": months,
        "start_date": start,
        "payment": payment,
        "currency": _currency_fmt(_pick(params, "currency")),
    }


def build_amortization_spec(params: dict) -> dict:
    """Loan amortization schedule — every formula code-generated.

    No ROUND() in the formulas on purpose: display rounding is done by
    the money number format; rounding inside formulas accumulates
    decimal-level drift (e.g. a balance ending at 0.14 instead of 0).
    """
    p = coerce_amortization_params(params)
    loan, rate, n = p["loan_amount"], p["annual_rate"], p["term_months"]
    start_iso, pay_override, cur = p["start_date"], p["payment"], p["currency"]

    money = cur or MONEY_FMT
    first_data = 8
    last_data = first_data + n - 1
    total_row = last_data + 1

    sy, sm, _sd = (int(x) for x in start_iso.split("-"))
    rows: List[List[Any]] = []
    y, mo = sy, sm
    for i in range(n):
        r = first_data + i
        rows.append(
            [
                f"{y:04d}-{mo:02d}-01",
                "=$B$5",
                f"=B{r}-D{r}",
                ("=$B$2*$B$3/12" if i == 0 else f"=E{r - 1}*$B$3/12"),
                ("=$B$2-C%d" % r if i == 0 else f"=E{r - 1}-C{r}"),
            ]
        )
        y, mo = _next_month(y, mo)

    payment_cell: Any = (
        pay_override if pay_override is not None else "=-PMT(B3/12,B4,B2)"
    )

    blocks: List[dict] = [
        {
            "cell": "A1",
            "text": "Loan Amortization Schedule",
            "bold": True,
            "font_size": 14,
        },
        {"cell": "A2", "text": "Loan Amount"},
        {"cell": "B2", "text": loan},
        {"cell": "A3", "text": "Annual Interest Rate"},
        {"cell": "B3", "text": rate},
        {"cell": "A4", "text": "Term (Months)"},
        {"cell": "B4", "text": n},
        {"cell": "A5", "text": "Monthly Payment"},
        # B5 carries the SAME money format as the schedule columns so
        # the payment reads as currency (e.g. $766.23), not a raw float.
        {
            "cell": "B5",
            "text": payment_cell,
            "bold": True,
            "number_format": money,
        },
    ]

    sched: Dict[str, Any] = {
        "name": "Amortization",
        "tab_color": "16304F",
        "freeze_panes": "A8",
        "column_widths": {"A": 14, "B": 14, "C": 14, "D": 13, "E": 18},
        "text_blocks": blocks,
        "tables": [
            {
                "start_cell": "A7",
                "headers": [
                    "Month",
                    "Payment",
                    "Principal",
                    "Interest",
                    "Remaining Balance",
                ],
                "rows": rows,
                "number_formats": {
                    "A": MONTH_FMT,
                    "B": money,
                    "C": money,
                    "D": money,
                    "E": money,
                },
                "total_row": [
                    "Total",
                    "=SUM(B{first_row}:B{last_row})",
                    "=SUM(C{first_row}:C{last_row})",
                    "=SUM(D{first_row}:D{last_row})",
                    "",
                ],
            }
        ],
        "charts": [
            {
                "type": "line",
                "title": "Remaining Balance",
                "anchor": "G2",
                "width": 16,
                "height": 9,
                "categories_range": f"Amortization!A{first_data}:A{last_data}",
                "series": [
                    {
                        "name": "Remaining Balance",
                        "values_range": f"Amortization!E{first_data}:E{last_data}",
                    }
                ],
            }
        ],
        "notes": (
            "Edit B2 (loan), B3 (rate) or B4 (term) and everything recalculates: "
            "payment = -PMT(rate/12, term, loan), interest = previous balance x "
            "rate/12, principal = payment - interest, balance = previous balance - principal."
        ),
    }

    summary: Dict[str, Any] = {
        "name": "Summary",
        "tab_color": "C9A227",
        "column_widths": {"A": 26, "B": 16},
        "tables": [
            {
                "start_cell": "A3",
                "title": "Loan Summary",
                "headers": ["Item", "Value"],
                "rows": [
                    ["Loan Amount", "=Amortization!$B$2"],
                    ["Annual Interest Rate", '=TEXT(Amortization!$B$3,"0.00%")'],
                    ["Term (Months)", "=Amortization!$B$4"],
                    ["Monthly Payment", "=Amortization!$B$5"],
                    ["Total Paid", f"=Amortization!$B${total_row}"],
                    ["Total Interest", f"=Amortization!$D${total_row}"],
                    ["Final Balance", f"=Amortization!$E${last_data}"],
                ],
                "number_formats": {"B": money},
                "zebra": True,
            }
        ],
        "notes": (
            "All values reference the Amortization sheet — change the inputs "
            "there and this summary updates."
        ),
    }

    return {
        "filename": "loan_amortization_schedule.xlsx",
        "sheets": [sched, summary],
    }


# ── Standard pattern entry points (used by the dynamic registry) ──────

coerce_params = coerce_amortization_params
build_spec = build_amortization_spec
