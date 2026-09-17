"""
Payroll pattern — payroll register for ONE pay period.

Deterministic: the classifier extracts the employee list (name, role,
pay basis, rate, hours, overtime, tax, deductions); this module lays
out the register and computes every formula reference from the actual
row numbers of the layout it emits.

Layout (Payroll sheet):
  row 1        title ("Payroll — {pay_period}" or "Payroll Register")
  rows 2..K    params / assumptions block (label in A, value in B):
                 Pay Period        when stated
                 Pay Date          when stated
                 Overtime Multiplier   always (default 1.5) — the
               hourly gross formula reads $B$K, so editing the cell
               reprices every overtime line.
  row K+2      table headers (start_cell, no table title)
  rows K+3..N  data: Employee | Role | Basis | Rate | Hours | OT Hours |
               Gross Pay | Tax Rate | Tax | Deductions | Net Pay
                 hourly:  Gross = Rate*Hours + OT*Rate*$B$K
                 monthly: Gross = Rate (salary)
                 Tax   = Gross * Tax Rate  (blank rate → 0 tax)
                 Net   = Gross - Tax - Deductions
  row N+1      totals: SUMs over the exact data rows for Hours / OT /
               Gross / Tax / Deductions / Net.

Single sheet on purpose: the register's totals row IS the summary for
one pay period; a gross-pay bar chart floats to the right of the table.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.services.patterns.utils import (
    MONEY_FMT,
    PCT_FMT,
    QTY_FMT,
    _currency_fmt,
    _pick,
    col_letter,
    to_iso_date,
    to_number,
    to_rate,
)

# Registry key — must match the pattern stanza in
# prompts/pattern_classifier.md.
PATTERN_NAME = "payroll"

PATTERN_DESCRIPTION = (
    "Payroll register for one pay period: per-employee gross (hourly "
    "with overtime or monthly salary), tax, deductions and net pay."
)

# Routing keywords/stems — drive the cheap pre-gate and the classifier
# shortlist (see excel_gen._shortlist_patterns).
PATTERN_KEYWORDS = (
    "payroll",
    "payslip",
    "paycheck",
    "salary",
    "wage",
    "pay period",
    "net pay",
)

MAX_EMPLOYEES = 200

DEFAULT_OT_MULTIPLIER = 1.5


def _clean_str(value: Any, cap: int) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s[:cap] if s else None


def _normalize_basis(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip().lower()
    if "hour" in s:
        return "hourly"
    if "month" in s or "salar" in s:
        return "monthly"
    return None


def coerce_payroll_params(params: dict) -> dict:
    """Validate + normalize classifier output. Raises ValueError when
    required params are missing (the employee list, a name, a rate, or
    hours for an hourly employee).

    Accepts alias keys, quoted/currency numbers and percent strings.
    Semantics per the stanza: ``overtime_hours`` 0 when not stated;
    ``tax_rate`` None (blank) when not stated — tax is then 0 until the
    user fills a rate; ``deductions`` 0 when not stated; the overtime
    multiplier defaults to 1.5 and lives in an assumptions cell.
    """
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    raw = None
    for key in ("employees", "staff", "employee_list", "staff_list"):
        if isinstance(params.get(key), list):
            raw = params[key]
            break
    if raw is None:
        raise ValueError("employees missing")

    employees: List[Dict[str, Any]] = []
    for entry in raw[:MAX_EMPLOYEES]:
        if not isinstance(entry, dict):
            continue
        name = _clean_str(
            _pick(entry, "name", "employee", "employee_name", "worker"), 60
        )
        if name is None:
            raise ValueError("employee without a name")
        rate = to_number(
            _pick(
                entry,
                "rate",
                "pay_rate",
                "hourly_rate",
                "monthly_salary",
                "salary",
                "wage",
            )
        )
        if rate is None:
            raise ValueError("employee without a rate")
        rate = max(rate, 0.0)

        hours = to_number(
            _pick(entry, "hours", "hours_worked", "regular_hours", "work_hours")
        )
        basis = _normalize_basis(_pick(entry, "pay_basis", "basis", "pay_type", "type"))
        if basis is None:
            basis = "hourly" if hours is not None else "monthly"
        if basis == "hourly" and hours is None:
            raise ValueError("hourly employee without hours")
        if hours is not None:
            hours = max(hours, 0.0)

        overtime = to_number(_pick(entry, "overtime_hours", "overtime", "ot_hours"))
        overtime = max(overtime, 0.0) if overtime is not None else 0.0

        tax_rate = to_rate(_pick(entry, "tax_rate", "tax_pct", "taxrate", "tax"))
        if tax_rate is not None:
            tax_rate = min(max(tax_rate, 0.0), 1.0)

        deductions = to_number(
            _pick(
                entry,
                "deductions",
                "deduction",
                "other_deductions",
                "deductions_amount",
            )
        )
        deductions = max(deductions, 0.0) if deductions is not None else 0.0

        employees.append(
            {
                "name": name,
                "role": _clean_str(
                    _pick(entry, "role", "title", "position", "job_title", "job"),
                    40,
                ),
                "pay_basis": basis,
                "rate": rate,
                "hours": hours if basis == "hourly" else None,
                "overtime_hours": overtime if basis == "hourly" else None,
                "tax_rate": tax_rate,
                "deductions": deductions,
            }
        )
    if not employees:
        raise ValueError("no usable employees")

    multiplier = to_number(
        _pick(params, "overtime_multiplier", "overtime_mult", "multiplier")
    )
    if multiplier is None:
        multiplier = DEFAULT_OT_MULTIPLIER
    multiplier = min(max(multiplier, 0.5), 3.0)

    notes = _pick(params, "notes", "note")
    notes = notes.strip()[:1000] if isinstance(notes, str) and notes.strip() else None

    return {
        "pay_period": _clean_str(
            _pick(params, "pay_period", "period", "period_label"), 60
        ),
        "pay_date": to_iso_date(_pick(params, "pay_date", "payment_date")),
        "currency": _currency_fmt(_pick(params, "currency")),
        "overtime_multiplier": multiplier,
        "employees": employees,
        "notes": notes,
    }


def build_payroll_spec(params: dict) -> dict:
    """Payroll register — every formula code-generated from the rows
    this builder emits.

    No ROUND() in the formulas: display rounding belongs to the money
    number formats. Tax is gross x rate so a blank rate cell (tax not
    stated) naturally computes 0.
    """
    p = coerce_payroll_params(params)
    employees: List[dict] = p["employees"]
    money = p["currency"] or MONEY_FMT

    n = len(employees)

    # ── Params / assumptions block (rows 2..K) ─────────────────────────
    blocks: List[dict] = [
        {
            "cell": "A1",
            "text": (
                f"Payroll — {p['pay_period']}"
                if p["pay_period"]
                else "Payroll Register"
            ),
            "bold": True,
            "font_size": 14,
        }
    ]
    row = 2
    if p["pay_period"]:
        blocks += [
            {"cell": f"A{row}", "text": "Pay Period"},
            {"cell": f"B{row}", "text": p["pay_period"], "bold": True},
        ]
        row += 1
    if p["pay_date"]:
        blocks += [
            {"cell": f"A{row}", "text": "Pay Date"},
            {
                "cell": f"B{row}",
                "text": p["pay_date"],
                "bold": True,
                "number_format": "yyyy-mm-dd",
            },
        ]
        row += 1
    mult_row = row
    blocks += [
        {"cell": f"A{row}", "text": "Overtime Multiplier"},
        {
            "cell": f"B{row}",
            "text": p["overtime_multiplier"],
            "bold": True,
            "number_format": "0.0",
        },
    ]
    row += 1

    # ── Layout math (table) ────────────────────────────────────────────
    # headers on row mult_row + 2, data below, total row after the data.
    header_row = mult_row + 2
    first_data = header_row + 1
    last_data = header_row + n
    total_row = last_data + 1  # noqa

    headers = [
        "Employee",
        "Role",
        "Basis",
        "Rate",
        "Hours",
        "OT Hours",
        "Gross Pay",
        "Tax Rate",
        "Tax",
        "Deductions",
        "Net Pay",
    ]
    ncols = len(headers)

    rows: List[List[Any]] = []
    for i, emp in enumerate(employees):
        r = first_data + i
        if emp["pay_basis"] == "hourly":
            gross = f"=D{r}*E{r}+F{r}*D{r}*$B${mult_row}"
            hours: Any = emp["hours"]
            overtime: Any = emp["overtime_hours"]
            basis = "Hourly"
        else:
            gross = f"=D{r}"
            hours = None
            overtime = None
            basis = "Monthly"
        rows.append(
            [
                emp["name"],
                emp["role"],
                basis,
                emp["rate"],
                hours,
                overtime,
                gross,
                emp["tax_rate"],
                f"=G{r}*H{r}",
                emp["deductions"] or 0.0,
                f"=G{r}-I{r}-J{r}",
            ]
        )

    net_cf = {
        "range": f"K{first_data}:K{last_data}",
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

    how_to = (
        f"Gross Pay = Rate x Hours + OT Hours x Rate x the overtime "
        f"multiplier in B{mult_row} for hourly staff (edit that cell to "
        "change overtime pay); monthly staff carry their salary as Gross. "
        "Tax = Gross x Tax Rate — a blank rate (tax not stated) computes 0 "
        "until you fill it. Net Pay = Gross - Tax - Deductions. Basis "
        "cells have an Hourly/Monthly dropdown."
    )
    sheet_notes = f"{p['notes'][:1000]} " + how_to if p["notes"] else how_to

    payroll_sheet: Dict[str, Any] = {
        "name": "Payroll",
        "tab_color": "16304F",
        "freeze_panes": f"A{first_data}",
        "column_widths": {
            "A": 24,
            "B": 22,
            "C": 11,
            "D": 13,
            "E": 10,
            "F": 10,
            "G": 14,
            "H": 11,
            "I": 13,
            "J": 13,
            "K": 14,
        },
        "text_blocks": blocks,
        "tables": [
            {
                "start_cell": f"A{header_row}",
                "headers": headers,
                "rows": rows,
                "number_formats": {
                    "D": money,
                    "E": QTY_FMT,
                    "F": QTY_FMT,
                    "G": money,
                    "H": PCT_FMT,
                    "I": money,
                    "J": money,
                    "K": money,
                },
                "alignments": {"C": "center", "H": "center"},
                "total_row": [
                    "Totals",
                    None,
                    None,
                    None,
                    f"=SUM(E{first_data}:E{last_data})",
                    f"=SUM(F{first_data}:F{last_data})",
                    f"=SUM(G{first_data}:G{last_data})",
                    None,
                    f"=SUM(I{first_data}:I{last_data})",
                    f"=SUM(J{first_data}:J{last_data})",
                    f"=SUM(K{first_data}:K{last_data})",
                ],
            }
        ],
        "conditional_formats": [net_cf],
        "data_validation": [
            {
                "range": f"C{first_data}:C{last_data}",
                "values": ["Hourly", "Monthly"],
                "error_style": "warning",
            }
        ],
        "charts": [
            {
                "type": "bar",
                "title": "Gross Pay by Employee",
                "anchor": f"{col_letter(ncols + 2)}{header_row}",
                "width": 16,
                "height": 10,
                "categories_range": f"Payroll!A{first_data}:A{last_data}",
                "series": [
                    {
                        "name": "Gross Pay",
                        "values_range": f"Payroll!G{first_data}:G{last_data}",
                    }
                ],
                "value_numfmt": money,
            }
        ],
        "notes": sheet_notes,
    }

    fname = "payroll"
    if p["pay_period"]:
        slug = re.sub(r"[^\w\s-]", "", p["pay_period"])[:40].strip().lower()
        slug = re.sub(r"[\s_-]+", "_", slug).strip("_")
        if slug and slug != "payroll":
            fname = f"payroll_{slug}"

    return {
        "filename": f"{fname}.xlsx",
        "sheets": [payroll_sheet],
    }


# ── Standard pattern entry points (used by the dynamic registry) ──────

coerce_params = coerce_payroll_params
build_spec = build_payroll_spec
