"""
Shared helpers for the deterministic workbook patterns.

Everything a pattern needs from outside its own module lives here:
param coercion (models quote numbers, add %, currency symbols…),
currency number formats, and tiny formatting/lookup utilities.

Pattern modules import from this package:

    from app.services.patterns.utils import to_number, _pick, money

Nothing pattern-specific lives here — keep this module generic so
every pattern (current and future) can rely on it.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, Optional, Tuple

# ── Shared number formats ──────────────────────────────────────────────

# Plain money (no currency symbol) — the fallback when no currency is
# specified. Display rounding only; patterns NEVER put ROUND() inside
# formulas (rounding in formulas accumulates decimal-level drift,
# e.g. an amortization balance ending at 0.14 instead of 0).
MONEY_FMT = "#,##0.00"
QTY_FMT = "#,##0.##"
MONTH_FMT = "mmm yyyy"
PCT_FMT = "0.00%"

# Currency → Excel number format with the symbol baked in.
CURRENCY_SYMBOLS: Dict[str, str] = {
    "USD": '"$"#,##0.00',
    "EUR": '#,##0.00" €"',
    "GBP": '"£"#,##0.00',
    "JPY": '"¥"#,##0',
    "CHF": '"CHF "#,##0.00',
    "CAD": '"CA$"#,##0.00',
    "AUD": '"A$"#,##0.00',
    "MAD": '#,##0.00" DH"',
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


def pick(params: dict, *keys: str) -> Any:
    """First non-None value among the given alias keys."""
    for k in keys:
        if k in params and params[k] is not None:
            return params[k]
    return None


# Backwards-compatible private alias — the existing pattern code and
# tests use `_pick`.
_pick = pick


def currency_fmt(currency: Any) -> Optional[str]:
    """Currency code ("USD", "MAD"…) → Excel number format string."""
    if not isinstance(currency, str) or not currency.strip():
        return None
    return CURRENCY_SYMBOLS.get(currency.strip().upper())


# Backwards-compatible private alias.
_currency_fmt = currency_fmt


def fmt_num(x: float) -> str:
    """Render a number compactly inside a formula (30.0 → "30")."""
    if x == int(x):
        return str(int(x))
    return repr(x)


# Backwards-compatible private alias.
_fmt_num = fmt_num


def next_month(year: int, month: int) -> Tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def first_of_next_month(today: Optional[date] = None) -> Tuple[int, int]:
    today = today or date.today()
    return next_month(today.year, today.month)


# Private aliases for the moved helpers.
_next_month = next_month
_first_of_next_month = first_of_next_month

__all__ = [
    "MONEY_FMT",
    "QTY_FMT",
    "MONTH_FMT",
    "PCT_FMT",
    "CURRENCY_SYMBOLS",
    "to_number",
    "to_rate",
    "to_int",
    "to_iso_date",
    "pick",
    "currency_fmt",
    "fmt_num",
    "next_month",
    "first_of_next_month",
    # private aliases (back-compat with the old excel_patterns module)
    "_pick",
    "_currency_fmt",
    "_fmt_num",
    "_next_month",
    "_first_of_next_month",
]
