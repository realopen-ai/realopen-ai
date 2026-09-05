"""
Deterministic workbook templates for high-frequency spreadsheet requests.

WHY THIS MODULE EXISTS
──────────────────────
Small models cannot reliably hand-write formula lattices: they emit
formulas whose row references don't match the layout that actually
gets rendered (off by one, two, three rows…). Post-hoc healing can
only patch signatures we can detect structurally (e.g. references
into the header row).

The fix for the common cases: stop asking the model to write those
formulas at all. A classifier LLM call extracts a handful of scalar
parameters from the brief (numbers, names, line items — the thing
models ARE good at), and this module builds the workbook JSON spec
in code. Every formula reference is computed from the actual row
numbers of the layout emitted by the same code, so an off-by-N is
impossible by construction.

Contract: every builder takes a plain params dict (already coerced
by ``coerce_*`` helpers or trusted literals) and returns a RAW
workbook spec dict in the exact schema ``excel_gen`` validates +
normalizes + converts. Builders never import from ``excel_gen``
(no circular import); they raise ``ValueError`` when required
parameters are missing/invalid — the caller falls back to the
AI-generated spec path in that case.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

# ── Limits ────────────────────────────────────────────────────────────

MAX_TERM_MONTHS = 600  # 50 years
MAX_INVOICE_ITEMS = 200
MAX_BUDGET_LINES = 300

MONEY_FMT = "#,##0.00"
QTY_FMT = "#,##0.##"
MONTH_FMT = "mmm yyyy"

_CURRENCY_SYMBOLS = {
    "USD": '"$"#,##0.00',
    "EUR": '#,##0.00" €"',
    "GBP": '"£"#,##0.00',
    "JPY": '"¥"#,##0',
    "CHF": '"CHF "#,##0.00',
    "CAD": '"CA$"#,##0.00',
    "AUD": '"A$"#,##0.00',
    "MAD": "#,##0.00" '" DH"',
    "INR": '"₹"#,##0.00',
    "CNY": '"¥"#,##0.00',
    "BRL": '"R$"#,##0.00',
    "ZAR": '"R"#,##0.00',
}

_NUM_CLEAN_RE = re.compile(r"[^\d.\-]")
_PERCENT_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*%\s*$")
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")


# ── Param coercion (models quote numbers, add %, currency…) ──────────


def to_number(value: Any) -> Optional[float]:
    """Coerce a classifier param to float; None when not numeric.

    Accepts ints/floats, numeric strings, currency strings
    ("$25,000", "25 000 €"), and keeps percents as their face value
    (caller decides the /100 semantics).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        m = _PERCENT_RE.match(s)
        if m:
            return float(m.group(1))
        s = s.replace("\u00a0", "").replace(" ", "")
        s = _NUM_CLEAN_RE.sub("", s)
        if not s or s in ("-", ".", "-."):
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def to_rate(value: Any) -> Optional[float]:
    """Annual interest rate → decimal fraction (0.065 for 6.5%).

    Strings with "%" are divided by 100. Bare numbers > 1 are treated
    as percent (a rate of 100%+ doesn't occur in practice); ≤ 1 stays
    a decimal fraction.
    """
    had_percent = isinstance(value, str) and "%" in value
    n = to_number(value)
    if n is None:
        return None
    if had_percent or n > 1.0:
        return n / 100.0
    return n


def to_int(value: Any) -> Optional[int]:
    n = to_number(value)
    if n is None or abs(n - round(n)) > 1e-9:
        return None
    return int(round(n))


def to_iso_date(value: Any) -> Optional[str]:
    if isinstance(value, str):
        m = _ISO_DATE_RE.match(value.strip())
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            try:
                date(y, mo, d)
                return f"{y:04d}-{mo:02d}-{d:02d}"
            except ValueError:
                return None
    return None


def _pick(params: dict, *keys: str) -> Any:
    for k in keys:
        if k in params and params[k] is not None:
            return params[k]
    return None


def _next_month(year: int, month: int) -> Tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _currency_fmt(currency: Any) -> Optional[str]:
    if not isinstance(currency, str) or not currency.strip():
        return None
    return _CURRENCY_SYMBOLS.get(currency.strip().upper())


def _first_of_next_month(today: Optional[date] = None) -> Tuple[int, int]:
    today = today or date.today()
    return _next_month(today.year, today.month)


