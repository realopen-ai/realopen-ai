"""
Expense report pattern — a dated log of actual expenses for
reimbursement / business trip reporting.

Contract (stanza "expense_report" in prompts/pattern_classifier.md):
  employee, purpose, period_start ("YYYY-MM-DD"), period_end,
  currency, expenses [{date, category, description, amount, paid_by
  ("Personal"|"Company"), billable, receipt}], notes.
  paid_by "Personal" unless stated; billable true unless stated;
  receipt true only when the user mentions having one. One entry per
  expense; date YYYY-MM-DD.

Layout (Expenses sheet):
  row 1    title text block ("Expense Report — <purpose>")
  rows 3+  info blocks: Employee / Purpose / Period (present ones only)
  below    the expense log: Date | Category | Description | Amount |
           Paid By | Billable | Receipt, with a SUM total row
  below    By Category table — SUMIF per category + share of the grand
           total (division-guarded)
  below    Reimbursement Summary — SUMIF/SUMIFS split by payer and
           billable flag, ending in a live Net Due to Employee

Rules of the sheet:
  billable/receipt flags display as Yes/No strings (COUNTIF-able,
  dropdown-editable); the reimbursement math keys on those exact
  values, so toggling a dropdown updates every total.
  A row without a category lands in the generic "Uncategorized"
  bucket so the per-category totals stay complete.
No ROUND() anywhere — display rounding is the number format's job.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

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
PATTERN_NAME = "expense_report"

PATTERN_DESCRIPTION = (
    "Dated expense log for reimbursement: per-category totals, payer / "
    "billable splits and the net amount due to or from the employee."
)

# Routing keywords/stems — drive the cheap pre-gate and the classifier
# shortlist (see excel_gen._shortlist_patterns).
PATTERN_KEYWORDS = (
    "expense report",
    "expense log",
    "expense claim",
    "expense",
    "reimburse",
    "reimbursement",
    "out of pocket",
    "business trip",
    "business expense",
    "trip expenses",
)

MAX_EXPENSES = 300
MAX_ABS_MONEY = 1e12

DEFAULT_CATEGORY = "Uncategorized"

_PAID_BY_ALIASES = {
    "personal": "Personal",
    "me": "Personal",
    "myself": "Personal",
    "employee": "Personal",
    "self": "Personal",
    "own pocket": "Personal",
    "company": "Company",
    "employer": "Company",
    "business": "Company",
    "corporate": "Company",
    "firm": "Company",
}

# Status palette (Excel classic) — shared visual identity.
CF_GREEN_FILL = "C6EFCE"
CF_GREEN_TEXT = "1E4620"
CF_AMBER_FILL = "FFF2CC"
CF_AMBER_TEXT = "7F6000"
CF_RED_FILL = "FFC7CE"
CF_RED_TEXT = "9C0006"


def _clean_text(value: Any, limit: int = 200) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()[:limit]
    return ""


def _to_bool(value: Any, default: Optional[bool] = None) -> Optional[bool]:
    """Boolean-ish param -> bool (Yes/No, true/false, 1/0…)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("yes", "y", "true", "t", "1", "has receipt"):
            return True
        if s in ("no", "n", "false", "f", "0", "none", "missing"):
            return False
    return default


def _normalize_paid_by(value: Any) -> str:
    if isinstance(value, str):
        return _PAID_BY_ALIASES.get(value.strip().lower(), "Personal")
    return "Personal"  # stanza: Personal unless stated otherwise


