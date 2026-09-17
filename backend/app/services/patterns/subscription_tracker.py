"""
Subscription tracker pattern — recurring subscriptions / memberships
with cost per billing cycle, next renewal, live monthly & yearly
equivalents and active/cancelled status.

Contract (stanza "subscription_tracker" in
prompts/pattern_classifier.md):
  currency, subscriptions [{name, category, amount, cycle
  ("monthly"|"yearly"|"quarterly"|"weekly"), next_renewal
  "YYYY-MM-DD", active}], notes.
  name + amount required per entry (amount as billed per cycle); cycle
  defaults to "monthly"; next_renewal only when stated; active true
  unless the user says cancelled.

Layout (Subscriptions sheet):
  row 1   title text block
  row 3   main table header; one row per subscription
          Name | Category | Amount | Cycle | Next Renewal |
          Days to Renewal | Monthly Equivalent | Yearly Equivalent | Status
  total   SUMIF over Status="Active" for the two equivalent columns
  below   Spend Summary table (counts, totals, guards, earliest
          renewal via TODAY()-based MIN)

Live math:
  monthly equivalent = amount x cycle factor (nested IF on the cycle
  cell — edit the cycle dropdown and the equivalents recompute)
  yearly  equivalent = amount x the yearly factor
  days to renewal    = E{r}-TODAY() with a blank guard
  totals             = SUMIF keyed on the Status dropdown

No ROUND() anywhere — display rounding is the number format's job.
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
PATTERN_NAME = "subscription_tracker"

PATTERN_DESCRIPTION = (
    "Recurring subscriptions / memberships tracker with billing cycles, "
    "next renewals, live monthly & yearly equivalents and totals."
)

# Routing keywords/stems — drive the cheap pre-gate and the classifier
# shortlist (see excel_gen._shortlist_patterns).
PATTERN_KEYWORDS = (
    "subscription",
    "recurring",
    "membership",
    "renew",
    "billing cycle",
    "monthly fee",
    "annual fee",
    "recurring cost",
    "streaming service",
)

MAX_SUBSCRIPTIONS = 200
MAX_ABS_MONEY = 1e12

CYCLES = ("monthly", "quarterly", "yearly", "weekly")

_CYCLE_ALIASES = {
    "monthly": "monthly",
    "month": "monthly",
    "mo": "monthly",
    "per month": "monthly",
    "monthly billing": "monthly",
    "yearly": "yearly",
    "year": "yearly",
    "annual": "yearly",
    "annually": "yearly",
    "yr": "yearly",
    "per year": "yearly",
    "12 months": "yearly",
    "quarterly": "quarterly",
    "quarter": "quarterly",
    "q": "quarterly",
    "3 months": "quarterly",
    "weekly": "weekly",
    "week": "weekly",
    "wk": "weekly",
    "per week": "weekly",
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
        if s in ("yes", "y", "true", "t", "1", "active"):
            return True
        if s in ("no", "n", "false", "f", "0", "cancelled", "canceled"):
            return False
    return default


def _normalize_cycle(value: Any) -> str:
    if isinstance(value, str):
        return _CYCLE_ALIASES.get(value.strip().lower(), "monthly")
    return "monthly"


def _normalize_subscription(entry: Any) -> Optional[dict]:
    """One raw entry -> normalized subscription dict (None = unusable)."""
    if isinstance(entry, dict):
        name = _clean_text(
            _pick(entry, "name", "service", "provider", "app", "title"), 120
        )
        amount = to_number(_pick(entry, "amount", "price", "cost", "fee", "billed"))
        category = _clean_text(_pick(entry, "category", "type", "group"), 80)
        cycle_raw = _pick(
            entry, "cycle", "billing_cycle", "frequency", "period", "interval"
        )
        renewal_raw = _pick(
            entry,
            "next_renewal",
            "renewal_date",
            "next_billing",
            "next_charge",
            "renews",
            "renewal",
        )
        active_raw = _pick(entry, "active", "is_active", "status", "state")
        cancelled_raw = _pick(entry, "cancelled", "canceled")
    elif isinstance(entry, list) and len(entry) >= 2:
        # [name, amount] or [name, amount, cycle] (+ next_renewal)
        name = _clean_text(entry[0], 120)
        amount = to_number(entry[1])
        category = ""
        cycle_raw = entry[2] if len(entry) >= 3 else None
        renewal_raw = entry[3] if len(entry) >= 4 else None
        active_raw = None
        cancelled_raw = None
    else:
        return None

    if not name or amount is None:
        return None  # stanza: name and amount required
    amount = abs(amount)
    if amount > MAX_ABS_MONEY:
        amount = MAX_ABS_MONEY

    active = True
    if cancelled_raw is not None:
        active = not _to_bool(cancelled_raw, default=False)
    elif active_raw is not None:
        active = _to_bool(active_raw, default=True)

    return {
        "name": name,
        "category": category,
        "amount": amount,
        "cycle": _normalize_cycle(cycle_raw),
        "next_renewal": to_iso_date(renewal_raw),
        "active": bool(active),
    }


def coerce_subscription_tracker_params(params: dict) -> dict:
    """Validate + normalize classifier params; raises ValueError."""
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    raw = _pick(params, "subscriptions", "subs", "subscription_list", "recurring_costs")
    subs: List[dict] = []
    if isinstance(raw, list):
        for entry in raw[:MAX_SUBSCRIPTIONS]:
            sub = _normalize_subscription(entry)
            if sub is not None:
                subs.append(sub)

    if not subs:
        raise ValueError("no usable subscriptions (name + amount required)")

    notes = _pick(params, "notes", "note")
    notes = notes.strip()[:1000] if isinstance(notes, str) and notes.strip() else ""

    return {
        "subscriptions": subs,
        "currency": _currency_fmt(_pick(params, "currency")),
        "notes": notes,
    }


def build_subscription_tracker_spec(params: dict) -> dict:
    """Subscription tracker workbook — every formula code-generated
    from the row numbers emitted below (refs can never drift)."""
    p = coerce_subscription_tracker_params(params)
    subs = p["subscriptions"]
    money = p["currency"] or MONEY_FMT

    # Layout math: main table anchored at A3 with NO table title →
    # header row 3, data 4..(3+n), total row right after the data.
    header_row = 3
    first = header_row + 1
    last = first + len(subs) - 1
    total_row = last + 1

    rows: List[List[Any]] = []
    for sub in subs:
        r = first + len(rows)
        rows.append(
            [
                sub["name"],
                sub["category"] or None,
                sub["amount"],
                sub["cycle"],
                sub["next_renewal"],
                f'=IF(E{r}="","n/a",E{r}-TODAY())',
                (
                    f'=C{r}*IF(D{r}="yearly",1/12,'
                    f'IF(D{r}="quarterly",1/3,'
                    f'IF(D{r}="weekly",52/12,1)))'
                ),
                (
                    f'=C{r}*IF(D{r}="yearly",1,'
                    f'IF(D{r}="quarterly",4,'
                    f'IF(D{r}="weekly",52,12)))'
                ),
                "Active" if sub["active"] else "Cancelled",
            ]
        )

    main_table: Dict[str, Any] = {
        "start_cell": f"A{header_row}",
        "headers": [
            "Name",
            "Category",
            "Amount",
            "Cycle",
            "Next Renewal",
            "Days to Renewal",
            "Monthly Equivalent",
            "Yearly Equivalent",
            "Status",
        ],
        "rows": rows,
        "number_formats": {
            "C": money,
            "E": "yyyy-mm-dd",
            "F": "0",
            "G": money,
            "H": money,
        },
        "alignments": {"D": "center", "E": "center", "F": "center", "I": "center"},
        "auto_filter": True,
        "total_row": [
            "Total (Active)",
            "",
            "",
            "",
            "",
            "",
            f'=SUMIF($I{first}:$I{last},"Active",$G{first}:$G{last})',
            f'=SUMIF($I{first}:$I{last},"Active",$H{first}:$H{last})',
            "",
        ],
    }

    # Spend summary below the main table. Mixed types are handled by
    # keeping money rows as live numeric refs and counts/dates as
    # TEXT()/concatenation formulas (number formats ignore text).
    sum_anchor = total_row + 2
    sum_first = sum_anchor + 2  # noqa
    sum_rows: List[List[Any]] = [
        [
            "Active / Cancelled Counts",
            (
                f'=COUNTIF($I{first}:$I{last},"Active")&" active / "'
                f'&COUNTIF($I{first}:$I{last},"Cancelled")&" cancelled"'
            ),
        ],
        ["Total Monthly Spend (Active)", f"=G{total_row}"],
        ["Total Yearly Spend (Active)", f"=H{total_row}"],
        [
            "Average Monthly per Active Sub",
            (
                f'=IF(COUNTIF($I{first}:$I{last},"Active")>0,'
                f'G{total_row}/COUNTIF($I{first}:$I{last},"Active"),"n/a")'
            ),
        ],
        [
            "Next Renewal (Earliest)",
            (
                f"=IF(COUNT($E{first}:$E{last})>0,"
                f'TEXT(MIN($E{first}:$E{last}),"yyyy-mm-dd"),'
                f'"no renewal dates")'
            ),
        ],
        [
            "Priciest Yearly Cost",
            f'=IF(COUNT($H{first}:$H{last})>0,MAX($H{first}:$H{last}),"n/a")',
        ],
    ]
    summary_table: Dict[str, Any] = {
        "start_cell": f"A{sum_anchor}",
        "title": "Spend Summary",
        "headers": ["Metric", "Value"],
        "rows": sum_rows,
        "number_formats": {"B": money},
    }

    notes = p["notes"] or (
        "Template-built subscription tracker — everything is live. Edit "
        "any amount or pick another cycle/status from the dropdowns: "
        "monthly/yearly equivalents recompute per row, totals count "
        "Active subscriptions only, days-to-renewal follow TODAY()."
    )

    sheet_spec: Dict[str, Any] = {
        "name": "Subscriptions",
        "tab_color": "16304F",
        "freeze_panes": f"A{first}",
        "column_widths": {
            "A": 26,
            "B": 18,
            "C": 12,
            "D": 12,
            "E": 14,
            "F": 13,
            "G": 16,
            "H": 16,
            "I": 12,
        },
        "text_blocks": [
            {
                "cell": "A1",
                "text": "Subscription Tracker",
                "bold": True,
                "font_size": 14,
            }
        ],
        "tables": [main_table, summary_table],
        "charts": [
            {
                "type": "bar",
                "title": "Yearly Cost by Subscription",
                "anchor": "K3",
                "width": 16,
                "height": 9,
                "categories_range": f"Subscriptions!A{first}:A{last}",
                "series": [
                    {
                        "name": "Yearly Equivalent",
                        "values_range": f"Subscriptions!H{first}:H{last}",
                    }
                ],
                "value_numfmt": money,
            }
        ],
        "data_validation": [
            {
                "range": f"D{first}:D{last}",
                "values": list(CYCLES),
                "allow_blank": False,
                "prompt_title": "Billing cycle",
                "prompt": "monthly / quarterly / yearly / weekly.",
                "error_title": "Invalid cycle",
                "error": (
                    "Pick one of: monthly, quarterly, yearly, weekly "
                    "(the equivalent columns key on the exact wording)."
                ),
                "error_style": "stop",
            },
            {
                "range": f"I{first}:I{last}",
                "values": ["Active", "Cancelled"],
                "allow_blank": False,
                "prompt_title": "Status",
                "prompt": "Active counts toward the totals; Cancelled does not.",
                "error_title": "Invalid status",
                "error": "Pick Active or Cancelled (the SUMIF totals key on it).",
                "error_style": "stop",
            },
        ],
        "conditional_formats": [
            # overdue renewal (date in the past) — red; renewing within
            # a week — amber. Text "n/a" never matches numeric rules.
            {
                "range": f"F{first}:F{last}",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "less_than",
                        "value": 0,
                        "fill": CF_RED_FILL,
                        "font_color": CF_RED_TEXT,
                        "bold": True,
                    },
                    {
                        "type": "cell_is",
                        "operator": "between",
                        "value": [0, 7],
                        "fill": CF_AMBER_FILL,
                        "font_color": CF_AMBER_TEXT,
                    },
                ],
            },
            # status color coding
            {
                "range": f"I{first}:I{last}",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "Active",
                        "fill": CF_GREEN_FILL,
                        "font_color": CF_GREEN_TEXT,
                    },
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "Cancelled",
                        "fill": CF_AMBER_FILL,
                        "font_color": CF_AMBER_TEXT,
                    },
                ],
            },
        ],
        "notes": notes,
    }

    return {
        "filename": "subscription_tracker.xlsx",
        "sheets": [sheet_spec],
    }


# ── Standard pattern entry points (used by the dynamic registry) ──────
coerce_params = coerce_subscription_tracker_params
build_spec = build_subscription_tracker_spec
