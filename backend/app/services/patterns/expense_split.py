"""
Expense split pattern — splitting shared costs between a group.

Deterministic workbook (every formula reference computed from the rows
this module emits):

  Expenses        Description | Paid By | Amount | Shares note, with a
                 SUM total row. The Paid By column carries a dropdown
                 sourced live from the Summary sheet's member column.
  Summary         Member Balances: Paid (SUMIF by member over the
                 expenses), Share Owed (equal split = total/n, or
                 SUMPRODUCT of the amounts and the member's normalized
                 share fractions when the request states unequal
                 shares), Balance = Paid − Share Owed (positive = is
                 owed money, negative = owes), and a plain-language
                 Status. The Total row proves the balances SUM to zero.
                 Below it, settlement suggestions (greedy
                 largest-creditor ↔ largest-debtor matching) and a bar
                 chart of the balances with positive/negative bars.
  Calc (hidden)   only when the request states unequal shares: the raw
                 per-expense weight matrix (one column per member, rows
                 aligned 1:1 with the Expenses sheet) and the
                 renormalized share fractions each weight edit
                 recomputes live.

PARAMETER SEMANTICS (per the classifier stanza):

  members   the request's people list. A paid_by name the members list
            misses is added to it (a payer is necessarily a
            participant) — nothing else is ever invented.
  expenses  description and amount required; paid_by must be a member;
            unusable entries are skipped.
  shares    per-member weights ONLY when the request states unequal
            shares ({"Alice": 2, "Bob": 1}); null → an equal split
            (every member weight 1 → share 1/n).

Live-formula rule: everything the user edits (amounts, payers, weights
on the Calc sheet) recalculates paid/owed/balance/status instantly.
The ONE Python-computed exception is the settlement suggestion table —
a greedy matching cannot be expressed as a simple spreadsheet formula,
so those display rows are frozen at build time and the sheet says so.
No ROUND() anywhere — display rounding is the number format's job.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from app.services.patterns.utils import (
    MONEY_FMT,
    _currency_fmt,
    _pick,
    fmt_num,
    to_number,
)

# Registry key — must match the pattern stanza in
# prompts/pattern_classifier.md.
PATTERN_NAME = "expense_split"

PATTERN_DESCRIPTION = (
    "Group cost splitting: shared expenses with who paid what, live "
    "per-member balances (paid vs share owed), a zero-sum total row, "
    "settlement suggestions and a balances chart. Use when a group of "
    "friends, roommates or a trip wants an even split / who owes whom. "
    "Do NOT use for business expense reimbursement reports or budgets."
)

# Routing keywords/stems — drive the cheap pre-gate and the classifier
# shortlist (see excel_gen._shortlist_patterns). "share"/"bill" are
# deliberately avoided (stock shares / invoices own them).
PATTERN_KEYWORDS = (
    "split",
    "owe",
    "settle",
    "roommate",
    "shared cost",
    "share cost",
    "cost split",
    "even split",
    "group trip",
    "settle up",
    "divvy",
)

MAX_MEMBERS = 12
MAX_EXPENSES = 60

# Balances under half a cent count as settled (float-noise epsilon).
_EPS = 0.005

# Design tokens (same palette as the converter).
NAVY = "16304F"
STEEL = "1B3A5C"
MUTED = "5C6470"

# Excel's classic Good / Bad / Neutral conditional-format palettes.
CF_GREEN_FILL = "C6EFCE"
CF_GREEN_TEXT = "1E4620"
CF_AMBER_FILL = "FFF2CC"
CF_AMBER_TEXT = "7F6000"
CF_RED_FILL = "FFC7CE"
CF_RED_TEXT = "9C0006"


def _slug(text: str, fallback: str) -> str:
    s = re.sub(r"[^\w\s-]", "", str(text).strip().lower())
    s = re.sub(r"[\s_-]+", "_", s).strip("_")
    return s[:40] or fallback


def _clean_name(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _match_member(members: List[str], name: str) -> Optional[str]:
    target = str(name or "").strip().lower()
    if not target:
        return None
    for m in members:
        if m.lower() == target:
            return m
    return None


# ── Param coercion ────────────────────────────────────────────────────


def _normalize_expense(entry: Any, members: List[str]) -> Optional[dict]:
    """One expenses entry → {description, paid_by, amount, shares}.

    Returns None for unusable entries (no description, no usable
    amount, or a payer that cannot be represented).
    """
    if not isinstance(entry, dict):
        return None
    description = _clean_name(
        _pick(entry, "description", "item", "expense", "title"), 60
    )
    if not description:
        return None
    amount = to_number(_pick(entry, "amount", "cost", "value", "total"))
    if amount is None or amount <= 0:
        return None

    paid_by_raw = _pick(entry, "paid_by", "payer", "paid", "by")
    paid_by = _match_member(members, str(paid_by_raw or ""))
    if paid_by is None:
        # a payer is necessarily a participant — add them (capped)
        if len(members) >= MAX_MEMBERS:
            return None
        paid_by = _clean_name(paid_by_raw, 40)
        if not paid_by:
            return None
        members.append(paid_by)

    # shares = per-member weights ONLY when the request states unequal
    # shares; unusable share maps fall back to an equal split.
    raw_shares = _pick(entry, "shares", "weights", "split")
    shares: Dict[str, float] = {}
    if isinstance(raw_shares, dict):
        for key, value in raw_shares.items():
            member = _match_member(members, str(key))
            n = to_number(value)
            if member is None or n is None or n <= 0:
                continue
            shares[member] = n
    if not shares:
        shares = {}

    return {
        "description": description,
        "paid_by": paid_by,
        "amount": float(amount),
        "shares": shares or None,
    }


def coerce_expense_split_params(params: dict) -> dict:
    """Validate + normalize classifier params; raises ValueError."""
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    group_name = (
        _clean_name(_pick(params, "group_name", "group", "title", "name"), 60)
        or "Group"
    )

    currency = str(_pick(params, "currency") or "").strip().upper() or None

    members: List[str] = []
    raw_members = _pick(params, "members", "member_list", "people", "names")
    if raw_members is not None:
        if not isinstance(raw_members, list):
            raise ValueError("members must be an array")
        seen = set()
        for entry in raw_members[:MAX_MEMBERS]:
            name = _clean_name(entry, 40) if isinstance(entry, str) else None
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            members.append(name)

    raw_expenses = _pick(params, "expenses", "expense_list", "costs")
    expenses: List[dict] = []
    if raw_expenses is not None:
        if not isinstance(raw_expenses, list):
            raise ValueError("expenses must be an array")
        for entry in raw_expenses[:MAX_EXPENSES]:
            normalized = _normalize_expense(entry, members)
            if normalized is not None:
                expenses.append(normalized)

    # The request must give both sides — never invent people or costs.
    if not members:
        raise ValueError("no members")
    if not expenses:
        raise ValueError("no usable expenses")

    notes = _pick(params, "notes", "note")
    notes = notes.strip()[:1000] if isinstance(notes, str) and notes.strip() else None

    return {
        "group_name": group_name,
        "currency": currency,
        "members": members,
        "expenses": expenses,
        "notes": notes,
    }


# ── Python reference math (settlement rows + tests) ───────────────────


def compute_balances(members: List[str], expenses: List[dict]) -> Dict[str, float]:
    """paid − share owed per member (positive = is owed money).

    share owed = Σ over expenses of amount × (member weight / total
    weight of that expense) — the equal split is the every-weight-1
    special case.
    """
    paid = {m: 0.0 for m in members}
    owed = {m: 0.0 for m in members}
    for e in expenses:
        paid[e["paid_by"]] += e["amount"]
        shares = e.get("shares") or {m: 1.0 for m in members}
        total_w = sum(shares.values())
        if total_w <= 0:
            total_w = 1.0
            shares = {m: 1.0 for m in members}
        for m in members:
            owed[m] += e["amount"] * shares.get(m, 0.0) / total_w
    return {m: paid[m] - owed[m] for m in members}


def compute_settlement(balances: Dict[str, float]) -> List[Tuple[str, str, float]]:
    """Greedy largest-creditor ↔ largest-debtor matching.

    Returns [(from_member, to_member, amount)] — `from` pays `to`.
    Deterministic: ordered by amount desc, then name. Balances under
    half a cent count as settled.
    """
    creditors = sorted(
        ((m, b) for m, b in balances.items() if b > _EPS),
        key=lambda x: (-x[1], x[0]),
    )
    debtors = sorted(
        ((m, -b) for m, b in balances.items() if b < -_EPS),
        key=lambda x: (-x[1], x[0]),
    )
    transfers: List[Tuple[str, str, float]] = []
    while creditors and debtors:
        c_name, c_amt = creditors[0]
        d_name, d_amt = debtors[0]
        amount = min(c_amt, d_amt)
        if amount > _EPS:
            transfers.append((d_name, c_name, round(amount, 10)))
        c_amt -= amount
        d_amt -= amount
        if c_amt <= _EPS:
            creditors.pop(0)
        else:
            creditors[0] = (c_name, c_amt)
        if d_amt <= _EPS:
            debtors.pop(0)
        else:
            debtors[0] = (d_name, d_amt)
    return transfers


# ── Builder ───────────────────────────────────────────────────────────


def _col(idx: int) -> str:
    """1-based column index → Excel column letters (1 → "A")."""
    s = ""
    n = int(idx)
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def build_expense_split_spec(params: dict) -> dict:
    """Expense split workbook — every formula code-generated.

    Layout (rows computed here, never guessed by a model):

    Expenses sheet:
      row 4      headers: Description | Paid By | Amount | Shares
      rows 5-    one row per expense; total row = SUM of the amounts
    Summary sheet:
      rows 5-    Member Balances: Paid = SUMIF by member, Share Owed =
                 total/n (equal) or SUMPRODUCT with the Calc share
                 fractions (unequal), Balance = Paid − Share Owed,
                 Status = IF chain; total row SUMs Paid/Share/Balance
                 (the zero-sum proof)
      below      settlement suggestions (greedy matching, frozen at
                 build time — noted on the sheet) + balances bar chart
    Calc sheet (hidden, unequal shares only):
      rows 5-    raw weight matrix (one column per member + row total),
                 aligned 1:1 with the Expenses rows, then the
                 renormalized share fractions as live formulas
    """
    p = coerce_expense_split_params(params)
    group_name: str = p["group_name"]
    currency: Optional[str] = p["currency"]
    members: List[str] = p["members"]
    expenses: List[dict] = p["expenses"]
    notes = p["notes"]

    n_m = len(members)
    n_e = len(expenses)
    has_weights = any(e["shares"] for e in expenses)
    balances = compute_balances(members, expenses)
    transfers = compute_settlement(balances)
    money_fmt = _currency_fmt(currency) or MONEY_FMT

    # ── geometry ────────────────────────────────────────────────────
    EX_R0 = 5  # first Expenses data row
    EX_RN = 4 + n_e  # last Expenses data row
    EX_TOT = EX_RN + 1  # expenses total row
    SM_TITLE = 3  # Member Balances table title row
    SM_HEADER = 4  # noqa
    SM_R0 = 5  # first member row
    SM_RN = 4 + n_m  # last member row
    SM_TOT = SM_RN + 1  # zero-sum total row
    SET_NOTE = SM_TOT + 2  # settlement note (text block)
    SET_TITLE = SET_NOTE + 2
    SET_HEADER = SET_TITLE + 1
    SET_R0 = SET_HEADER + 1  # noqa

    # ── Expenses sheet ──────────────────────────────────────────────
    def _shares_note(e: dict) -> str:
        if not e["shares"]:
            return "equal"
        parts = [f"{m} {fmt_num(w)}" for m, w in e["shares"].items()]
        text = " · ".join(parts)
        return text[:60]

    ex_rows: List[List[Any]] = [
        [e["description"], e["paid_by"], e["amount"], _shares_note(e)] for e in expenses
    ]

    expenses_sheet: Dict[str, Any] = {
        "name": "Expenses",
        "tab_color": NAVY,
        "freeze_panes": "A5",
        "column_widths": {"A": 34, "B": 16, "C": 14, "D": 26},
        "text_blocks": [
            {
                "cell": "A1",
                "text": f"{group_name} — Expense Split",
                "bold": True,
                "font_size": 14,
                "font_color": NAVY,
            },
            {
                "cell": "A2",
                "text": (
                    "Add or edit shared costs below — pick the payer from "
                    "the dropdown. The Summary sheet recalculates who owes "
                    "whom instantly."
                ),
                "italic": True,
                "font_color": MUTED,
            },
        ],
        "tables": [
            {
                "start_cell": "A4",
                "headers": ["Description", "Paid By", "Amount", "Shares"],
                "rows": ex_rows,
                "total_row": [
                    "Total",
                    "",
                    f"=SUM(C{EX_R0}:C{EX_RN})",
                    "",
                ],
                "number_formats": {"C": money_fmt},
                "alignments": {"B": "center", "C": "right", "D": "center"},
            }
        ],
        "data_validation": [
            {
                "range": f"B{EX_R0}:B{EX_RN}",
                "source_range": f"Summary!$A${SM_R0}:$A${SM_RN}",
                "allow_blank": True,
                "prompt_title": "Paid by",
                "prompt": "Pick the group member who paid.",
                "error_title": "Unknown member",
                "error": "Pick one of the members listed on the Summary sheet.",
            }
        ],
        "notes": notes
        or (
            "One row per shared cost. Paid By is a dropdown fed by the "
            "Summary sheet's member list. The Shares note records unequal "
            "weights from the request ('equal' otherwise). Amounts flow "
            "into the live balances on the Summary sheet."
        ),
    }

    # ── Summary sheet ───────────────────────────────────────────────
    if has_weights:
        # shares table column per member on the Calc sheet
        w_member_cols = [_col(2 + j) for j in range(n_m)]  # B..
        w_total_col = _col(2 + n_m)  # after members
        s_member_cols = [_col(4 + n_m + j) for j in range(n_m)]  # after gap

    sm_rows: List[List[Any]] = []
    for j, m in enumerate(members):
        r = SM_R0 + j
        paid = (
            f"=SUMIF(Expenses!$B${EX_R0}:$B${EX_RN},$A{r},"
            f"Expenses!$C${EX_R0}:$C${EX_RN})"
        )
        if has_weights:
            sc = s_member_cols[j]
            share = (
                f"=SUMPRODUCT(Expenses!$C${EX_R0}:$C${EX_RN},"
                f"Calc!{sc}${EX_R0}:{sc}${EX_RN})"
            )
        else:
            share = f"=Expenses!$C${EX_TOT}/{n_m}"
        sm_rows.append(
            [
                m,
                paid,
                share,
                f"=B{r}-C{r}",
                f'=IF(D{r}>{fmt_num(_EPS)},"Is owed",'
                f'IF(D{r}<-{fmt_num(_EPS)},"Owes","Settled"))',
            ]
        )

    sm_total_row: List[Any] = [
        "Total",
        f"=SUM(B{SM_R0}:B{SM_RN})",
        f"=SUM(C{SM_R0}:C{SM_RN})",
        f"=SUM(D{SM_R0}:D{SM_RN})",
        "",
    ]

    summary_blocks: List[dict] = [
        {
            "cell": "A1",
            "text": "Balances & Settlement",
            "bold": True,
            "font_size": 12,
            "font_color": STEEL,
        },
    ]
    summary_tables: List[dict] = [
        {
            "start_cell": f"A{SM_TITLE}",
            "title": "Member Balances",
            "headers": ["Member", "Paid", "Share Owed", "Balance", "Status"],
            "rows": sm_rows,
            "total_row": sm_total_row,
            "number_formats": {
                "B": money_fmt,
                "C": money_fmt,
                "D": money_fmt,
            },
            "alignments": {"E": "center"},
        }
    ]

    if transfers:
        summary_tables.append(
            {
                "start_cell": f"A{SET_TITLE}",
                "title": "Settlement Suggestions",
                "headers": ["From (owes)", "To (receives)", "Amount"],
                "rows": [[frm, to, amt] for frm, to, amt in transfers],
                "number_formats": {"C": money_fmt},
                "alignments": {"C": "right"},
            }
        )
        settle_note = (
            "Settlement suggestions: the fewest greedy transfers to zero "
            "everyone out (largest debtor pays the largest creditor). "
            "Computed at build time — the balances above are live "
            "formulas, so re-check these rows after editing expenses."
        )
    else:
        settle_note = (
            "All settled up — nobody owes anybody anything (balances are "
            "within half a cent of zero)."
        )
    summary_blocks.append(
        {
            "cell": f"A{SET_NOTE}",
            "text": settle_note,
            "italic": True,
            "font_color": MUTED,
        }
    )

    summary_sheet: Dict[str, Any] = {
        "name": "Summary",
        "tab_color": STEEL,
        "column_widths": {"A": 24, "B": 14, "C": 14, "D": 14, "E": 12},
        "text_blocks": summary_blocks,
        "tables": summary_tables,
        "conditional_formats": [
            {
                "range": f"E{SM_R0}:E{SM_RN}",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "Is owed",
                        "fill": CF_GREEN_FILL,
                        "font_color": CF_GREEN_TEXT,
                    },
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "Owes",
                        "fill": CF_RED_FILL,
                        "font_color": CF_RED_TEXT,
                    },
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "Settled",
                        "fill": CF_AMBER_FILL,
                        "font_color": CF_AMBER_TEXT,
                    },
                ],
            },
        ],
        "charts": [
            {
                "type": "bar",
                "title": "Balances by Member (positive = is owed)",
                "anchor": "G3",
                "width": 16,
                "height": 9,
                "categories_range": f"Summary!A{SM_R0}:A{SM_RN}",
                "series": [
                    {
                        "name": "Balance",
                        "values_range": f"Summary!D{SM_R0}:D{SM_RN}",
                    }
                ],
                "value_numfmt": money_fmt,
            }
        ],
        "notes": (
            "Paid = SUMIF over the expenses by payer. Share Owed = "
            + (
                "SUMPRODUCT of the amounts and the member's share "
                "fractions (hidden Calc sheet — unequal shares stated in "
                "the request; edit a weight there and everything "
                "renormalizes)."
                if has_weights
                else "total / members (equal split — the request stated "
                "no unequal shares)."
            )
            + " Balance = Paid − Share Owed: positive means the member is "
            "owed money, negative means they owe. The Total row proves "
            "the balances sum to zero. " + settle_note
        ),
    }

    sheets: List[dict] = [expenses_sheet, summary_sheet]

    # ── Calc sheet (hidden) — only when unequal shares exist ─────────
    if has_weights:
        weight_rows: List[List[Any]] = []
        share_rows: List[List[Any]] = []
        w_last_member = w_member_cols[-1]
        for i, e in enumerate(expenses):
            r = EX_R0 + i  # rows aligned 1:1 with the Expenses sheet
            raw = e["shares"] or {m: 1.0 for m in members}
            weight_row: List[Any] = [raw.get(m, 0.0) for m in members]
            weight_row.append(f"=SUM({w_member_cols[0]}{r}:{w_last_member}{r})")
            weight_rows.append(weight_row)
            share_row: List[Any] = [
                f"=IF(SUM(${w_member_cols[0]}{r}:${w_last_member}{r})=0,0,"
                f"{w_member_cols[j]}{r}/"
                f"SUM(${w_member_cols[0]}{r}:${w_last_member}{r}))"
                for j in range(n_m)
            ]
            share_rows.append(share_row)

        calc_sheet: Dict[str, Any] = {
            "name": "Calc",
            "hidden": True,
            "column_widths": {
                **{c: 12 for c in w_member_cols},
                w_total_col: 10,
                _col(3 + n_m): 4,
                **{c: 12 for c in s_member_cols},
            },
            "text_blocks": [
                {
                    "cell": "A1",
                    "text": (
                        "Hidden helpers — rows run 1:1 with the Expenses " "sheet."
                    ),
                    "italic": True,
                    "font_color": MUTED,
                },
                {
                    "cell": "A2",
                    "text": (
                        f"{w_member_cols[0]}..{w_last_member} = raw "
                        f"per-member weights per expense (1 = equal); "
                        f"{w_total_col} = row total. "
                        f"{s_member_cols[0]}..{s_member_cols[-1]} = "
                        "renormalized share fractions (weight / row "
                        "total) the Summary's Share Owed multiplies the "
                        "amounts by."
                    ),
                    "italic": True,
                    "font_color": MUTED,
                },
            ],
            "tables": [
                {
                    "start_cell": "B4",
                    "headers": list(members) + ["Total"],
                    "rows": weight_rows,
                    "number_formats": {
                        c: "#,##0.##" for c in w_member_cols + [w_total_col]
                    },
                },
                {
                    "start_cell": f"{s_member_cols[0]}4",
                    "headers": list(members),
                    "rows": share_rows,
                    "number_formats": {c: "#,##0.###" for c in s_member_cols},
                },
            ],
            "notes": (
                "Hidden calculation sheet for the unequal split. The raw "
                "weight matrix holds the per-member weights the request "
                "stated (equal expenses carry 1 for everyone); edit a "
                "weight and the share fractions, every member's Share "
                "Owed and the balances recompute. Right-click a tab → "
                "Unhide to inspect."
            ),
        }
        sheets.append(calc_sheet)

    return {
        "filename": f"{_slug(group_name, 'group')}_expense_split.xlsx",
        "sheets": sheets,
    }


# ── Standard pattern entry points (used by the dynamic registry) ──────

coerce_params = coerce_expense_split_params
build_spec = build_expense_split_spec