def _normalize_expense(entry: Any) -> Optional[dict]:
    """One raw entry -> normalized expense dict (None = unusable)."""
    if isinstance(entry, dict):
        raw_date = _pick(entry, "date", "expense_date", "when")
        category = _clean_text(_pick(entry, "category", "type", "kind"), 80)
        description = _clean_text(
            _pick(entry, "description", "desc", "detail", "details", "item"), 200
        )
        amount = to_number(_pick(entry, "amount", "value", "cost", "price", "total"))
        paid_by = _pick(
            entry, "paid_by", "payer", "paid_with", "payment_method", "paid"
        )
        billable = _pick(entry, "billable", "billable_to_company", "chargeable")
        receipt = _pick(entry, "receipt", "receipt_attached", "has_receipt", "receipts")
    elif isinstance(entry, list) and len(entry) >= 3:
        # [date, description, amount] or [date, category, description, amount]
        raw_date = entry[0]
        if len(entry) >= 4:
            category = _clean_text(entry[1], 80)
            description = _clean_text(entry[2], 200)
            amount = to_number(entry[3])
        else:
            category = ""
            description = _clean_text(entry[1], 200)
            amount = to_number(entry[2])
        paid_by = None
        billable = None
        receipt = None
    else:
        return None

    if amount is None:
        return None  # nothing spent — unusable row
    amount = abs(amount)
    if amount > MAX_ABS_MONEY:
        amount = MAX_ABS_MONEY

    return {
        "date": to_iso_date(raw_date),
        "category": category or DEFAULT_CATEGORY,
        "description": description,
        "amount": amount,
        "paid_by": _normalize_paid_by(paid_by),
        "billable": bool(_to_bool(billable, default=True)),  # stanza default
        "receipt": bool(_to_bool(receipt, default=False)),  # stanza default
    }


def coerce_expense_report_params(params: dict) -> dict:
    """Validate + normalize classifier params; raises ValueError."""
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    employee = _clean_text(
        _pick(params, "employee", "employee_name", "submitted_by", "reporter"), 120
    )
    purpose = _clean_text(
        _pick(params, "purpose", "trip", "title", "reason", "description"), 200
    )
    period_start = to_iso_date(
        _pick(params, "period_start", "start_date", "from", "from_date")
    )
    period_end = to_iso_date(_pick(params, "period_end", "end_date", "to", "to_date"))

    raw = _pick(params, "expenses", "expense_log", "items", "lines", "expense_lines")
    expenses: List[dict] = []
    if isinstance(raw, list):
        for entry in raw[:MAX_EXPENSES]:
            exp = _normalize_expense(entry)
            if exp is not None:
                expenses.append(exp)

    if not expenses:
        raise ValueError("no usable expense lines (date + amount required)")

    notes = _pick(params, "notes", "note")
    notes = notes.strip()[:1000] if isinstance(notes, str) and notes.strip() else ""

    return {
        "employee": employee,
        "purpose": purpose,
        "period_start": period_start,
        "period_end": period_end,
        "expenses": expenses,
        "currency": _currency_fmt(_pick(params, "currency")),
        "notes": notes,
    }


