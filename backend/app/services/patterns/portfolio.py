"""
Portfolio pattern — investment portfolio tracker (deterministic).

Creates investment portfolio spreadsheets containing holdings,
investment cost, current value, gain/loss, returns, allocation, and
optional transaction history. Used when the user wants to track
stocks, ETFs, funds, crypto, or other investments — NOT for general
financial budgets or amortization schedules.

Two input modes (the classifier fills one of them):
  Simple mode        "I own 100 IAM shares at an average cost of 95.50,
                     now at 102"  →  holdings given directly.
  Transaction mode  "I bought 100 IAM at 95.50 and another 50 at 98"
                     →  transactions given; positions and average
                     cost are computed here with the average-cost
                     method. When both are present, holdings win
                     (they are the explicit current positions) and
                     the transactions still render as the trade log.

Sheets produced (all formulas code-generated from the actual layout
rows — the model never decides formulas, chart ranges or cell
coordinates):
  Overview       key metrics (live cross-sheet formulas) + the
                 allocation pie chart
  Holdings       Symbol | Asset | Quantity | Avg. Cost | Invested |
                 Current Price | Current Value | Gain/Loss |
                 Return % | Allocation — every value a live formula
  Performance    gain/loss stats + the gain/loss bar chart
  Transactions   raw trade log with signed cash flow (transaction
                 history present only)

No ROUND() in the formulas — display rounding is the number format's
job; rounding inside formulas accumulates decimal-level drift.
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
    to_iso_date,
    to_number,
)

# Registry key — must match the pattern stanza in
# prompts/pattern_classifier.md.
PATTERN_NAME = "portfolio"

PATTERN_DESCRIPTION = (
    "Creates investment portfolio spreadsheets containing holdings, "
    "investment cost, current value, gain/loss, returns, allocation, "
    "and optional transaction history. Use this generator when the "
    "user wants to track stocks, ETFs, funds, crypto, or other "
    "investments. Do not use it for general financial budgets or "
    "amortization schedules."
)

MAX_PORTFOLIO_HOLDINGS = 200
MAX_PORTFOLIO_TRANSACTIONS = 500

_DATE_FMT = "yyyy-mm-dd"


# ── Param coercion ────────────────────────────────────────────────────


def _normalize_holding(entry: Any) -> Optional[dict]:
    """One holdings entry → {symbol, name, quantity, average_cost,
    current_price}, or None when unusable.

    Accepts the documented object form and the compact list form
    [symbol, quantity, avg_cost, (current_price), (name)].
    """
    if isinstance(entry, dict):
        symbol = _pick(entry, "symbol", "ticker")
        name = _pick(entry, "name", "asset", "asset_name", "company", "security")
        quantity = to_number(_pick(entry, "quantity", "qty", "shares", "units"))
        avg_cost = to_number(
            _pick(entry, "average_cost", "avg_cost", "cost_basis", "cost", "buy_price")
        )
        current_price = to_number(
            _pick(
                entry,
                "current_price",
                "price",
                "market_price",
                "last_price",
                "last",
            )
        )
    elif isinstance(entry, list) and len(entry) >= 3:
        # [symbol, quantity, avg_cost, (current_price), (name)]
        symbol = entry[0]
        quantity = to_number(entry[1])
        avg_cost = to_number(entry[2])
        current_price = to_number(entry[3]) if len(entry) >= 4 else None
        name = entry[4] if len(entry) >= 5 else None
    else:
        return None

    if symbol is None or not str(symbol).strip():
        return None
    symbol = str(symbol).strip()
    if quantity is None or quantity <= 0:
        return None
    if avg_cost is None or avg_cost < 0:
        return None  # invested cost is required — can't compute returns
    if current_price is None or current_price < 0:
        # No market price given → flat position (gain 0) rather than
        # dropping the holding; the notes explain the default.
        current_price = avg_cost

    name = str(name).strip() if name is not None and str(name).strip() else symbol
    return {
        "symbol": symbol,
        "name": name,
        "quantity": quantity,
        "average_cost": avg_cost,
        "current_price": current_price,
    }


def _normalize_transaction(entry: Any) -> Optional[dict]:
    """One transaction entry → {date, symbol, type, name, quantity,
    price}, or None when unusable.

    Accepts the documented object form and the compact list form
    [date, symbol, type, quantity, price, (name)].
    """
    if isinstance(entry, dict):
        date_s = to_iso_date(
            _pick(entry, "date", "trade_date", "when", "transaction_date")
        )
        symbol = _pick(entry, "symbol", "ticker", "asset")
        ttype = _pick(entry, "type", "side", "action", "transaction_type", "direction")
        name = _pick(entry, "name", "asset_name", "company")
        quantity = to_number(_pick(entry, "quantity", "qty", "shares", "units"))
        price = to_number(
            _pick(entry, "price", "unit_price", "per_share", "rate", "amount")
        )
    elif isinstance(entry, list) and len(entry) >= 5:
        # [date, symbol, type, quantity, price, (name)]
        date_s = to_iso_date(entry[0]) if isinstance(entry[0], str) else None
        symbol = entry[1]
        ttype = entry[2]
        quantity = to_number(entry[3])
        price = to_number(entry[4])
        name = entry[5] if len(entry) >= 6 else None
    else:
        return None

    if symbol is None or not str(symbol).strip():
        return None
    symbol = str(symbol).strip()
    if quantity is None or quantity <= 0:
        return None
    if price is None or price < 0:
        return None

    ttype_s = str(ttype).strip().lower() if ttype is not None else ""
    if ttype_s in ("sell", "sold", "sale", "s", "out", "divest"):
        ttype = "sell"
    else:
        # missing / unknown → buy (the overwhelmingly common case)
        ttype = "buy"

    name = str(name).strip() if name is not None and str(name).strip() else None
    return {
        "date": date_s,
        "symbol": symbol,
        "type": ttype,
        "name": name,
        "quantity": quantity,
        "price": price,
    }


def coerce_portfolio_params(params: dict) -> dict:
    """Validate + normalize classifier params; raises ValueError."""
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    holdings_raw = _pick(params, "holdings", "positions", "assets")
    holdings: List[dict] = []
    if holdings_raw is not None:
        if not isinstance(holdings_raw, list):
            raise ValueError("holdings must be an array")
        for entry in holdings_raw[:MAX_PORTFOLIO_HOLDINGS]:
            normalized = _normalize_holding(entry)
            if normalized is not None:
                holdings.append(normalized)

    transactions_raw = _pick(params, "transactions", "trades", "history")
    transactions: List[dict] = []
    if transactions_raw is not None:
        if not isinstance(transactions_raw, list):
            raise ValueError("transactions must be an array")
        for entry in transactions_raw[:MAX_PORTFOLIO_TRANSACTIONS]:
            normalized = _normalize_transaction(entry)
            if normalized is not None:
                transactions.append(normalized)

    if not holdings and not transactions:
        raise ValueError("no holdings or transactions provided")

    name = _pick(params, "portfolio_name", "name")
    name = (
        str(name).strip()
        if name is not None and str(name).strip()
        else "Investment Portfolio"
    )

    notes = _pick(params, "notes", "note")
    notes = notes.strip()[:1000] if isinstance(notes, str) and notes.strip() else None

    return {
        "portfolio_name": name,
        "currency": _currency_fmt(_pick(params, "currency")),
        "holdings": holdings,
        "transactions": transactions,
        "notes": notes,
    }


# ── Transaction mode: positions from the trade history ───────────────


def _holdings_from_transactions(transactions: List[dict]) -> List[dict]:
    """Compute open positions + average cost from buy/sell trades.

    Average-cost method: every buy adds (quantity, cost) to the
    position; every sell removes quantity at the CURRENT average cost
    (cost basis shrinks proportionally). Fully closed positions (or
    attempts to sell more than held, clamped) are dropped. The
    current price defaults to each symbol's most recent trade price.

    Trades are applied chronologically — dated trades sorted by date
    first, undated trades after them in their original order.
    """
    dated = [t for t in transactions if t["date"]]
    undated = [t for t in transactions if not t["date"]]
    ordered = sorted(dated, key=lambda t: str(t["date"])) + undated

    positions: Dict[str, Dict[str, Any]] = {}
    for txn in ordered:
        pos = positions.setdefault(
            txn["symbol"],
            {
                "symbol": txn["symbol"],
                "name": None,
                "quantity": 0.0,
                "cost": 0.0,
                "last_price": None,
            },
        )
        if txn["name"]:
            pos["name"] = txn["name"]
        if txn["type"] == "buy":
            pos["quantity"] += txn["quantity"]
            pos["cost"] += txn["quantity"] * txn["price"]
        else:  # sell — average-cost method
            if pos["quantity"] > 0:
                sell_qty = min(txn["quantity"], pos["quantity"])
                avg = pos["cost"] / pos["quantity"]
                pos["cost"] = max(0.0, pos["cost"] - avg * sell_qty)
                pos["quantity"] -= sell_qty
        pos["last_price"] = txn["price"]

    holdings: List[dict] = []
    for pos in positions.values():
        if pos["quantity"] <= 1e-9:
            continue  # fully closed position — nothing left to track
        avg_cost = pos["cost"] / pos["quantity"]
        current_price = pos["last_price"] if pos["last_price"] is not None else avg_cost
        holdings.append(
            {
                "symbol": pos["symbol"],
                "name": pos["name"] or pos["symbol"],
                "quantity": pos["quantity"],
                "average_cost": avg_cost,
                "current_price": current_price,
            }
        )
    if not holdings:
        raise ValueError("transactions closed all positions — nothing to track")
    return holdings


# ── Builder ───────────────────────────────────────────────────────────


def build_portfolio_spec(params: dict) -> dict:
    """Investment portfolio workbook — every formula code-generated.

    Layout (rows computed here, never guessed by a model):

    Holdings sheet:
      row 1     title
      row 3     table headers (start_cell A3, no table title)
      rows 4..  data: Symbol | Asset | Quantity | Avg. Cost | Invested |
                Current Price | Current Value | Gain/Loss | Return % |
                Allocation (Invested / Value / Gain / Return / Allocation
                are live formulas)
      row N+1   totals (SUM over the exact data rows; the allocation
                total reads 100%)

    Overview sheet: portfolio name + live metric labels/values
    (text blocks, each with its own number format — money rows,
    percent rows, symbol rows) + the allocation pie chart.

    Performance sheet: live stats (text blocks) + the gain/loss bar
    chart. Transactions sheet (when a trade history was provided):
    Date | Symbol | Type | Quantity | Price | Cash Flow — signed from
    the investor's wallet (buys negative, sells positive) with a Net
    Cash Flow total.
    """
    p = coerce_portfolio_params(params)
    portfolio_name = p["portfolio_name"]
    holdings: List[dict] = list(p["holdings"])
    transactions: List[dict] = list(p["transactions"])

    # Transaction mode: derive the current positions from the trades.
    # Explicit holdings win when both are present; the transactions
    # then still render as the trade log.
    computed_from_transactions = False
    if not holdings and transactions:
        holdings = _holdings_from_transactions(transactions)
        computed_from_transactions = True

    money = p["currency"] or MONEY_FMT

    # Holdings table geometry
    first_data = 4
    last_data = first_data + len(holdings) - 1
    total_row = last_data + 1

    holdings_rows: List[List[Any]] = []
    for i, h in enumerate(holdings):
        r = first_data + i
        holdings_rows.append(
            [
                h["symbol"],
                h["name"],
                h["quantity"],
                h["average_cost"],
                f"=C{r}*D{r}",  # Invested
                h["current_price"],
                f"=C{r}*F{r}",  # Current Value
                f"=G{r}-E{r}",  # Gain/Loss
                f"=IF(E{r}=0,0,H{r}/E{r})",  # Return %
                f"=IF($G${total_row}=0,0,G{r}/$G${total_row})",  # Allocation
            ]
        )

    holdings_notes = (
        "Invested = Quantity x Avg. Cost, Current Value = Quantity x Current "
        "Price, Gain/Loss = Current Value - Invested, Return = Gain/Loss / "
        "Invested, Allocation = holding's share of the total current value. "
        "Edit any quantity, cost or price and every column recalculates."
    )
    if computed_from_transactions:
        holdings_notes += (
            " Positions and average cost were computed from the transaction "
            "history using the average-cost method; the current price "
            "defaults to each symbol's most recent trade price."
        )

    holdings_sheet: Dict[str, Any] = {
        "name": "Holdings",
        "tab_color": "16304F",
        "freeze_panes": "A4",
        "column_widths": {
            "A": 10,
            "B": 26,
            "C": 11,
            "D": 14,
            "E": 16,
            "F": 14,
            "G": 16,
            "H": 15,
            "I": 10,
            "J": 11,
        },
        "text_blocks": [
            {
                "cell": "A1",
                "text": "Portfolio Holdings",
                "bold": True,
                "font_size": 14,
            }
        ],
        "tables": [
            {
                "start_cell": "A3",
                "headers": [
                    "Symbol",
                    "Asset",
                    "Quantity",
                    "Avg. Cost",
                    "Invested",
                    "Current Price",
                    "Current Value",
                    "Gain/Loss",
                    "Return %",
                    "Allocation",
                ],
                "rows": holdings_rows,
                "number_formats": {
                    "C": QTY_FMT,
                    "D": money,
                    "E": money,
                    "F": money,
                    "G": money,
                    "H": money,
                    "I": PCT_FMT,
                    "J": PCT_FMT,
                },
                "total_row": [
                    "Total",
                    "",
                    f"=SUM(C{first_data}:C{last_data})",
                    "",
                    f"=SUM(E{first_data}:E{last_data})",
                    "",
                    f"=SUM(G{first_data}:G{last_data})",
                    f"=SUM(H{first_data}:H{last_data})",
                    "",
                    f"=SUM(J{first_data}:J{last_data})",
                ],
            }
        ],
        "notes": holdings_notes,
    }

    # Overview — live metrics + allocation pie. Text blocks (not a
    # table) so each value keeps its own number format: money rows,
    # the percent row and the symbol rows differ.
    ret_col = "I"
    overview_blocks: List[dict] = [
        {
            "cell": "A1",
            "text": portfolio_name,
            "bold": True,
            "font_size": 16,
            "font_color": "16304F",
        },
        {"cell": "A3", "text": "Number of Holdings"},
        {
            "cell": "B3",
            "text": f"=COUNTA(Holdings!A{first_data}:A{last_data})",
        },
        {"cell": "A4", "text": "Total Invested"},
        {
            "cell": "B4",
            "text": f"=Holdings!E{total_row}",
            "number_format": money,
            "bold": True,
        },
        {"cell": "A5", "text": "Current Value"},
        {
            "cell": "B5",
            "text": f"=Holdings!G{total_row}",
            "number_format": money,
            "bold": True,
        },
        {"cell": "A6", "text": "Total Gain/Loss"},
        {
            "cell": "B6",
            "text": f"=Holdings!H{total_row}",
            "number_format": money,
            "bold": True,
        },
        {"cell": "A7", "text": "Return"},
        {
            "cell": "B7",
            "text": (
                f"=IF(Holdings!E{total_row}=0,0,"
                f"Holdings!H{total_row}/Holdings!E{total_row})"
            ),
            "number_format": PCT_FMT,
            "bold": True,
        },
        {"cell": "A8", "text": "Best Performer"},
        {
            "cell": "B8",
            "text": (
                f"=INDEX(Holdings!A{first_data}:A{last_data},"
                f"MATCH(MAX(Holdings!{ret_col}{first_data}:"
                f"{ret_col}{last_data}),"
                f"Holdings!{ret_col}{first_data}:{ret_col}{last_data},0))"
            ),
        },
        {"cell": "A9", "text": "Worst Performer"},
        {
            "cell": "B9",
            "text": (
                f"=INDEX(Holdings!A{first_data}:A{last_data},"
                f"MATCH(MIN(Holdings!{ret_col}{first_data}:"
                f"{ret_col}{last_data}),"
                f"Holdings!{ret_col}{first_data}:{ret_col}{last_data},0))"
            ),
        },
    ]

    overview_notes = p["notes"] or (
        "All metrics reference the Holdings sheet — change a quantity, cost "
        "or price there and this overview updates."
    )

    overview_sheet: Dict[str, Any] = {
        "name": "Overview",
        "tab_color": "C9A227",
        "column_widths": {"A": 20, "B": 20},
        "text_blocks": overview_blocks,
        "charts": [
            {
                "type": "pie",
                "title": "Portfolio Allocation",
                "anchor": "D3",
                "width": 12,
                "height": 9,
                "categories_range": f"Holdings!A{first_data}:A{last_data}",
                "series": [
                    {
                        "name": "Allocation",
                        "values_range": (f"Holdings!G{first_data}:G{last_data}"),
                    }
                ],
            }
        ],
        "notes": overview_notes,
    }

    # Performance — live stats + gain/loss bar chart
    perf_blocks: List[dict] = [
        {"cell": "A1", "text": "Performance", "bold": True, "font_size": 14},
        {"cell": "A3", "text": "Holdings Gaining"},
        {
            "cell": "B3",
            "text": (
                f"=COUNTIF(Holdings!{ret_col}{first_data}:"
                f'{ret_col}{last_data},">0")'
            ),
            "number_format": "0",
        },
        {"cell": "A4", "text": "Holdings Losing"},
        {
            "cell": "B4",
            "text": (
                f"=COUNTIF(Holdings!{ret_col}{first_data}:"
                f'{ret_col}{last_data},"<0")'
            ),
            "number_format": "0",
        },
        {"cell": "A5", "text": "Best Return"},
        {
            "cell": "B5",
            "text": f"=MAX(Holdings!{ret_col}{first_data}:{ret_col}{last_data})",
            "number_format": PCT_FMT,
        },
        {"cell": "A6", "text": "Worst Return"},
        {
            "cell": "B6",
            "text": f"=MIN(Holdings!{ret_col}{first_data}:{ret_col}{last_data})",
            "number_format": PCT_FMT,
        },
        {"cell": "A7", "text": "Average Return"},
        {
            "cell": "B7",
            "text": (f"=AVERAGE(Holdings!{ret_col}{first_data}:{ret_col}{last_data})"),
            "number_format": PCT_FMT,
        },
    ]

    performance_sheet: Dict[str, Any] = {
        "name": "Performance",
        "tab_color": "1B3A5C",
        "column_widths": {"A": 20, "B": 14},
        "text_blocks": perf_blocks,
        "charts": [
            {
                "type": "bar",
                "title": "Gain/Loss by Holding",
                "anchor": "D3",
                "width": 16,
                "height": 10,
                "categories_range": f"Holdings!A{first_data}:A{last_data}",
                # Value labels on the bars ("+650 / -120") — the chart
                # answers "what's making or losing me money?" at a glance.
                "show_values": True,
                "series": [
                    {
                        "name": "Gain/Loss",
                        "values_range": (f"Holdings!H{first_data}:H{last_data}"),
                    }
                ],
            }
        ],
        "notes": (
            "Counts and returns are live formulas over the Holdings sheet's "
            "Return % column; the bar chart shows each holding's absolute "
            "gain/loss."
        ),
    }

    sheets: List[Dict[str, Any]] = [
        overview_sheet,
        holdings_sheet,
        performance_sheet,
    ]

    # Transactions — the raw trade log (whenever a history was given)
    if transactions:
        txn_first = 4
        txn_last = txn_first + len(transactions) - 1
        txn_rows: List[List[Any]] = []
        for i, t in enumerate(transactions):
            r = txn_first + i
            # Signed from the investor's wallet: buys are cash out
            # (negative), sells are cash in (positive).
            amount_formula = f"=-D{r}*E{r}" if t["type"] == "buy" else f"=D{r}*E{r}"
            txn_rows.append(
                [
                    t["date"] or "",
                    t["symbol"],
                    t["type"].capitalize(),
                    t["quantity"],
                    t["price"],
                    amount_formula,
                ]
            )

        transactions_sheet: Dict[str, Any] = {
            "name": "Transactions",
            "tab_color": "8A94A3",
            "freeze_panes": "A4",
            "column_widths": {
                "A": 13,
                "B": 10,
                "C": 9,
                "D": 11,
                "E": 13,
                "F": 15,
            },
            "text_blocks": [
                {
                    "cell": "A1",
                    "text": "Transactions",
                    "bold": True,
                    "font_size": 14,
                }
            ],
            "tables": [
                {
                    "start_cell": "A3",
                    "headers": [
                        "Date",
                        "Symbol",
                        "Type",
                        "Quantity",
                        "Price",
                        "Cash Flow",
                    ],
                    "rows": txn_rows,
                    "number_formats": {
                        "A": _DATE_FMT,
                        "D": QTY_FMT,
                        "E": money,
                        "F": money,
                    },
                    "total_row": [
                        "",
                        "",
                        "",
                        "",
                        "Net Cash Flow",
                        f"=SUM(F{txn_first}:F{txn_last})",
                    ],
                }
            ],
            "notes": (
                "Cash Flow is signed from the investor's wallet: buys are "
                "cash out (negative), sells are cash in (positive). Net Cash "
                "Flow = sells - buys."
            ),
        }
        sheets.append(transactions_sheet)

    fname = re.sub(r"[^\w\s-]", "", portfolio_name)[:40].strip()
    fname = re.sub(r"[\s_-]+", "_", fname).strip("_").lower() or "portfolio"

    return {
        "filename": f"{fname}.xlsx",
        "sheets": sheets,
    }


# ── Standard pattern entry points (used by the dynamic registry) ──────

coerce_params = coerce_portfolio_params
build_spec = build_portfolio_spec