# ══════════════════════════════════════════════════════════════════════
# Amortization template
# ══════════════════════════════════════════════════════════════════════


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

    Layout (Amortization sheet):
      row 1      title
      rows 2-5   inputs: B2 loan, B3 rate, B4 term, B5 payment (PMT)
      row 7      table headers (start_cell A7, no table title)
      rows 8..   data: Month | Payment | Principal | Interest | Balance
      row N+1    totals (SUM over the exact data rows)
    The interest chain references the PREVIOUS row's balance, the
    first row references the loan input — rows computed here, never
    guessed by a model.
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
        {"cell": "B5", "text": payment_cell, "bold": True},
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
        "notes" : (
            "Edit B2 (loan), B3 (rate) or B4 (term) and everything recalculates: "
            "payment = -PMT(rate/12, term, loan), interest = previous balance x "
            "rate/12, principal = payment - interest, balance = previous balance - principal."
        )
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


# ══════════════════════════════════════════════════════════════════════
# Invoice template
# ══════════════════════════════════════════════════════════════════════


def _coerce_line_items(
    params: dict, aliases: Tuple[str, ...], label_keys: Tuple[str, ...]
) -> List[Tuple[str, float, float]]:
    raw = None
    for key in aliases:
        if isinstance(params.get(key), list):
            raw = params[key]
            break
    if raw is None:
        raise ValueError("line items missing")
    items: List[Tuple[str, float, float]] = []
    for entry in raw[:MAX_INVOICE_ITEMS]:
        if isinstance(entry, dict):
            desc = _pick(entry, *label_keys)
            qty = to_number(_pick(entry, "quantity", "qty", "count")) or 1.0
            price = to_number(_pick(entry, "unit_price", "price", "rate"))
        elif isinstance(entry, list) and len(entry) >= 2:
            desc = entry[0]
            qty = (
                to_number(entry[1])
                if len(entry) > 2 and to_number(entry[1]) is not None
                else 1.0
            )
            price = to_number(entry[-1])
        else:
            continue
        if desc is None or price is None:
            continue
        desc = str(desc).strip()
        if not desc:
            continue
        if qty is None or qty <= 0:
            qty = 1.0
        if price < 0:
            price = 0.0
        items.append((desc, qty, price))
    if not items:
        raise ValueError("no usable line items")
    return items


