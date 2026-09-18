"""
Invoice pattern — a professional bill for goods / services.

Deterministic: the classifier extracts parties, meta and line items;
this module lays out the invoice and computes every formula reference
from the actual row numbers of the layout it emits.

Layout (Invoice sheet):
  row 1        INVOICE title
  rows 3-5     meta: Invoice # / Date / Due Date (label + value)
  From         seller name (bold) + optional address, phone/fax, email
  Bill To      client name (bold) + optional ID, address, phone, email
  items table  Description | Qty | Unit Price | Amount (Amount =
               Qty x Unit Price; Subtotal = SUM over the exact rows)
  totals       Subtotal → Discount → Tax → TOTAL DUE. The discount is
               either a fixed amount or a percentage of the subtotal,
               applied BEFORE the tax; the tax base is the subtotal
               AFTER the discount. TOTAL DUE sums the subtotal, the
               (negative) discount and the tax.
  notes        optional "Notes / Terms & Conditions" section
"""

from __future__ import annotations

import re
from typing import Any, List, Optional, Tuple

from app.services.patterns.utils import (
    MONEY_FMT,
    QTY_FMT,
    _currency_fmt,
    _fmt_num,
    _pick,
    to_iso_date,
    to_number,
    to_rate,
)

# Registry key — must match the pattern stanza in
# prompts/pattern_classifier.md.
PATTERN_NAME = "invoice"

PATTERN_DESCRIPTION = (
    "A bill for goods/services to a client: parties, itemized lines, "
    "subtotal / discount / tax / total."
)

# Routing keywords/stems — drive the cheap pre-gate and the classifier
# shortlist (see excel_gen._shortlist_patterns).
PATTERN_KEYWORDS = (
    "invoice",
    "bill",
    "billing",
    "quote",
    "quotation",
    "estimate",
    "receipt",
)

MAX_INVOICE_ITEMS = 200


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


def _address_line(addr: Any) -> Optional[str]:
    """Coerce an address param (string | list | {city,state,zip…}) to one line."""
    if addr is None:
        return None
    if isinstance(addr, dict):
        parts: List[str] = []
        for key in (
            "line1",
            "street",
            "address1",
            "city",
            "state",
            "zip",
            "zip_code",
            "postal_code",
        ):
            v = addr.get(key)
            if v is not None and str(v).strip():
                parts.append(str(v).strip())
        parts = list(dict.fromkeys(parts))  # dedupe, keep order
        return ", ".join(parts) if parts else None
    if isinstance(addr, (list, tuple)):
        parts = [str(p).strip() for p in addr if str(p).strip()]
        return ", ".join(parts) if parts else None
    s = str(addr).strip()
    return s or None


def _coerce_party_info(
    value: Any, name_keys: Tuple[str, ...]
) -> Tuple[Optional[str], List[str]]:
    """Party param → (name, detail lines) for the From / Bill To blocks.

    Accepts a structured object (``{"name": …, "address": …,
    "phone": …, "fax": …, "email": …}`` — the client may also carry
    an ``"id"``), or a plain string / list of strings where the first
    line is the name and the rest are detail lines. Returns
    ``(None, [])`` when no usable name is present — the block is then
    skipped entirely (a party block without a name is useless).
    """
    if value is None:
        return None, []
    if isinstance(value, dict):
        name = _pick(value, *name_keys)
        name = str(name).strip() if name is not None else ""
        if not name:
            return None, []
        lines: List[str] = []
        ident = _pick(
            value, "id", "client_id", "customer_id", "vat", "vat_number", "tax_id"
        )
        if ident is not None and str(ident).strip():
            lines.append(f"ID: {str(ident).strip()}")
        addr = _address_line(_pick(value, "address", "addr", "location"))
        if addr:
            lines.append(addr)
        for label, keys in (
            ("Phone", ("phone", "tel", "telephone", "phone_number")),
            ("Fax", ("fax", "fax_number")),
            ("Email", ("email", "mail", "email_address")),
        ):
            v = _pick(value, *keys)
            if v is not None and str(v).strip():
                lines.append(f"{label}: {str(v).strip()}")
        return name, lines
    if isinstance(value, list):
        parts = [str(v).strip() for v in value if str(v).strip()]
    elif isinstance(value, str):
        parts = [ln.strip() for ln in value.splitlines() if ln.strip()]
    else:
        return None, []
    if not parts:
        return None, []
    return parts[0], parts[1:]


