"""
Meal planner pattern — weekly meal plan grid + shopping list.

A self-computing menu workbook:

  Plan            Day | Date | Breakfast | Lunch | Dinner | Snack |
                  Meals Planned — rows Monday..Sunday dated from the
                  week's Monday; grid cells hold the dish names from
                  the request (empty when nothing is planned for a
                  slot); Meals Planned counts filled slots per day via
                  SUMPRODUCT, the total row counts per meal type, a
                  fully-planned day turns green and a bar chart shows
                  meals planned per day
  Shopping List   Item | Quantity | Unit | Category | Have at Home |
                  To Buy — items grouped by category (sorted emission),
                  unit dropdown (g / kg / ml / L / pcs / pack), To Buy
                  = MAX(0, Quantity − Have at Home) as a live formula
                  (blank until a quantity is entered), rows still to
                  buy highlighted amber, live item / to-buy counters

CALCULATION SEMANTICS (all LIVE formulas — nothing frozen at build
time):

  meals planned (day)  = SUMPRODUCT(--(row's 4 grid cells <> ""))
  planned (meal type)  = SUMPRODUCT(--(meal column <> ""))
  to buy               = IF(quantity = "", "", MAX(0, qty − have))
  items                = COUNTA(item column)
  items to buy         = COUNTIF(to-buy column, ">0")

TEMPLATE MODE: a request with no specific dishes ("create a meal
planner spreadsheet") builds the BLANK dated Monday-Sunday grid +
empty shopping-list rows — never invents dishes (hard rule), never
refuses for lack of data. week_start null → next Monday (stanza
default); a week_start that is not itself a Monday snaps back to its
week's Monday so the grid's seven dated rows always run Monday..Sunday.

FILL MODE: when the request states guidance instead of dishes (a
  diet goal, foods to eat more of, cuisines, dislikes), the router
  sets "fill": true and excel_gen drafts starter meals + a shopping
  list via prompts/pattern_populator.md BEFORE calling the builder
  — the user gets a populated week, not a blank grid. Drafted params
  flow through coerce_meal_planner_params like any other; a failed
  or invalid draft falls back to the plain extracted params (blank
  template — never worse than no fill).


Every formula reference is computed from the actual layout rows this
module emits, so off-by-N row math is impossible by construction. No
ROUND() anywhere — display rounding is the number format's job.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.services.patterns.utils import (
    QTY_FMT,
    _pick,
    to_number,
    to_iso_date,
)

# Registry key — must match the pattern stanza in
# prompts/pattern_classifier.md.
PATTERN_NAME = "meal_planner"

PATTERN_DESCRIPTION = (
    "Creates a weekly meal planner: a Monday-to-Sunday grid of "
    "Breakfast / Lunch / Dinner / Snack dishes with per-day and "
    "per-meal coverage counts, plus a shopping list with unit "
    "dropdowns and a live To-Buy column (MAX(0, quantity − have at "
    "home)). Use when the user plans meals, a weekly menu or a meal "
    "prep — a request with no dishes still gets a blank dated "
    "planner template. Do not use it for daily habit tracking."
)

# Routing keywords/stems — drive the cheap pre-gate and the classifier
# shortlist (see excel_gen._shortlist_patterns).
PATTERN_KEYWORDS = (
    "meal",
    "menu",
    "meal plan",
    "meal prep",
    "weekly menu",
    "dinner plan",
    "what to eat",
)

# Fillable pattern: guidance-only requests (diet goals, foods to eat
# more of, cuisines…) may draft starter dishes + shopping list via
# prompts/pattern_populator.md before building — see PATTERN_FILLABLE
# in patterns/__init__.py.
PATTERN_FILL = True

MAX_MEALS = 100  # meal entries accepted from the request
MAX_SHOPPING = 120  # shopping-list items accepted
MIN_SHOP_ROWS = 8  # blank editable rows kept on the list
DEFAULT_CATEGORY = "Other"

_DATE_FMT = "yyyy-mm-dd"
_INT_FMT = "0"
_QTY_FMT = QTY_FMT  # "#,##0.##" — integers clean, fractions exact

# Fixed grid layout: rows = weekdays, columns = meal types.
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MEALS = ("Breakfast", "Lunch", "Dinner", "Snack")
UNITS = ("g", "kg", "ml", "L", "pcs", "pack")

# Day words → weekday index (0 = Monday). Prefix matching keeps it
# tolerant of "Mon", "Tues", "Thur"…
_DAY_ALIASES: Tuple[Tuple[str, int], ...] = (
    ("monday", 0),
    ("tuesday", 1),
    ("wednesday", 2),
    ("thursday", 3),
    ("friday", 4),
    ("saturday", 5),
    ("sunday", 6),
)

# Meal words → canonical slot (prefix matching: "break", "lun", "din",
# "supper"…).
_MEAL_ALIASES: Tuple[Tuple[str, str], ...] = (
    ("breakfast", "Breakfast"),
    ("lunch", "Lunch"),
    ("dinner", "Dinner"),
    ("supper", "Dinner"),
    ("snack", "Snack"),
)

# Design tokens (same palette as the converter).
NAVY = "16304F"
STEEL = "1B3A5C"
GOLD = "C9A227"
MUTED = "5C6470"

# Excel's classic Good / Neutral conditional-format palettes.
CF_GREEN_FILL = "C6EFCE"
CF_GREEN_TEXT = "1E4620"
CF_GOLD_FILL = "FFF2CC"
CF_GOLD_TEXT = "7F6000"


# ── Param coercion ────────────────────────────────────────────────────


def _monday_of(d: date) -> date:
    """The Monday of the week containing d (d itself when Monday)."""
    return d - timedelta(days=d.weekday())


def _next_monday(today: Optional[date] = None) -> date:
    """The Monday of NEXT week (never today) — stanza default for a
    missing week_start. (Local twin of utils.next_monday: that helper
    currently raises NameError in the shared module, so this pattern
    keeps its own stdlib-only copy.)"""
    today = today or date.today()
    return today + timedelta(days=(7 - today.weekday()) or 7)


def _canonical_day(raw: Any) -> Optional[int]:
    if not isinstance(raw, str):
        return None
    s = raw.strip().lower()
    if not s:
        return None
    for alias, idx in _DAY_ALIASES:
        if alias.startswith(s) or s.startswith(alias[:3]):
            return idx
    return None


def _canonical_meal(raw: Any) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    s = raw.strip().lower()
    if not s:
        return None
    for alias, canonical in _MEAL_ALIASES:
        if alias.startswith(s) or s.startswith(alias[:3]):
            return canonical
    return None


def _clean_text(raw: Any, limit: int) -> str:
    if not isinstance(raw, str):
        return ""
    return raw.strip()[:limit]


def _clean_unit(raw: Any) -> str:
    """Unit word → one of the dropdown units (default pcs)."""
    if not isinstance(raw, str):
        return "pcs"
    s = raw.strip().lower()
    if not s:
        return "pcs"
    for unit in UNITS:
        if s == unit.lower():
            return unit
    if s in ("piece", "pieces", "item", "items"):
        return "pcs"
    if s in ("gram", "grams", "gr"):
        return "g"
    if s in ("kilo", "kilos", "kilogram", "kilograms"):
        return "kg"
    if s in ("liter", "liters", "litre", "litres"):
        return "L"
    return "pcs"


def coerce_meal_planner_params(params: dict) -> dict:
    """Validate + normalize classifier params; raises ValueError.

    Template mode: empty/missing meals and shopping_list are FINE —
    the builder emits the blank dated grid with blank shopping rows
    (the "create a meal planner spreadsheet" case). ValueError only
    for structurally wrong params (non-array meals/shopping_list).
    """
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    raw_week = _pick(params, "week_start", "start_date", "week")
    week_iso = to_iso_date(raw_week)
    if week_iso is not None:
        y, m, d = (int(x) for x in week_iso.split("-"))
        monday = _monday_of(date(y, m, d))
    else:
        monday = _next_monday()  # null → next Monday (stanza default)

    # meals → the 7×4 grid (multiple dishes in one slot are joined).
    raw_meals = _pick(params, "meals", "meal_list", "dishes")
    grid: List[List[str]] = [["" for _ in MEALS] for _ in DAYS]
    if raw_meals is not None:
        if not isinstance(raw_meals, list):
            raise ValueError("meals must be an array")
        for entry in raw_meals[:MAX_MEALS]:
            if not isinstance(entry, dict):
                continue
            dish = _clean_text(_pick(entry, "dish", "name", "title", "meal_name"), 80)
            if not dish:
                continue
            day_idx = _canonical_day(_pick(entry, "day", "weekday"))
            meal = _canonical_meal(_pick(entry, "meal", "slot", "type"))
            if day_idx is None or meal is None:
                continue  # cannot place it in the grid
            slot = grid[day_idx][MEALS.index(meal)]
            grid[day_idx][MEALS.index(meal)] = f"{slot}, {dish}" if slot else dish
    # Template mode: n_meals == 0 is fine — blank grid, the user
    # types dishes into the dated cells (never invent dishes).

    # shopping_list → sorted, grouped rows.
    raw_items = _pick(params, "shopping_list", "shopping", "groceries")
    items: List[dict] = []
    if raw_items is not None:
        if not isinstance(raw_items, list):
            raise ValueError("shopping_list must be an array")
        for entry in raw_items[:MAX_SHOPPING]:
            if not isinstance(entry, dict):
                continue
            name = _clean_text(_pick(entry, "item", "name", "ingredient"), 60)
            if not name:
                continue
            quantity = to_number(_pick(entry, "quantity", "qty", "amount"))
            if quantity is not None and quantity < 0:
                quantity = 0.0
            have = to_number(_pick(entry, "have_at_home", "have", "in_stock", "stock"))
            if have is None:
                have = 0.0  # stanza: have_at_home null → 0
            elif have < 0:
                have = 0.0
            items.append(
                {
                    "item": name,
                    "quantity": quantity,  # None = unstated → blank cell
                    "unit": _clean_unit(_pick(entry, "unit", "uom")),
                    "category": _clean_text(_pick(entry, "category", "group"), 30)
                    or DEFAULT_CATEGORY,
                    "have": have,
                }
            )
    # Category grouping via sorted emission (category, then item).
    items.sort(key=lambda it: (it["category"].lower(), it["item"].lower()))

    notes = _pick(params, "notes", "note")
    notes = notes.strip()[:1000] if isinstance(notes, str) and notes.strip() else None

    return {
        "week_start": monday,
        "grid": grid,
        "items": items,
        "notes": notes,
    }


# ── Builder ───────────────────────────────────────────────────────────


def build_meal_planner_spec(params: dict) -> dict:
    """Meal planner workbook — every formula code-generated.

    Layout (rows computed here, never guessed by a model):

    Plan sheet:
      row 1      title text block
      row 2      usage hint
      row 4      headers: Day | Date | Breakfast | Lunch | Dinner |
                 Snack | Meals Planned
      rows 5-11  Monday..Sunday dated from the week's Monday;
                 G = SUMPRODUCT over the row's grid cells
      row 12     total row: per-meal-type planned counts + day-count sum
    Shopping List sheet:
      row 1      title text block
      row 2      usage hint
      row 4      headers: Item | Quantity | Unit | Category |
                 Have at Home | To Buy
      rows 5..   one row per item (blank editable rows padded to 8);
                 F = IF(quantity blank, blank, MAX(0, qty − have))
      below      live counters (items, items to buy)
    """
    p = coerce_meal_planner_params(params)
    monday: date = p["week_start"]
    grid: List[List[str]] = p["grid"]
    items: List[dict] = p["items"]
    notes = p["notes"]

    # ── geometry ────────────────────────────────────────────────────
    r0 = 5  # first grid data row
    rN = 4 + len(DAYS)  # last grid data row (11)
    total_row = rN + 1  # noqa: 12
    sunday = monday + timedelta(days=6)

    s0 = 5  # first shopping data row
    n_shop_rows = max(len(items), MIN_SHOP_ROWS)
    sN = 4 + n_shop_rows  # last shopping data row

    # ═════════════════════════════ Plan sheet ═══════════════════════
    plan_rows: List[List[Any]] = []
    for i, day in enumerate(DAYS):
        r = r0 + i
        day_date = monday + timedelta(days=i)
        plan_rows.append(
            [
                day,
                day_date.isoformat(),
                grid[i][0] or None,
                grid[i][1] or None,
                grid[i][2] or None,
                grid[i][3] or None,
                # Meals Planned — filled slots in this day's row
                '=SUMPRODUCT(--(C{r}:F{r}<>""))'.format(r=r),
            ]
        )

    plan_sheet: Dict[str, Any] = {
        "name": "Plan",
        "tab_color": NAVY,
        "freeze_panes": "C5",
        "column_widths": {
            "A": 12,
            "B": 13,
            "C": 24,
            "D": 24,
            "E": 24,
            "F": 22,
            "G": 13,
        },
        "text_blocks": [
            {
                "cell": "A1",
                "text": "Weekly Meal Plan",
                "bold": True,
                "font_size": 14,
                "font_color": NAVY,
            },
            {
                "cell": "A2",
                "text": (
                    "Week of {monday} to {sunday} — type dishes into the "
                    "grid; coverage counts update automatically."
                ).format(
                    monday=monday.isoformat(),
                    sunday=sunday.isoformat(),
                ),
                "italic": True,
                "font_color": MUTED,
            },
        ],
        "tables": [
            {
                "start_cell": "A4",
                "headers": ["Day", "Date"] + list(MEALS) + ["Meals Planned"],
                "rows": plan_rows,
                "number_formats": {"B": _DATE_FMT, "G": _INT_FMT},
                "alignments": {
                    "A": "center",
                    "B": "center",
                    "G": "center",
                },
                "total_row": [
                    "Total",
                    None,
                    # per-meal-type planned counts (the coverage summary)
                    '=SUMPRODUCT(--(C{r0}:C{rN}<>""))'.format(r0=r0, rN=rN),
                    '=SUMPRODUCT(--(D{r0}:D{rN}<>""))'.format(r0=r0, rN=rN),
                    '=SUMPRODUCT(--(E{r0}:E{rN}<>""))'.format(r0=r0, rN=rN),
                    '=SUMPRODUCT(--(F{r0}:F{rN}<>""))'.format(r0=r0, rN=rN),
                    "=SUM(G{r0}:G{rN})".format(r0=r0, rN=rN),
                ],
            }
        ],
        "conditional_formats": [
            {
                # a fully planned day (all 4 slots filled) turns green
                "range": f"G{r0}:G{rN}",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": len(MEALS),
                        "fill": CF_GREEN_FILL,
                        "font_color": CF_GREEN_TEXT,
                        "bold": True,
                        "stop_if_true": False,
                    }
                ],
            }
        ],
        "charts": [
            {
                "type": "bar",
                "title": "Meals Planned per Day",
                "anchor": "I4",
                "width": 14,
                "height": 9,
                "categories_range": f"Plan!$A${r0}:$A${rN}",
                "series": [
                    {
                        "name": "Meals Planned",
                        "values_range": f"Plan!$G${r0}:$G${rN}",
                    }
                ],
                "show_values": True,
                "value_numfmt": _INT_FMT,
            }
        ],
        "notes": notes
        or (
            "Type the dish names straight into the Breakfast / Lunch / "
            "Dinner / Snack cells. Meals Planned counts the filled slots "
            "of each day; the Total row counts each meal type across the "
            "week, and a day with all four slots filled turns green. The "
            "Shopping List sheet works out what to buy from the quantity "
            "you need minus what you already have at home."
        ),
    }

    # ═════════════════════════════ Shopping List sheet ══════════════
    shop_rows: List[List[Any]] = []
    for item in items:
        r = s0 + len(shop_rows)
        shop_rows.append(
            [
                item["item"],
                item["quantity"],  # None → blank cell (unstated)
                item["unit"],
                item["category"],
                item["have"],
                # To Buy — live MAX(0, quantity − have); stays blank
                # until a quantity is entered
                '=IF($B{r}="","",MAX(0,$B{r}-$E{r}))'.format(r=r),
            ]
        )
    while len(shop_rows) < n_shop_rows:
        r = s0 + len(shop_rows)
        shop_rows.append(
            [
                None,
                None,
                None,
                None,
                None,
                '=IF($B{r}="","",MAX(0,$B{r}-$E{r}))'.format(r=r),
            ]
        )

    shop_sheet: Dict[str, Any] = {
        "name": "Shopping List",
        "tab_color": STEEL,
        "freeze_panes": "A5",
        "column_widths": {
            "A": 28,
            "B": 12,
            "C": 10,
            "D": 16,
            "E": 14,
            "F": 12,
        },
        "text_blocks": [
            {
                "cell": "A1",
                "text": "Shopping List",
                "bold": True,
                "font_size": 14,
                "font_color": STEEL,
            },
            {
                "cell": "A2",
                "text": (
                    "Week of {monday} — To Buy = Quantity − Have at Home, "
                    "never below 0. Update Have at Home as you shop."
                ).format(monday=monday.isoformat()),
                "italic": True,
                "font_color": MUTED,
            },
            {
                "cell": f"A{sN + 2}",
                "text": "Items",
            },
            {
                "cell": f"B{sN + 2}",
                "text": "=COUNTA($A${s0}:$A${sN})".format(s0=s0, sN=sN),
                "number_format": _INT_FMT,
            },
            {
                "cell": f"A{sN + 3}",
                "text": "Items to Buy",
                "bold": True,
            },
            {
                "cell": f"B{sN + 3}",
                "text": '=COUNTIF($F${s0}:$F${sN},">0")'.format(s0=s0, sN=sN),
                "number_format": _INT_FMT,
                "bold": True,
            },
        ],
        "tables": [
            {
                "start_cell": "A4",
                "headers": [
                    "Item",
                    "Quantity",
                    "Unit",
                    "Category",
                    "Have at Home",
                    "To Buy",
                ],
                "rows": shop_rows,
                "number_formats": {
                    "B": _QTY_FMT,
                    "E": _QTY_FMT,
                    "F": _QTY_FMT,
                },
                "alignments": {"B": "center", "C": "center", "F": "center"},
                "auto_filter": True,
            }
        ],
        "data_validation": [
            {
                "range": f"C{s0}:C{sN}",
                "values": list(UNITS),
                "allow_blank": True,
                "prompt_title": "Unit",
                "prompt": "g · kg · ml · L · pcs · pack",
                "error_title": "Invalid unit",
                "error": "Pick one of the six units.",
                "error_style": "stop",
            }
        ],
        "conditional_formats": [
            {
                # still needs buying → amber
                "range": f"F{s0}:F{sN}",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "greater_than",
                        "value": 0,
                        "fill": CF_GOLD_FILL,
                        "font_color": CF_GOLD_TEXT,
                        "bold": True,
                        "stop_if_true": False,
                    }
                ],
            }
        ],
        "notes": (
            "Grouped by category. Quantity and Have at Home are numbers "
            "(0.5 works fine); Unit comes from the dropdown. To Buy = "
            "MAX(0, Quantity − Have at Home) and stays blank until a "
            "quantity is entered. Rows with something still to buy are "
            "amber. Add items on the blank rows — copy the To Buy formula "
            "down from the row above."
        ),
    }

    return {
        "filename": "meal_planner_{week}.xlsx".format(week=monday.isoformat()),
        "sheets": [plan_sheet, shop_sheet],
    }


# ── Standard pattern entry points (used by the dynamic registry) ─────

coerce_params = coerce_meal_planner_params
build_spec = build_meal_planner_spec