def build_invoice_spec(params: dict) -> dict:
    """Invoice: header blocks + items table + live totals.

    Amount = Qty x Unit Price per row; Subtotal = SUM over the exact
    item rows; Tax and TOTAL DUE reference the subtotal cell — all row
    numbers computed here.
    """
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    items = _coerce_line_items(
        params,
        ("items", "lines", "line_items"),
        ("description", "desc", "item", "label", "name"),
    )

    tax_rate = to_rate(_pick(params, "tax_rate", "vat", "tax", "vat_rate")) or 0.0
    tax_rate = min(max(tax_rate, 0.0), 1.0)
    discount = to_number(_pick(params, "discount", "discount_amount")) or 0.0
    discount = max(discount, 0.0)

    inv_number = _pick(params, "invoice_number", "number", "invoice_no", "id")
    inv_number = str(inv_number).strip() if inv_number is not None else ""
    inv_date = to_iso_date(_pick(params, "date", "invoice_date")) or ""
    due_date = to_iso_date(_pick(params, "due_date", "due")) or ""
    seller = _pick(params, "seller", "from", "business", "company", "seller_name")
    client = _pick(params, "client", "to", "customer", "bill_to", "client_name")
    notes = _pick(params, "notes", "note", "terms", "payment_terms")
    money = _currency_fmt(_pick(params, "currency")) or MONEY_FMT

    def _lines(value: Any, limit: int = 4) -> List[str]:
        if value is None:
            return []
        if isinstance(value, list):
            parts = [str(v).strip() for v in value if str(v).strip()]
        else:
            parts = [ln.strip() for ln in str(value).splitlines() if ln.strip()]
        return parts[:limit]

    blocks: List[dict] = [
        {
            "cell": "A1",
            "text": "INVOICE",
            "bold": True,
            "font_size": 16,
            "font_color": "16304F",
        },
    ]
    if inv_number:
        blocks += [
            {"cell": "A3", "text": "Invoice #"},
            {"cell": "B3", "text": inv_number},
        ]
    if inv_date:
        blocks += [
            {"cell": "A4", "text": "Date"},
            {"cell": "B4", "text": inv_date},
        ]
    if due_date:
        blocks += [
            {"cell": "A5", "text": "Due date"},
            {"cell": "B5", "text": due_date},
        ]

    row = 7
    seller_lines = _lines(seller)
    if seller_lines:
        blocks.append({"cell": f"A{row}", "text": "From:", "bold": True})
        row += 1
        for ln in seller_lines:
            blocks.append({"cell": f"A{row}", "text": ln})
            row += 1
        row += 1
    client_lines = _lines(client)
    if client_lines:
        blocks.append({"cell": f"A{row}", "text": "Bill To:", "bold": True})
        row += 1
        for ln in client_lines:
            blocks.append({"cell": f"A{row}", "text": ln})
            row += 1
        row += 1

    items_start = max(row, 12)
    header_row = items_start
    first_data = items_start + 1
    last_data = first_data + len(items) - 1

    item_rows: List[List[Any]] = []
    for i, (desc, qty, price) in enumerate(items):
        r = first_data + i
        item_rows.append([desc, qty, price, f"=B{r}*C{r}"])

    # totals block rows: Subtotal sits on the table's total row; tax /
    # discount / TOTAL DUE stack below it (computed, never overlapping)
    subtotal_row = last_data + 1
    r = subtotal_row + 1
    tax_row: Optional[int] = None
    disc_row: Optional[int] = None
    if tax_rate > 0:
        tax_row = r
        r += 1
    if discount > 0:
        disc_row = r
        r += 1
    total_row = r

    # totals block: labels in C, money in D (aligned under Amount)
    total_blocks: List[dict] = []
    if tax_row is not None:
        pct = round(tax_rate * 100, 4)
        pct_txt = f"{pct:g}"
        total_blocks += [
            {"cell": f"C{tax_row}", "text": f"Tax ({pct_txt}%)"},
            {"cell": f"D{tax_row}", "text": f"=D{subtotal_row}*{tax_rate}"},
        ]
    if disc_row is not None:
        total_blocks += [
            {"cell": f"C{disc_row}", "text": "Discount"},
            {"cell": f"D{disc_row}", "text": f"=-{discount}"},
        ]
    total_formula = f"=D{subtotal_row}"
    if tax_row is not None:
        total_formula += f"+D{tax_row}"
    if disc_row is not None:
        total_formula += f"+D{disc_row}"
    total_blocks += [
        {"cell": f"C{total_row}", "text": "TOTAL DUE", "bold": True, "font_size": 12},
        {"cell": f"D{total_row}", "text": total_formula, "bold": True, "font_size": 12},
    ]

    sheet_notes = None
    if isinstance(notes, str) and notes.strip():
        sheet_notes = notes.strip()[:1000]

    fname = "invoice"
    if inv_number:
        safe = re.sub(r"[^\w-]", "", inv_number)[:40]
        if safe:
            fname = f"invoice_{safe}"

    return {
        "filename": f"{fname}.xlsx",
        "sheets": [
            {
                "name": "Invoice",
                "tab_color": "16304F",
                "column_widths": {"A": 34, "B": 10, "C": 14, "D": 16},
                "text_blocks": blocks + total_blocks,
                "tables": [
                    {
                        "start_cell": f"A{items_start}",
                        "headers": ["Description", "Qty", "Unit Price", "Amount"],
                        "rows": item_rows,
                        "number_formats": {"B": QTY_FMT, "C": money, "D": money},
                        "total_row": [
                            "",
                            "",
                            "Subtotal",
                            "=SUM(D{first_row}:D{last_row})",
                        ],
                    }
                ],
                "notes": sheet_notes,
            }
        ],
    }


# ══════════════════════════════════════════════════════════════════════
# Budget planner template
# ══════════════════════════════════════════════════════════════════════


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
    """Budget planner: income + expenses tables + live summary.

    Totals are SUM() over the exact data rows; the summary references
    the two total-row cells; savings rate is a live TEXT() formula.
    """
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
                        f'TEXT(({income_total_cell}-{expense_total_cell})/{income_total_cell},"0.0%"),"n/a")'
                    ),
                ],
            ],
            "number_formats": {"B": money},
        }
    )

    return {
        "filename": f"{period.lower()}_budget.xlsx",
        "sheets": [
            {
                "name": "Budget",
                "tab_color": "16304F",
                "column_widths": {"A": 30, "B": 16},
                "text_blocks": blocks,
                "tables": tables,
                "notes": sheet_notes,
            }
        ],
    }


# ── Registry ──────────────────────────────────────────────────────────

PATTERN_BUILDERS = {
    "amortization": build_amortization_spec,
    "invoice": build_invoice_spec,
    "budget": build_budget_spec,
}