def _discount_is_percentage(params: dict, raw_discount: Any) -> bool:
    """Fixed-amount vs percentage discount detection.

    Explicit wins: a ``discount_type`` / ``discount_is_percentage``
    param from the classifier, or a ``%`` inside the raw value string.
    Everything else defaults to a fixed amount (backward compatible).
    """
    dtype = _pick(params, "discount_type", "discount_kind")
    if isinstance(dtype, str) and dtype.strip():
        return dtype.strip().lower() in ("percent", "percentage", "pct", "rate", "%")
    flag = _pick(params, "discount_is_percentage", "is_percentage_discount")
    if isinstance(flag, bool):
        return flag
    return isinstance(raw_discount, str) and "%" in raw_discount


def build_invoice_spec(params: dict) -> dict:
    """Professional invoice: parties, itemized list, live totals.

    Row numbers are computed here, never guessed by a model. No
    ROUND() in the formulas — the money number format rounds the
    display (rounding inside formulas accumulates decimal-level drift).
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

    raw_discount = _pick(
        params, "discount", "discount_amount", "discount_pct", "discount_percent"
    )
    disc_is_pct = _discount_is_percentage(params, raw_discount)
    if disc_is_pct:
        discount = to_rate(raw_discount) or 0.0
        discount = min(max(discount, 0.0), 1.0)
    else:
        discount = to_number(raw_discount) or 0.0
        discount = max(discount, 0.0)

    inv_number = _pick(params, "invoice_number", "number", "invoice_no", "id")
    inv_number = str(inv_number).strip() if inv_number is not None else ""
    inv_date = to_iso_date(_pick(params, "date", "invoice_date")) or ""
    due_date = to_iso_date(_pick(params, "due_date", "due")) or ""
    seller_name, seller_lines = _coerce_party_info(
        _pick(params, "seller", "from", "business", "company", "seller_name"),
        ("name", "seller_name", "seller", "business", "company"),
    )
    client_name, client_lines = _coerce_party_info(
        _pick(params, "client", "to", "customer", "bill_to", "client_name"),
        ("name", "client_name", "client", "customer", "bill_to"),
    )
    notes = _pick(params, "notes", "note", "terms", "payment_terms")
    note_lines: List[str] = []
    if notes is not None:
        if isinstance(notes, list):
            note_lines = [str(v).strip() for v in notes if str(v).strip()]
        elif isinstance(notes, str):
            note_lines = [ln.strip() for ln in notes.splitlines() if ln.strip()]
    note_lines = [ln[:200] for ln in note_lines][:8]

    money = _currency_fmt(_pick(params, "currency")) or MONEY_FMT

    blocks: List[dict] = [
        {
            "cell": "A1",
            "text": "INVOICE",
            "bold": True,
            "font_size": 16,
            "font_color": "16304F",
        },
    ]
    row = 3
    if inv_number:
        blocks += [
            {"cell": f"A{row}", "text": "Invoice #"},
            {"cell": f"B{row}", "text": inv_number},
        ]
        row += 1
    if inv_date:
        blocks += [
            {"cell": f"A{row}", "text": "Date"},
            {"cell": f"B{row}", "text": inv_date},
        ]
        row += 1
    if due_date:
        blocks += [
            {"cell": f"A{row}", "text": "Due Date"},
            {"cell": f"B{row}", "text": due_date},
        ]
        row += 1

    row += 1  # breathing room between the meta and the parties

    if seller_name:
        blocks.append(
            {
                "cell": f"A{row}",
                "text": "From",
                "bold": True,
                "font_size": 12,
                "font_color": "16304F",
            }
        )
        row += 1
        blocks.append({"cell": f"A{row}", "text": seller_name, "bold": True})
        row += 1
        for ln in seller_lines:
            blocks.append({"cell": f"A{row}", "text": ln})
            row += 1
        row += 1
    if client_name:
        blocks.append(
            {
                "cell": f"A{row}",
                "text": "Bill To",
                "bold": True,
                "font_size": 12,
                "font_color": "16304F",
            }
        )
        row += 1
        blocks.append({"cell": f"A{row}", "text": client_name, "bold": True})
        row += 1
        for ln in client_lines:
            blocks.append({"cell": f"A{row}", "text": ln})
            row += 1
        row += 1

    items_start = row
    header_row = items_start  # noqa
    first_data = items_start + 1
    last_data = first_data + len(items) - 1

    item_rows: List[List[Any]] = []
    for i, (desc, qty, price) in enumerate(items):
        r = first_data + i
        item_rows.append([desc, qty, price, f"=B{r}*C{r}"])

    # Totals stack directly under the items table: the Subtotal rides
    # the table's total row; Discount and Tax stack below it (computed,
    # never overlapping). The discount applies BEFORE the tax — the
    # tax base is (subtotal + discount) with the discount negative.
    subtotal_row = last_data + 1
    r = subtotal_row + 1
    disc_row: Optional[int] = None
    tax_row: Optional[int] = None
    if discount > 0:
        disc_row = r
        r += 1
    if tax_rate > 0:
        tax_row = r
        r += 1
    total_due_row = r

    # totals block: labels in C, money in D (aligned under Amount)
    total_blocks: List[dict] = []
    if disc_row is not None:
        if disc_is_pct:
            pct = round(discount * 100, 4)
            disc_label = f"Discount ({pct:g}%)"
            disc_formula = f"=-D{subtotal_row}*{_fmt_num(discount)}"
        else:
            disc_label = "Discount"
            disc_formula = f"=-{_fmt_num(discount)}"
        total_blocks += [
            {"cell": f"C{disc_row}", "text": disc_label},
            {
                "cell": f"D{disc_row}",
                "text": disc_formula,
                "number_format": money,
            },
        ]
    if tax_row is not None:
        pct = round(tax_rate * 100, 4)
        tax_base = (
            f"(D{subtotal_row}+D{disc_row})"
            if disc_row is not None
            else f"D{subtotal_row}"
        )
        total_blocks += [
            {"cell": f"C{tax_row}", "text": f"Tax ({pct:g}%)"},
            {
                "cell": f"D{tax_row}",
                "text": f"={tax_base}*{_fmt_num(tax_rate)}",
                "number_format": money,
            },
        ]
    total_formula = f"=D{subtotal_row}"
    if disc_row is not None:
        total_formula += f"+D{disc_row}"
    if tax_row is not None:
        total_formula += f"+D{tax_row}"
    total_blocks += [
        {
            "cell": f"C{total_due_row}",
            "text": "TOTAL DUE",
            "bold": True,
            "font_size": 12,
        },
        {
            "cell": f"D{total_due_row}",
            "text": total_formula,
            "bold": True,
            "font_size": 12,
            "number_format": money,
        },
    ]

    # Notes / terms & conditions section under the totals (visible in
    # the sheet — not just a muted sheet note).
    if note_lines:
        nrow = total_due_row + 2
        blocks.append(
            {
                "cell": f"A{nrow}",
                "text": "Notes / Terms & Conditions",
                "bold": True,
                "font_color": "16304F",
            }
        )
        for ln in note_lines:
            nrow += 1
            blocks.append({"cell": f"A{nrow}", "text": ln})

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
                "column_widths": {"A": 34, "B": 12, "C": 14, "D": 16},
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
            }
        ],
    }


# ── Standard pattern entry points (used by the dynamic registry) ──────

build_spec = build_invoice_spec