def build_expense_report_spec(params: dict) -> dict:
    """Expense report workbook — every formula code-generated from the
    row numbers emitted below (refs can never drift)."""
    p = coerce_expense_report_params(params)
    expenses = p["expenses"]
    money = p["currency"] or MONEY_FMT

    title = "Expense Report"
    if p["purpose"]:
        title = f"Expense Report — {p['purpose']}"

    # ── Info blocks (present fields only) ─────────────────────────────
    blocks: List[dict] = [
        {"cell": "A1", "text": title, "bold": True, "font_size": 14},
    ]
    info_row = 3
    if p["employee"]:
        blocks.append({"cell": f"A{info_row}", "text": "Employee"})
        blocks.append({"cell": f"B{info_row}", "text": p["employee"]})
        info_row += 1
    if p["purpose"]:
        blocks.append({"cell": f"A{info_row}", "text": "Purpose"})
        blocks.append({"cell": f"B{info_row}", "text": p["purpose"]})
        info_row += 1
    if p["period_start"] or p["period_end"]:
        if p["period_start"] and p["period_end"]:
            period = f"{p['period_start']} to {p['period_end']}"
        elif p["period_start"]:
            period = f"from {p['period_start']}"
        else:
            period = f"to {p['period_end']}"
        blocks.append({"cell": f"A{info_row}", "text": "Period"})
        blocks.append({"cell": f"B{info_row}", "text": period})
        info_row += 1

    # ── Expense log ───────────────────────────────────────────────────
    log_anchor = info_row + 1
    log_first = log_anchor + 1  # no table title -> header ON the anchor
    log_last = log_first + len(expenses) - 1
    log_total = log_last + 1

    log_rows: List[List[Any]] = [
        [
            exp["date"],
            exp["category"],
            exp["description"] or None,
            exp["amount"],
            exp["paid_by"],
            "Yes" if exp["billable"] else "No",
            "Yes" if exp["receipt"] else "No",
        ]
        for exp in expenses
    ]

    log_table: Dict[str, Any] = {
        "start_cell": f"A{log_anchor}",
        "headers": [
            "Date",
            "Category",
            "Description",
            "Amount",
            "Paid By",
            "Billable",
            "Receipt",
        ],
        "rows": log_rows,
        "number_formats": {"A": "yyyy-mm-dd", "D": money},
        "alignments": {"E": "center", "F": "center", "G": "center"},
        "auto_filter": True,
        "total_row": [
            "Total",
            "",
            "",
            "=SUM(D{first_row}:D{last_row})",
            "",
            "",
            "",
        ],
    }

    # ── Per-category totals ───────────────────────────────────────────
    categories: List[str] = []
    for exp in expenses:
        if exp["category"] not in categories:
            categories.append(exp["category"])

    cat_anchor = log_total + 2
    cat_first = cat_anchor + 2  # table carries a title
    cat_last = cat_first + len(categories) - 1
    cat_total = cat_last + 1

    cat_rows: List[List[Any]] = []
    for category in categories:
        r = cat_first + len(cat_rows)
        cat_rows.append(
            [
                category,
                (
                    f"=SUMIF($B${log_first}:$B${log_last},$A{r},"
                    f"$D${log_first}:$D${log_last})"
                ),
                f'=IF($D${log_total}>0,B{r}/$D${log_total},"n/a")',
            ]
        )

    cat_table: Dict[str, Any] = {
        "start_cell": f"A{cat_anchor}",
        "title": "By Category",
        "headers": ["Category", "Total", "Share"],
        "rows": cat_rows,
        "number_formats": {"B": money, "C": PCT_FMT},
        "total_row": ["Total", "=SUM(B{first_row}:B{last_row})", ""],
    }

    # ── Reimbursement summary ─────────────────────────────────────────
    reimb_anchor = cat_total + 2
    reimb_first = reimb_anchor + 2  # table carries a title
    # Row offsets inside the reimbursement table (7 rows, indices 0..6):
    #   0 Total Expenses | 1 Paid Personally | 2 Paid by Company
    #   3 Reimbursable   | 4 Personal non-billable
    #   5 Company-paid non-billable (owe back) | 6 Net Due to Employee
    r_reimb = reimb_first + 3
    r_owe_back = reimb_first + 5
    r_net = reimb_first + 6

    reimb_rows: List[List[Any]] = [
        ["Total Expenses", f"=D{log_total}"],
        [
            "Paid Personally (total)",
            f'=SUMIF($E${log_first}:$E${log_last},"Personal",$D${log_first}:$D${log_last})',
        ],
        [
            "Paid by Company (total)",
            f'=SUMIF($E${log_first}:$E${log_last},"Company",$D${log_first}:$D${log_last})',
        ],
        [
            "Reimbursable (paid personally, billable)",
            (
                f"=SUMIFS($D${log_first}:$D${log_last},"
                f'$E${log_first}:$E${log_last},"Personal",'
                f'$F${log_first}:$F${log_last},"Yes")'
            ),
        ],
        [
            "Personal non-billable (not reimbursable)",
            (
                f"=SUMIFS($D${log_first}:$D${log_last},"
                f'$E${log_first}:$E${log_last},"Personal",'
                f'$F${log_first}:$F${log_last},"No")'
            ),
        ],
        [
            "Company-paid non-billable (owe back)",
            (
                f"=SUMIFS($D${log_first}:$D${log_last},"
                f'$E${log_first}:$E${log_last},"Company",'
                f'$F${log_first}:$F${log_last},"No")'
            ),
        ],
        ["Net Due to Employee", f"=B{r_reimb}-B{r_owe_back}"],
    ]

    reimb_table: Dict[str, Any] = {
        "start_cell": f"A{reimb_anchor}",
        "title": "Reimbursement Summary",
        "headers": ["Item", "Value"],
        "rows": reimb_rows,
        "number_formats": {"B": money},
    }

    notes = p["notes"] or (
        "Template-built expense report — everything is live. Toggle "
        "Paid By / Billable / Receipt with the dropdowns and the "
        "category totals + reimbursement summary recompute: reimbursable "
        "= paid personally AND billable; the company claws back "
        "non-billable company-paid rows; the Net Due cell turns green "
        "when the company owes you and red when you owe it."
    )

    charts: List[dict] = []
    if len(categories) >= 2:
        charts.append(
            {
                "type": "pie",
                "title": "Expenses by Category",
                "anchor": "I3",
                "width": 12,
                "height": 9,
                "categories_range": f"Expenses!A{cat_first}:A{cat_last}",
                "series": [
                    {
                        "name": "Total",
                        "values_range": f"Expenses!B{cat_first}:B{cat_last}",
                    }
                ],
            }
        )

    sheet_spec: Dict[str, Any] = {
        "name": "Expenses",
        "tab_color": "16304F",
        "freeze_panes": f"A{log_first}",
        "column_widths": {
            "A": 12,
            "B": 18,
            "C": 36,
            "D": 14,
            "E": 12,
            "F": 11,
            "G": 10,
        },
        "text_blocks": blocks,
        "tables": [log_table, cat_table, reimb_table],
        "data_validation": [
            {
                "range": f"E{log_first}:E{log_last}",
                "values": ["Personal", "Company"],
                "allow_blank": False,
                "prompt_title": "Paid by",
                "prompt": "Personal = you paid, Company = paid directly.",
                "error_title": "Invalid payer",
                "error": "Pick Personal or Company (the totals key on it).",
                "error_style": "stop",
            },
            {
                "range": f"F{log_first}:F{log_last}",
                "values": ["Yes", "No"],
                "allow_blank": False,
                "prompt_title": "Billable",
                "prompt": "Yes = reimbursable / client-billable expense.",
                "error_title": "Invalid value",
                "error": "Pick Yes or No (the reimbursement math keys on it).",
                "error_style": "stop",
            },
            {
                "range": f"G{log_first}:G{log_last}",
                "values": ["Yes", "No"],
                "allow_blank": False,
                "prompt_title": "Receipt",
                "prompt": "Do you have the receipt for this expense?",
                "error_title": "Invalid value",
                "error": "Pick Yes or No.",
                "error_style": "stop",
            },
        ],
        "conditional_formats": [
            # unbillable rows — amber flag
            {
                "range": f"F{log_first}:F{log_last}",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "No",
                        "fill": CF_AMBER_FILL,
                        "font_color": CF_AMBER_TEXT,
                    }
                ],
            },
            # missing receipts — red when the row is billable (the
            # risky case), amber for any other missing receipt
            {
                "range": f"G{log_first}:G{log_last}",
                "rules": [
                    {
                        "type": "formula",
                        "formula": f'AND($F{log_first}="Yes",$G{log_first}="No")',
                        "fill": CF_RED_FILL,
                        "font_color": CF_RED_TEXT,
                        "bold": True,
                    },
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "No",
                        "fill": CF_AMBER_FILL,
                        "font_color": CF_AMBER_TEXT,
                    },
                ],
            },
            # net settlement — green when the company owes the employee,
            # red when the employee owes the company
            {
                "range": f"B{r_net}",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "greater_than",
                        "value": 0,
                        "fill": CF_GREEN_FILL,
                        "font_color": CF_GREEN_TEXT,
                        "bold": True,
                    },
                    {
                        "type": "cell_is",
                        "operator": "less_than",
                        "value": 0,
                        "fill": CF_RED_FILL,
                        "font_color": CF_RED_TEXT,
                        "bold": True,
                    },
                ],
            },
        ],
        "notes": notes,
    }
    if charts:
        sheet_spec["charts"] = charts

    return {
        "filename": "expense_report.xlsx",
        "sheets": [sheet_spec],
    }


# ── Standard pattern entry points (used by the dynamic registry) ──────
coerce_params = coerce_expense_report_params
build_spec = build_expense_report_spec
