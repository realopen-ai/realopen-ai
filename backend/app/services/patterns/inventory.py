"""
Inventory tracker pattern — home inventory / personal belongings /
collections / small side businesses (deterministic).

A complete, self-updating inventory workbook for everyday users:

  Inventory       Item ID (auto INV-001…) | Item Name | Category |
                  Brand / Model | Qty | Min Stock | Location |
                  Purchase Date | Purchase Price | Current Value |
                  Condition | Serial Number | Warranty Until |
                  Status | Lent To | Date Lent | Notes |
                  Last Updated | Total Purchase Value | Total
                  Current Value | Days Owned — dropdowns for
                  Category (live from the Categories sheet), Location
                  (live from the Locations sheet), Condition and
                  Status; low-stock rows turn red, warranties inside
                  60 days turn orange (expired red), status cells
                  color-code softly; header frozen, auto-filter on,
                  panes frozen so ID + Name stay visible scrolling right
  Dashboard       KPI cards (items tracked, unique names, purchase vs
                  current value, unrealized gain/loss, lent out, low
                  stock, warranty warnings, units in stock), Top 5
                  most valuable items, the 10 most recently added
                  items, a live Lent Out tracker, an items-by-category
                  pie chart and an items-by-location bar chart
  Categories      editable category list + live item counts — the
                  Inventory dropdown reads THIS range, so adding a
                  row here extends the dropdown automatically
  Locations       editable location list + live item counts — feeds
                  the Inventory Location dropdown the same way
  Instructions    plain-English how-to for non-power users
  Calc (hidden)   ranking helpers (top-value with tie-break epsilon,
                  recency rank, lent rank) + display sanitizers
                  (blank inputs → "—") aligned 1:1 with the
                  Inventory rows

CALCULATION SEMANTICS (all LIVE formulas — nothing is frozen at
build time; every edit updates every stat):

  Item ID     = "INV-" & dense count of named rows above (renumbers
                itself when a row is cleared)
  totals      = Qty × Purchase Price / Qty × Current Value (blank
                until the row has all three factors)
  days owned  = TODAY() − Purchase Date
  gain/loss   = Σ Total Current Value − Σ Total Purchase Value
  low stock   = rows where Qty ≤ Min Stock (both present)
  warranty    = expiry within TODAY()..TODAY()+60 (orange), past
                expiry (red text)
  top 5       = value + ROW()/1e6 tie-break epsilon → LARGE/MATCH —
                stable ranking even with duplicate values
  recency     = count of named rows from the row to the bottom —
                rank 1 is the newest (bottom-most) filled row
  lent list   = sequential rank of "Lent Out" rows

Every formula reference is computed from the actual layout rows this
module emits, so off-by-N row math is impossible by construction. No
ROUND() anywhere — display rounding is the number format's job.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from app.services.patterns.utils import (
    _pick,
    currency_fmt,
    to_iso_date,
    to_number,
)

# Registry key — must match the pattern stanza in
# prompts/pattern_classifier.md.
PATTERN_NAME = "inventory"

PATTERN_DESCRIPTION = (
    "Creates an inventory tracker spreadsheet: items with category, "
    "quantity, low-stock alerts, locations, purchase price vs current "
    "value, gain/loss, condition, warranty expiry warnings, a lending "
    "tracker and a dashboard with charts. Use when the user wants to "
    "track a home inventory, personal belongings, collections or "
    "stock for a small side business. Do NOT use it for financial "
    "budgets, loans, invoices, investment portfolios or daily habits."
)

# Routing keywords/stems — drive the cheap pre-gate and the classifier
# shortlist (see excel_gen._shortlist_patterns).
PATTERN_KEYWORDS = (
    "inventory",
    "belonging",
    "collection",
    "warranty",
    "serial",
)

# Fixed layout geometry (rows never shift — formulas pin to these).
MAX_ITEM_ROWS = 50  # Inventory data rows 5..54
MAX_CATEGORY_ROWS = 20  # Categories rows 5..24
MAX_LOCATION_ROWS = 15  # Locations rows 5..19
TOP_N = 5  # most valuable items on the Dashboard
RECENT_N = 10  # recently added items on the Dashboard
LENT_N = 6  # lent-out rows shown on the Dashboard

R0 = 5  # first Inventory data row
RN = 4 + MAX_ITEM_ROWS  # last Inventory data row (54)
CAT_R0, CAT_RN = 5, 4 + MAX_CATEGORY_ROWS
LOC_R0, LOC_RN = 5, 4 + MAX_LOCATION_ROWS

_DATE_FMT = "yyyy-mm-dd"
_INT_FMT = "#,##0"
_DAYS_FMT = "0"

DEFAULT_CATEGORIES = (
    "Electronics",
    "Clothing",
    "Kitchen",
    "Furniture",
    "Tools",
    "Books",
    "Sports",
    "Other",
)
DEFAULT_LOCATIONS = (
    "Home Office",
    "Living Room",
    "Bedroom",
    "Kitchen",
    "Bathroom",
    "Garage",
    "Storage Unit",
)

CONDITIONS = ("New", "Excellent", "Good", "Fair", "Poor")
STATUSES = ("In Stock", "Lent Out", "Sold", "Lost", "Retired")

# Design tokens (same palette as the converter).
NAVY = "16304F"
STEEL = "1B3A5C"
GOLD = "C9A227"
MUTED = "5C6470"
GREY = "8A94A3"

# Excel's classic Good / Bad / Neutral conditional-format palettes —
# instantly readable in every viewer.
CF_GREEN_FILL = "C6EFCE"
CF_GREEN_TEXT = "006100"
CF_RED_FILL = "FFC7CE"
CF_RED_TEXT = "9C0006"
CF_GOLD_FILL = "FFF2CC"
CF_GOLD_TEXT = "7F6000"
CF_GREY_FILL = "E7E6E6"
CF_GREY_TEXT = "595959"
CF_GREY_FILL_2 = "F2F2F2"
CF_GREY_TEXT_2 = "7F7F7F"
# Soft orange for the warranty-expiring window (distinct from gold).
CF_ORANGE_FILL = "FCE4D6"
CF_ORANGE_TEXT = "C55A11"

# Tie-break epsilon for the Top-5 ranking: value + ROW()/1e6 keeps
# duplicates distinguishable while staying far below a cent.
_EPS = 1000000


# ── Param coercion ────────────────────────────────────────────────────


def _clean_list(raw: Any, limit: int) -> List[str]:
    """A list of plain strings → stripped, deduped, capped."""
    if not isinstance(raw, list):
        return []
    seen = set()
    out: List[str] = []
    for entry in raw[:limit]:
        if not isinstance(entry, str):
            continue
        s = entry.strip()[:40]
        if not s or s.lower() in seen:
            continue
        seen.add(s.lower())
        out.append(s)
    return out


def _normalize_status(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    s = value.strip().title()
    aliases = {
        "In Stock": "In Stock",
        "Stock": "In Stock",
        "Available": "In Stock",
        "Lent": "Lent Out",
        "Lent Out": "Lent Out",
        "On Loan": "Lent Out",
        "Borrowed": "Lent Out",
        "Sold": "Sold",
        "Lost": "Lost",
        "Missing": "Lost",
        "Retired": "Retired",
        "Disposed": "Retired",
        "Donated": "Retired",
    }
    return aliases.get(s)


def _normalize_condition(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    s = value.strip().title()
    return s if s in CONDITIONS else None


def _normalize_item_entry(entry: Any) -> Optional[dict]:
    """One items entry → a row dict or None (unusable).

    Accepts the documented object form and bare item-name strings.
    """
    if isinstance(entry, str):
        name = entry.strip()
        if not name:
            return None
        return {
            "name": name[:60],
            "category": None,
            "brand_model": None,
            "quantity": 1,
            "min_stock": None,
            "location": None,
            "purchase_date": None,
            "purchase_price": None,
            "current_value": None,
            "condition": None,
            "serial_number": None,
            "warranty_expiry": None,
            "status": "In Stock",
            "lent_to": None,
            "date_lent": None,
            "notes": None,
        }

    if not isinstance(entry, dict):
        return None

    name = str(_pick(entry, "name", "item", "item_name", "title") or "").strip()
    if not name:
        return None

    quantity = to_number(_pick(entry, "quantity", "qty", "count"))
    if quantity is None or quantity < 0:
        quantity = 1

    status = _normalize_status(_pick(entry, "status", "state")) or "In Stock"

    return {
        "name": name[:60],
        "category": (str(_pick(entry, "category") or "").strip()[:40] or None),
        "brand_model": (
            str(
                _pick(entry, "brand_model", "brand", "model", "brand_and_model") or ""
            ).strip()[:60]
            or None
        ),
        "quantity": quantity,
        "min_stock": to_number(_pick(entry, "min_stock", "min", "minimum", "reorder")),
        "location": (
            str(_pick(entry, "location", "room", "place") or "").strip()[:40] or None
        ),
        "purchase_date": to_iso_date(
            _pick(entry, "purchase_date", "bought", "acquired")
        ),
        "purchase_price": to_number(
            _pick(entry, "purchase_price", "price", "cost", "bought_for")
        ),
        "current_value": to_number(
            _pick(entry, "current_value", "value", "worth", "estimated_value")
        ),
        "condition": _normalize_condition(_pick(entry, "condition")),
        "serial_number": (
            str(_pick(entry, "serial_number", "serial", "sn") or "").strip()[:40]
            or None
        ),
        "warranty_expiry": to_iso_date(
            _pick(entry, "warranty_expiry", "warranty", "warranty_until")
        ),
        "status": status,
        "lent_to": (
            str(_pick(entry, "lent_to", "borrower") or "").strip()[:40] or None
        ),
        "date_lent": to_iso_date(_pick(entry, "date_lent", "lent_date")),
        "notes": (
            str(_pick(entry, "notes", "note", "comment") or "").strip()[:200] or None
        ),
    }


def coerce_inventory_params(params: dict) -> dict:
    """Validate + normalize classifier params; raises ValueError."""
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    title = str(_pick(params, "inventory_name", "title", "name") or "").strip()[:60]
    if not title:
        title = "Inventory Tracker"

    currency = str(_pick(params, "currency") or "").strip().upper() or None

    categories = _clean_list(
        _pick(params, "categories", "category_list"), MAX_CATEGORY_ROWS
    ) or list(DEFAULT_CATEGORIES)

    locations = _clean_list(
        _pick(params, "locations", "location_list"), MAX_LOCATION_ROWS
    ) or list(DEFAULT_LOCATIONS)

    raw_items = _pick(params, "items", "item_list", "inventory")
    items: List[dict] = []
    if raw_items is not None:
        if not isinstance(raw_items, list):
            raise ValueError("items must be an array")
        for entry in raw_items[:MAX_ITEM_ROWS]:
            normalized = _normalize_item_entry(entry)
            if normalized is not None:
                items.append(normalized)

    notes = _pick(params, "notes", "note")
    notes = notes.strip()[:1000] if isinstance(notes, str) and notes.strip() else None

    return {
        "title": title,
        "currency": currency,
        "categories": categories,
        "locations": locations,
        "items": items,
        "notes": notes,
    }


# ── Builder ───────────────────────────────────────────────────────────


def build_inventory_spec(params: dict) -> dict:
    """Inventory tracker workbook — every formula code-generated.

    Layout (rows computed here, never guessed by a model):

    Inventory sheet:
      row 1      title
      row 2      usage hint
      row 4      headers A..U (21 columns)
      rows 5-54  50 item rows — seeded items first, then blank rows
                 with the ID / value / days formulas pre-installed
    Categories sheet:
      rows 5-24  category names (8 defaults when none given) + a
                 live COUNTIF "Items" column; the Inventory Category
                 dropdown is sourced from THIS range
    Locations sheet:
      rows 5-19  location names + live counts, same dropdown wiring
    Calc sheet (hidden):
      rows 5-54  helpers aligned 1:1 with the Inventory rows:
                 C top-value (T + ROW()/1e6), D recency rank,
                 E lent rank, F/G/H lent display values,
                 I/J category + status display sanitizers
    Dashboard:
      KPIs rows 6-14, Top 5 rows 18-22, Recently Added rows 26-35,
      Lent Out rows 39-44, pie chart F3, bar chart F22
    """
    p = coerce_inventory_params(params)
    title: str = p["title"]
    currency: Optional[str] = p["currency"]
    categories: List[str] = p["categories"]
    locations: List[str] = p["locations"]
    items: List[dict] = p["items"]
    notes = p["notes"]

    money_fmt = currency_fmt(currency) or "#,##0.00"

    today = date.today().isoformat()

    # ═════════════════════════════ Inventory sheet ═════════════════
    inv_rows: List[List[Any]] = []
    for i in range(MAX_ITEM_ROWS):
        r = R0 + i
        item = items[i] if i < len(items) else None
        inv_rows.append(
            [
                # A Item ID — dense auto-numbering over named rows
                '=IF($B{r}="","","INV-"&TEXT(COUNTA($B${r0}:$B{r}),"000"))'.format(
                    r=r, r0=R0
                ),
                # B..R user inputs
                item["name"] if item else None,
                item["category"] if item else None,
                item["brand_model"] if item else None,
                item["quantity"] if item else None,
                item["min_stock"] if item else None,
                item["location"] if item else None,
                item["purchase_date"] if item else None,
                item["purchase_price"] if item else None,
                item["current_value"] if item else None,
                item["condition"] if item else None,
                item["serial_number"] if item else None,
                item["warranty_expiry"] if item else None,
                item["status"] if item else None,
                item["lent_to"] if item else None,
                item["date_lent"] if item else None,
                item["notes"] if item else None,
                # R Last Updated — seeded rows carry the build date;
                # afterwards the user stamps it when editing a row
                today if item else None,
                # S Total Purchase Value
                '=IF(OR($B{r}="",$E{r}="",$I{r}=""),"",$E{r}*$I{r})'.format(r=r),
                # T Total Current Value
                '=IF(OR($B{r}="",$E{r}="",$J{r}=""),"",$E{r}*$J{r})'.format(r=r),
                # U Days Owned (bonus — days since purchase)
                '=IF(OR($B{r}="",$H{r}=""),"",TODAY()-$H{r})'.format(r=r),
            ]
        )

    inventory_sheet: Dict[str, Any] = {
        "name": "Inventory",
        "tab_color": NAVY,
        "freeze_panes": "C5",  # header rows + Item ID / Name stay visible
        "column_widths": {
            "A": 10,
            "B": 26,
            "C": 14,
            "D": 18,
            "E": 8,
            "F": 11,
            "G": 15,
            "H": 13,
            "I": 13,
            "J": 13,
            "K": 11,
            "L": 16,
            "M": 13,
            "N": 12,
            "O": 14,
            "P": 12,
            "Q": 26,
            "R": 13,
            "S": 15,
            "T": 15,
            "U": 11,
        },
        "text_blocks": [
            {
                "cell": "A1",
                "text": title,
                "bold": True,
                "font_size": 14,
                "font_color": NAVY,
            },
            {
                "cell": "A2",
                "text": (
                    "Type a name in the first empty row and fill what you "
                    "know — the ID, values and Dashboard update automatically. "
                    "See the Instructions tab."
                ),
                "italic": True,
                "font_color": MUTED,
            },
        ],
        "tables": [
            {
                "start_cell": "A4",
                "headers": [
                    "Item ID",
                    "Item Name",
                    "Category",
                    "Brand / Model",
                    "Qty",
                    "Min Stock",
                    "Location",
                    "Purchase Date",
                    "Purchase Price",
                    "Current Value",
                    "Condition",
                    "Serial Number",
                    "Warranty Until",
                    "Status",
                    "Lent To",
                    "Date Lent",
                    "Notes",
                    "Last Updated",
                    "Total Purchase Value",
                    "Total Current Value",
                    "Days Owned",
                ],
                "rows": inv_rows,
                "number_formats": {
                    "E": _INT_FMT,
                    "F": _INT_FMT,
                    "H": _DATE_FMT,
                    "I": money_fmt,
                    "J": money_fmt,
                    "M": _DATE_FMT,
                    "P": _DATE_FMT,
                    "R": _DATE_FMT,
                    "S": money_fmt,
                    "T": money_fmt,
                    "U": _DAYS_FMT,
                },
                "alignments": {
                    "A": "center",
                    "E": "center",
                    "F": "center",
                    "H": "center",
                    "K": "center",
                    "M": "center",
                    "N": "center",
                    "P": "center",
                    "R": "center",
                    "S": "right",
                    "T": "right",
                    "U": "center",
                },
                "auto_filter": True,
            }
        ],
        "data_validation": [
            {
                # live range source: rows added on the Categories sheet
                # appear in this dropdown automatically
                "range": f"C{R0}:C{RN}",
                "source_range": f"Categories!$A${CAT_R0}:$A${CAT_RN}",
                "allow_blank": True,
                "prompt_title": "Category",
                "prompt": (
                    "Pick a category. New one? Add it on the Categories "
                    "tab first — it appears here instantly."
                ),
                "error_title": "Not in the category list",
                "error": (
                    "Pick a category from the list, or add it on the " "Categories tab."
                ),
                "error_style": "warning",
            },
            {
                "range": f"G{R0}:G{RN}",
                "source_range": f"Locations!$A${LOC_R0}:$A${LOC_RN}",
                "allow_blank": True,
                "prompt_title": "Location",
                "prompt": (
                    "Where the item lives. Add new places on the " "Locations tab."
                ),
                "error_title": "Not in the location list",
                "error": (
                    "Pick a location from the list, or add it on the " "Locations tab."
                ),
                "error_style": "warning",
            },
            {
                "range": f"K{R0}:K{RN}",
                "values": list(CONDITIONS),
                "allow_blank": True,
                "prompt_title": "Condition",
                "prompt": "New / Excellent / Good / Fair / Poor.",
                "error_style": "warning",
            },
            {
                "range": f"N{R0}:N{RN}",
                "values": list(STATUSES),
                "allow_blank": True,
                "prompt_title": "Status",
                "prompt": (
                    "In Stock = you have it · Lent Out = with someone · "
                    "Sold / Lost / Retired = no longer active."
                ),
                "error_title": "Invalid status",
                "error": (
                    "Pick one of: In Stock, Lent Out, Sold, Lost, Retired "
                    "(the lending tracker depends on the exact wording)."
                ),
                "error_style": "stop",
            },
        ],
        "conditional_formats": [
            # low stock — red Qty cell
            {
                "range": f"E{R0}:E{RN}",
                "rules": [
                    {
                        "type": "formula",
                        "value": 'AND($E{r}<>"",$F{r}<>"",$E{r}<=$F{r})'.format(r=R0),
                        "fill": CF_RED_FILL,
                        "font_color": CF_RED_TEXT,
                        "bold": True,
                        "stop_if_true": False,
                    }
                ],
            },
            # warranty — orange inside 60 days, red text when expired
            {
                "range": f"M{R0}:M{RN}",
                "rules": [
                    {
                        "type": "formula",
                        "value": (
                            'AND($M{r}<>"",$M{r}>=TODAY(),' "$M{r}<=TODAY()+60)"
                        ).format(r=R0),
                        "fill": CF_ORANGE_FILL,
                        "font_color": CF_ORANGE_TEXT,
                        "bold": True,
                        "stop_if_true": False,
                    },
                    {
                        "type": "formula",
                        "value": 'AND($M{r}<>"",$M{r}<TODAY())'.format(r=R0),
                        "font_color": CF_RED_TEXT,
                        "stop_if_true": False,
                    },
                ],
            },
            # soft status color coding
            {
                "range": f"N{R0}:N{RN}",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "In Stock",
                        "fill": CF_GREEN_FILL,
                        "font_color": CF_GREEN_TEXT,
                        "stop_if_true": False,
                    },
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "Lent Out",
                        "fill": CF_GOLD_FILL,
                        "font_color": CF_GOLD_TEXT,
                        "stop_if_true": False,
                    },
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "Sold",
                        "fill": CF_GREY_FILL,
                        "font_color": CF_GREY_TEXT,
                        "stop_if_true": False,
                    },
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "Lost",
                        "fill": CF_RED_FILL,
                        "font_color": CF_RED_TEXT,
                        "stop_if_true": False,
                    },
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "Retired",
                        "fill": CF_GREY_FILL_2,
                        "font_color": CF_GREY_TEXT_2,
                        "stop_if_true": False,
                    },
                ],
            },
        ],
        "protect": {
            # everything the user fills in is editable; the ID, value
            # and days-owned formula columns stay locked
            "unlocked_ranges": [f"B{R0}:R{RN}"],
        },
        "notes": notes
        or (
            "Type in the white cells — Item ID, Total Purchase/Current "
            "Value and Days Owned are automatic. Qty turns red when it "
            "drops to its Min Stock level; Warranty Until turns orange "
            "inside 60 days (red text once expired); the Status cell "
            "color-codes itself. Add new categories/locations on their "
            "tabs — the dropdowns here pick them up automatically. Stamp "
            "Last Updated with today's date whenever you edit a row."
        ),
    }

    # ═════════════════════════════ Categories sheet ═════════════════
    cat_rows: List[List[Any]] = []
    for i in range(MAX_CATEGORY_ROWS):
        r = CAT_R0 + i
        name = categories[i] if i < len(categories) else None
        cat_rows.append(
            [
                name,
                '=IF($A{r}="","",COUNTIF(Inventory!$C${r0}:$C${rN},$A{r}))'.format(
                    r=r, r0=R0, rN=RN
                ),
            ]
        )

    categories_sheet: Dict[str, Any] = {
        "name": "Categories",
        "tab_color": STEEL,
        "column_widths": {"A": 26, "B": 10},
        "text_blocks": [
            {
                "cell": "A1",
                "text": "Categories",
                "bold": True,
                "font_size": 14,
                "font_color": NAVY,
            },
            {
                "cell": "A2",
                "text": (
                    "One category per row. Add a new row and it appears "
                    "in the Inventory dropdown instantly — the Items "
                    "column counts itself."
                ),
                "italic": True,
                "font_color": MUTED,
            },
        ],
        "tables": [
            {
                "start_cell": "A4",
                "headers": ["Category", "Items"],
                "rows": cat_rows,
                "number_formats": {"B": _DAYS_FMT},
                "alignments": {"B": "center"},
            }
        ],
        "protect": {"unlocked_ranges": [f"A{CAT_R0}:A{CAT_RN}"]},
        "notes": (
            "Type new categories in column A — the Inventory Category "
            "dropdown reads this exact range, so new rows appear there "
            "without any setup. The Items column is a live COUNTIF; it "
            "only counts Inventory rows whose Category matches."
        ),
    }

    # ═════════════════════════════ Locations sheet ══════════════════
    loc_rows: List[List[Any]] = []
    for i in range(MAX_LOCATION_ROWS):
        r = LOC_R0 + i
        name = locations[i] if i < len(locations) else None
        loc_rows.append(
            [
                name,
                '=IF($A{r}="","",COUNTIF(Inventory!$G${r0}:$G${rN},$A{r}))'.format(
                    r=r, r0=R0, rN=RN
                ),
            ]
        )

    locations_sheet: Dict[str, Any] = {
        "name": "Locations",
        "tab_color": STEEL,
        "column_widths": {"A": 26, "B": 10},
        "text_blocks": [
            {
                "cell": "A1",
                "text": "Locations",
                "bold": True,
                "font_size": 14,
                "font_color": NAVY,
            },
            {
                "cell": "A2",
                "text": (
                    "Your storage places, one per row. New rows join the "
                    "Inventory Location dropdown automatically."
                ),
                "italic": True,
                "font_color": MUTED,
            },
        ],
        "tables": [
            {
                "start_cell": "A4",
                "headers": ["Location", "Items"],
                "rows": loc_rows,
                "number_formats": {"B": _DAYS_FMT},
                "alignments": {"B": "center"},
            }
        ],
        "protect": {"unlocked_ranges": [f"A{LOC_R0}:A{LOC_RN}"]},
        "notes": (
            "Type new locations in column A — the Inventory Location "
            "dropdown reads this exact range. The Items column counts "
            "Inventory rows placed here."
        ),
    }

    # ═════════════════════════════ Calc sheet (hidden) ═══════════════
    helper_rows: List[List[Any]] = []
    for i in range(MAX_ITEM_ROWS):
        r = R0 + i
        helper_rows.append(
            [
                # C top-value rank key: T (0 when missing) + ROW()/1e6
                # — duplicates stay distinguishable, epsilon ≪ a cent
                '=IF(Inventory!$B{r}="","",'
                'IF(Inventory!$T{r}="",0,Inventory!$T{r})+ROW()/{eps})'.format(
                    r=r, eps=_EPS
                ),
                # D recency rank: named rows from here to the bottom —
                # rank 1 = newest (bottom-most) filled row
                '=IF(Inventory!$B{r}="","",'
                "COUNTA(Inventory!$B{r}:$B${rN}))".format(r=r, rN=RN),
                # E lent rank: 1..n over "Lent Out" rows, top to bottom
                '=IF(Inventory!$N{r}="Lent Out",'
                'COUNTIF(Inventory!$N${r0}:$N{r},"Lent Out"),"")'.format(r=r, r0=R0),
                # F lent-to display ("—" when left blank)
                '=IF(Inventory!$N{r}="Lent Out",'
                'IF(Inventory!$O{r}="","—",Inventory!$O{r}),"")'.format(r=r),
                # G date-lent display (pre-formatted text)
                '=IF(Inventory!$N{r}="Lent Out",'
                'IF(Inventory!$P{r}="","—",'
                'TEXT(Inventory!$P{r},"yyyy-mm-dd")),"")'.format(r=r),
                # H days out (blank until a date lent exists)
                '=IF(OR(Inventory!$N{r}<>"Lent Out",'
                'Inventory!$P{r}=""),"",TODAY()-Inventory!$P{r})'.format(r=r),
                # I category display sanitizer
                '=IF(Inventory!$B{r}="","",'
                'IF(Inventory!$C{r}="","—",Inventory!$C{r}))'.format(r=r),
                # J status display sanitizer
                '=IF(Inventory!$B{r}="","",'
                'IF(Inventory!$N{r}="","—",Inventory!$N{r}))'.format(r=r),
            ]
        )

    calc_sheet: Dict[str, Any] = {
        "name": "Calc",
        "tab_color": GREY,
        "hidden": True,
        "column_widths": {
            "C": 14,
            "D": 10,
            "E": 10,
            "F": 16,
            "G": 14,
            "H": 10,
            "I": 16,
            "J": 12,
        },
        "text_blocks": [
            {
                "cell": "A1",
                "text": "Hidden helpers — rows run 1:1 with the Inventory sheet.",
                "italic": True,
                "font_color": MUTED,
            },
            {
                "cell": "A2",
                "text": (
                    "C = Top-5 rank key (value + ROW()/1e6 tie-break) · "
                    "D = recency rank (1 = newest) · E = Lent Out rank"
                ),
                "italic": True,
                "font_color": MUTED,
            },
            {
                "cell": "A3",
                "text": (
                    "F/G/H = lending display values · I/J = category/status "
                    "display sanitizers"
                ),
                "italic": True,
                "font_color": MUTED,
            },
        ],
        "tables": [
            {
                "start_cell": "C4",
                "headers": [
                    "Top Value",
                    "Recency",
                    "Lent Rank",
                    "Lent To",
                    "Lent Date",
                    "Days Out",
                    "Category",
                    "Status",
                ],
                "rows": helper_rows,
                "number_formats": {},
                "zebra": False,
            }
        ],
        "protect": True,
        "notes": (
            "Hidden calculation sheet. The helper columns C..J align "
            "1:1 with Inventory rows 5..54: C ranks items by current "
            "value (with a ROW()/1e6 tie-break so duplicate values "
            "still rank deterministically), D ranks recency (1 = "
            "bottom-most = newest), E ranks Lent Out rows, F/G/H hold "
            "display-safe lending values, I/J sanitize blank category/"
            "status cells into em-dashes. The Dashboard tables are "
            "LARGE/MATCH/INDEX lookups over these helpers. Extend by "
            "dragging all helper columns down together with the "
            "Inventory rows. Right-click a tab → Unhide to inspect."
        ),
    }

    # ═════════════════════════════ Dashboard sheet ══════════════════
    inv_b = f"Inventory!$B${R0}:$B${RN}"
    inv_e = f"Inventory!$E${R0}:$E${RN}"
    inv_f = f"Inventory!$F${R0}:$F${RN}"
    inv_n = f"Inventory!$N${R0}:$N${RN}"
    inv_m = f"Inventory!$M${R0}:$M${RN}"
    inv_s = f"Inventory!$S${R0}:$S${RN}"
    inv_t = f"Inventory!$T${R0}:$T${RN}"

    kpi_blocks: List[dict] = [
        {
            "cell": "A1",
            "text": f"{title} — Dashboard",
            "bold": True,
            "font_size": 16,
            "font_color": NAVY,
        },
        {"cell": "A3", "text": "Today"},
        {
            "cell": "B3",
            "text": "=TODAY()",
            "number_format": _DATE_FMT,
            "bold": True,
        },
        {
            "cell": "A5",
            "text": "OVERVIEW",
            "bold": True,
            "font_size": 12,
            "font_color": GOLD,
        },
        {"cell": "A6", "text": "Items Tracked"},
        {
            "cell": "B6",
            "text": f"=COUNTA({inv_b})",
            "number_format": _DAYS_FMT,
            "bold": True,
        },
        {"cell": "A7", "text": "Unique Item Names"},
        {
            "cell": "B7",
            "text": ('=SUMPRODUCT(({b}<>"")/' 'COUNTIF({b},{b}&""))').format(b=inv_b),
            "number_format": _DAYS_FMT,
            "bold": True,
        },
        {"cell": "A8", "text": "Total Purchase Value"},
        {
            "cell": "B8",
            "text": f"=SUM({inv_s})",
            "number_format": money_fmt,
            "bold": True,
        },
        {"cell": "A9", "text": "Total Current Value"},
        {
            "cell": "B9",
            "text": f"=SUM({inv_t})",
            "number_format": money_fmt,
            "bold": True,
        },
        {"cell": "A10", "text": "Unrealized Gain / Loss"},
        {
            "cell": "B10",
            "text": "=$B$9-$B$8",
            "number_format": money_fmt,
            "bold": True,
        },
        {"cell": "A11", "text": "Items Lent Out"},
        {
            "cell": "B11",
            "text": '=COUNTIF({n},"Lent Out")'.format(n=inv_n),
            "number_format": _DAYS_FMT,
        },
        {"cell": "A12", "text": "Low Stock Items"},
        {
            "cell": "B12",
            "text": ('=SUMPRODUCT(({e}<>"")*({f}<>"")*({e}<={f}))').format(
                e=inv_e, f=inv_f
            ),
            "number_format": _DAYS_FMT,
        },
        {"cell": "A13", "text": "Warranty Expiring (60 days)"},
        {
            "cell": "B13",
            "text": (
                '=SUMPRODUCT(({m}<>"")*({m}>=TODAY())*' "({m}<=TODAY()+60))"
            ).format(m=inv_m),
            "number_format": _DAYS_FMT,
        },
        {"cell": "A14", "text": "Units In Stock"},
        {
            "cell": "B14",
            "text": '=SUMIF({n},"In Stock",{e})'.format(n=inv_n, e=inv_e),
            "number_format": _INT_FMT,
        },
        {
            "cell": "A16",
            "text": f"TOP {TOP_N} MOST VALUABLE ITEMS",
            "bold": True,
            "font_size": 12,
            "font_color": GOLD,
        },
        {
            "cell": "A24",
            "text": "RECENTLY ADDED",
            "bold": True,
            "font_size": 12,
            "font_color": GOLD,
        },
        {
            "cell": "A37",
            "text": "LENT OUT NOW",
            "bold": True,
            "font_size": 12,
            "font_color": GOLD,
        },
    ]

    # Top 5 by current value — LARGE/MATCH over the Calc rank key.
    calc_c = f"Calc!$C${R0}:$C${RN}"
    calc_i = f"Calc!$I${R0}:$I${RN}"
    top_rows: List[List[Any]] = []
    for k in range(1, TOP_N + 1):
        pos = f"MATCH(LARGE({calc_c},{k}),{calc_c},0)"
        top_rows.append(
            [
                k,
                f'=IFERROR(INDEX({inv_b},{pos}),"—")',
                f'=IFERROR(INDEX({calc_i},{pos}),"—")',
                f'=IFERROR(INDEX({inv_t},{pos}),"—")',
            ]
        )

    # Recently added — recency rank over Calc column D.
    calc_d = f"Calc!$D${R0}:$D${RN}"
    calc_j = f"Calc!$J${R0}:$J${RN}"
    recent_rows: List[List[Any]] = []
    for k in range(1, RECENT_N + 1):
        pos = f"MATCH({k},{calc_d},0)"
        recent_rows.append(
            [
                f'=IFERROR(INDEX({inv_b},{pos}),"—")',
                f'=IFERROR(INDEX({calc_i},{pos}),"—")',
                f'=IFERROR(INDEX({calc_j},{pos}),"—")',
            ]
        )

    # Lent-out tracker — rank over Calc column E, display via F..H.
    calc_e = f"Calc!$E${R0}:$E${RN}"
    calc_f = f"Calc!$F${R0}:$F${RN}"
    calc_g = f"Calc!$G${R0}:$G${RN}"
    calc_h = f"Calc!$H${R0}:$H${RN}"
    lent_rows: List[List[Any]] = []
    for k in range(1, LENT_N + 1):
        pos = f"MATCH({k},{calc_e},0)"
        lent_rows.append(
            [
                f'=IFERROR(INDEX({inv_b},{pos}),"—")',
                f'=IFERROR(INDEX({calc_f},{pos}),"—")',
                f'=IFERROR(INDEX({calc_g},{pos}),"—")',
                f'=IFERROR(INDEX({calc_h},{pos}),"—")',
            ]
        )

    dashboard_sheet: Dict[str, Any] = {
        "name": "Dashboard",
        "tab_color": GOLD,
        "no_freeze": True,
        "column_widths": {
            "A": 30,
            "B": 18,
            "C": 18,
            "D": 16,
            "E": 3,
        },
        "text_blocks": kpi_blocks,
        "tables": [
            {
                "start_cell": "A17",
                "headers": ["Rank", "Item", "Category", "Current Value"],
                "rows": top_rows,
                "number_formats": {"D": money_fmt},
                "alignments": {"A": "center"},
            },
            {
                "start_cell": "A25",
                "headers": ["Item", "Category", "Status"],
                "rows": recent_rows,
            },
            {
                "start_cell": "A38",
                "headers": ["Item", "Lent To", "Date Lent", "Days Out"],
                "rows": lent_rows,
                "number_formats": {"D": _DAYS_FMT},
                "alignments": {"D": "center"},
            },
        ],
        "conditional_formats": [
            # gain green / loss red
            {
                "range": "B10",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "greater_than",
                        "value": 0,
                        "font_color": CF_GREEN_TEXT,
                        "bold": True,
                        "stop_if_true": False,
                    },
                    {
                        "type": "cell_is",
                        "operator": "less_than",
                        "value": 0,
                        "font_color": CF_RED_TEXT,
                        "bold": True,
                        "stop_if_true": False,
                    },
                ],
            },
            # attention counters only light up when non-zero
            {
                "range": "B11",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "greater_than",
                        "value": 0,
                        "font_color": CF_GOLD_TEXT,
                        "bold": True,
                        "stop_if_true": False,
                    }
                ],
            },
            {
                "range": "B12",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "greater_than",
                        "value": 0,
                        "font_color": CF_RED_TEXT,
                        "bold": True,
                        "stop_if_true": False,
                    }
                ],
            },
            {
                "range": "B13",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "greater_than",
                        "value": 0,
                        "font_color": CF_ORANGE_TEXT,
                        "bold": True,
                        "stop_if_true": False,
                    }
                ],
            },
        ],
        "charts": [
            {
                "type": "pie",
                "title": "Items by Category",
                "anchor": "F3",
                "width": 15,
                "height": 9,
                "categories_range": f"Categories!$A${CAT_R0}:$A${CAT_RN}",
                "series": [
                    {
                        "name": "Items",
                        "values_range": f"Categories!$B${CAT_R0}:$B${CAT_RN}",
                    }
                ],
            },
            {
                "type": "bar",
                "title": "Items by Location",
                "anchor": "F22",
                "width": 15,
                "height": 9,
                "categories_range": f"Locations!$A${LOC_R0}:$A${LOC_RN}",
                "series": [
                    {
                        "name": "Items",
                        "values_range": f"Locations!$B${LOC_R0}:$B${LOC_RN}",
                    }
                ],
                "show_values": True,
                "value_numfmt": _DAYS_FMT,
            },
        ],
        "protect": True,
        "notes": (
            "Every value updates live from the Inventory sheet. Items "
            "Tracked counts named rows; Unique Item Names de-duplicates "
            "them. Unrealized Gain / Loss = total current value minus "
            "total purchase value. Low Stock counts rows whose Qty "
            "dropped to their Min Stock; Warranty Expiring counts "
            "expiries inside the next 60 days. Top 5 ranks by Total "
            "Current Value (ties broken by row). Recently Added lists "
            "the newest filled rows — append new items at the bottom of "
            "the Inventory sheet to keep it accurate. The Lent Out "
            "tracker follows the Status column."
        ),
    }

    # ═════════════════════════════ Instructions sheet ════════════════
    instructions_sheet: Dict[str, Any] = {
        "name": "Instructions",
        "tab_color": GREY,
        "column_widths": {"A": 100},
        "text_blocks": _instructions_blocks(TOP_N),
        "protect": True,
        "notes": (
            "This tab is documentation — every cell is locked. The "
            "Inventory, Dashboard, Categories, Locations and (hidden) "
            "Calc tabs do the actual work."
        ),
    }

    return {
        "filename": "inventory_tracker.xlsx",
        "sheets": [
            inventory_sheet,
            dashboard_sheet,
            categories_sheet,
            locations_sheet,
            instructions_sheet,
            calc_sheet,
        ],
    }


def _instructions_blocks(top_n: int) -> List[dict]:
    """The how-to tab: short lines, one text block each."""

    def heading(cell: str, text: str) -> dict:
        return {
            "cell": cell,
            "text": text,
            "bold": True,
            "font_size": 12,
            "font_color": STEEL,
        }

    def line(cell: str, text: str) -> dict:
        return {"cell": cell, "text": text}

    return [
        {
            "cell": "A1",
            "text": "How to Use Your Inventory Tracker",
            "bold": True,
            "font_size": 14,
            "font_color": NAVY,
        },
        heading("A3", "1. ADDING A NEW ITEM — the Inventory tab"),
        line(
            "A4",
            "Go to the first empty row and type the item's name in the Item Name column.",
        ),
        line(
            "A5",
            "Pick a Category and a Location from the dropdowns; type the Brand / Model "
            "if you know it.",
        ),
        line(
            "A6",
            "Enter Quantity, Purchase Date and Purchase Price — everything else is optional.",
        ),
        line(
            "A7",
            "The Item ID (INV-001…), Total Purchase Value, Total Current Value and Days Owned "
            "fill in automatically.",
        ),
        line(
            "A8",
            "50 rows are pre-wired — you never need to set up formulas. "
            "To go beyond 50: select the last row,",
        ),
        line(
            "A9",
            "drag-fill it down, then unhide the Calc tab (right-click a tab → Unhide) "
            "and drag its helper columns C..J",
        ),
        line(
            "A10",
            "down the same number of rows so the Dashboard rankings keep working.",
        ),
        heading("A12", "2. UPDATES AS THINGS CHANGE"),
        line(
            "A13",
            "Update Quantity when you buy or use items; update Current Value when prices move.",
        ),
        line(
            "A14",
            "Stamp Last Updated with today's date whenever you edit a row "
            "(seeded rows carry today's date).",
        ),
        line("A15", "Days Owned counts itself from the Purchase Date."),
        heading("A17", "3. LOW STOCK ALERTS"),
        line(
            "A18",
            "Give an item a Min Stock Level (e.g. keep at least 2 spare phone chargers).",
        ),
        line(
            "A19",
            "When its Qty drops to or below that level, the Qty cell turns red on the "
            "Inventory tab.",
        ),
        line(
            "A20", "The Dashboard's Low Stock Items counter lights up the same moment."
        ),
        heading("A22", "4. LENDING THINGS OUT"),
        line(
            "A23",
            "Set the item's Status to Lent Out (dropdown), then fill Lent To and Date Lent.",
        ),
        line(
            "A24",
            "The Status cell turns gold, the Dashboard counts it, and the Lent Out Now list shows",
        ),
        line("A25", "who has it, since when, and for how many days."),
        line(
            "A26",
            "When it comes back, set Status back to In Stock and clear Lent To / Date Lent.",
        ),
        heading("A28", "5. WARRANTY WATCH"),
        line("A29", "Enter Warranty Until dates when items have coverage."),
        line(
            "A30",
            "The date turns orange inside the 60 days before expiry and red text once expired.",
        ),
        line("A31", "The Dashboard counts expiries in the next 60 days."),
        heading("A33", "6. THE DASHBOARD"),
        line(
            "A34",
            "Every card, chart and list updates live — never type on the Dashboard.",
        ),
        line(
            "A35",
            f"Top {top_n} Most Valuable ranks by Total Current Value; the pie breaks items down by",
        ),
        line(
            "A36",
            "category and the bar chart by location; Recently Added lists your newest rows.",
        ),
        heading("A38", "7. CATEGORIES & LOCATIONS"),
        line(
            "A39",
            "Those tabs hold one name per row. Add a row and the matching Inventory dropdown",
        ),
        line(
            "A40",
            "picks it up automatically — no setup needed. The Items columns count themselves.",
        ),
        heading("A42", "8. PROTECTION"),
        line("A43", "Formula cells are locked against accidental edits (no password)."),
        line("A44", "Review → Unprotect Sheet removes the lock if you ever need to."),
    ]


# ── Standard pattern entry points (used by the dynamic registry) ─────

coerce_params = coerce_inventory_params
build_spec = build_inventory_spec
