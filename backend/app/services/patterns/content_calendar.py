"""
Content calendar pattern — social-media / editorial content plan.

A posting pipeline workbook built from the classifier's post list:

  Posts           Date | Title | Platform | Topic | Status | Owner —
                  one row per planned post (blank rows padded to 8 for
                  easy additions), status dropdown
                  (Idea / Drafting / Scheduled / Published), platform
                  dropdown, panes frozen below the header, auto-filter,
                  Published → green, Scheduled → amber, overdue
                  (date < TODAY() and status ≠ Published) → red
  Summary         live KPIs (total / published / scheduled posts, posts
                  this month via COUNTIFS date bounds, posts in the
                  next 7 days via COUNTIFS with TODAY()), a per-platform
                  COUNTIF table + bar chart and a per-status COUNTIF
                  table — all live formulas, everything updates as the
                  Posts sheet is edited

CALCULATION SEMANTICS (all LIVE formulas — nothing frozen at build
time):

  per-platform count = COUNTIF(Posts platform column, platform cell)
  per-status count   = COUNTIF(Posts status column, status cell)
  posts this month   = COUNTIFS(date >= first of month,
                                date < first of next month)
  upcoming 7 days    = COUNTIFS(date >= TODAY(), date <= TODAY()+7)
  overdue (CF)       = AND(date <> "", date < TODAY(),
                           status <> "Published")

Every formula reference is computed from the actual layout rows this
module emits, so off-by-N row math is impossible by construction. No
ROUND() anywhere — display rounding is the number format's job.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, List, Optional

from app.services.patterns.utils import (
    _pick,
    to_iso_date,
)

# Registry key — must match the pattern stanza in
# prompts/pattern_classifier.md.
PATTERN_NAME = "content_calendar"

PATTERN_DESCRIPTION = (
    "Creates a social-media / editorial content calendar: posts with "
    "platform, date, topic, owner and pipeline status (Idea / Drafting / "
    "Scheduled / Published), a live summary with per-platform and "
    "per-status counts, a posts-by-platform bar chart, upcoming-7-days "
    "count and overdue highlighting. Use when the user plans content, "
    "blog posts or a posting schedule. Do not use it for project task "
    "plans or event planning."
)

# Routing keywords/stems — drive the cheap pre-gate and the classifier
# shortlist (see excel_gen._shortlist_patterns).
PATTERN_KEYWORDS = (
    "content calendar",
    "content plan",
    "content schedule",
    "social media",
    "instagram",
    "editorial",
    "blog post",
    "posting schedule",
    "post calendar",
    "calendar",
)

MAX_POSTS = 200  # hard cap on emitted post rows
MAX_PLATFORMS = 8  # platform summary + dropdown cap
MIN_PAD_ROWS = 8  # blank editable rows kept below the posts
MONTH_WINDOW = 24  # months a requested month_start may sit away
DEFAULT_NAME = "Content Calendar"

_DATE_FMT = "yyyy-mm-dd"
_INT_FMT = "0"

# The canonical pipeline statuses (dropdown + conditional colors).
STATUSES = ("Idea", "Drafting", "Scheduled", "Published")

# Design tokens (same palette as the converter).
NAVY = "16304F"
STEEL = "1B3A5C"
GOLD = "C9A227"
MUTED = "5C6470"

# Excel's classic Good / Neutral / Bad conditional-format palettes.
CF_GREEN_FILL = "C6EFCE"
CF_GREEN_TEXT = "1E4620"
CF_GOLD_FILL = "FFF2CC"
CF_GOLD_TEXT = "7F6000"
CF_RED_FILL = "FFC7CE"
CF_RED_TEXT = "9C0006"

_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

_YYYY_MM_RE = re.compile(r"^(\d{4})-(\d{1,2})$")


# ── Param coercion ────────────────────────────────────────────────────


def _canonical_status(raw: Any) -> str:
    """Status word from the request → canonical form; 'Idea' default."""
    if not isinstance(raw, str) or not raw.strip():
        return "Idea"
    s = raw.strip()[:40]
    for status in STATUSES:
        if s.lower() == status.lower():
            return status
    return s


def _clean_text(raw: Any, limit: int) -> str:
    if not isinstance(raw, str):
        return ""
    return raw.strip()[:limit]


def _clean_platform(raw: Any) -> str:
    """Platform name → dropdown-safe text (no commas/quotes: they break
    Excel list validations) and short enough for the summary table."""
    if not isinstance(raw, str):
        return ""
    return raw.replace(",", " ").replace('"', "'").replace(";", " ").strip()[:30]


def _parse_month_start(raw: Any) -> date:
    """month_start ("YYYY-MM" or a full date) → first-of-month date.

    null / unusable → the current month (documented stanza default).
    """
    today = date.today()
    first: Optional[date] = None
    if isinstance(raw, str):
        m = _YYYY_MM_RE.match(raw.strip())
        if m:
            try:
                first = date(int(m.group(1)), int(m.group(2)), 1)
            except ValueError:
                first = None
        else:
            iso = to_iso_date(raw)
            if iso:
                y, mo, _d = (int(x) for x in iso.split("-"))
                first = date(y, mo, 1)
    if first is None:
        return date(today.year, today.month, 1)
    delta = (first.year - today.year) * 12 + (first.month - today.month)
    if abs(delta) > MONTH_WINDOW:  # crazy far → fall back to this month
        return date(today.year, today.month, 1)
    return first


def _normalize_post(entry: Any) -> Optional[dict]:
    """One posts entry → {title, platform, date, topic, status, owner}."""
    if not isinstance(entry, dict):
        return None
    title = _clean_text(_pick(entry, "title", "post", "headline", "name"), 100)
    if not title:
        return None  # a post without a title cannot be rendered — skip
    platform = _clean_platform(_pick(entry, "platform", "channel", "network"))
    return {
        "title": title,
        "platform": platform,
        "date": to_iso_date(_pick(entry, "date", "post_date", "publish_date")),
        "topic": _clean_text(_pick(entry, "topic", "theme", "category", "pillar"), 60),
        "status": _canonical_status(_pick(entry, "status", "state")),
        "owner": _clean_text(_pick(entry, "owner", "assignee", "author"), 40),
    }


def _coerce_platforms(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    seen: set = set()
    for entry in raw[:MAX_PLATFORMS]:
        name = _clean_platform(entry)
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


def coerce_content_calendar_params(params: dict) -> dict:
    """Validate + normalize classifier params; raises ValueError."""
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    raw_name = _pick(params, "calendar_name", "name", "title")
    calendar_name = (
        _clean_text(raw_name, 80) or DEFAULT_NAME
        if isinstance(raw_name, str)
        else DEFAULT_NAME
    )

    month_start = _parse_month_start(
        _pick(params, "month_start", "month", "start_month")
    )

    raw_posts = _pick(params, "posts", "post_list", "content")
    posts: List[dict] = []
    if raw_posts is not None:
        if not isinstance(raw_posts, list):
            raise ValueError("posts must be an array")
        for entry in raw_posts[:MAX_POSTS]:
            normalized = _normalize_post(entry)
            if normalized is not None:
                posts.append(normalized)
    if not posts:
        raise ValueError("content_calendar needs at least one post")

    # platforms = the request's platform list, else the distinct
    # platforms in posts (stanza) — union so every post is counted;
    # dedup is case-insensitive (first casing wins).
    platforms = _coerce_platforms(
        _pick(params, "platforms", "platform_list", "channels")
    )
    known = {p.lower() for p in platforms}
    for post in posts:
        if post["platform"] and post["platform"].lower() not in known:
            known.add(post["platform"].lower())
            platforms.append(post["platform"])
        if len(platforms) >= MAX_PLATFORMS:
            break
    platforms = platforms[:MAX_PLATFORMS]

    # Deterministic row order: dated posts chronologically, undated last.
    posts.sort(key=lambda p: (p["date"] is None, p["date"] or "", p["title"]))

    notes = _pick(params, "notes", "note")
    notes = notes.strip()[:1000] if isinstance(notes, str) and notes.strip() else None

    return {
        "calendar_name": calendar_name,
        "month_start": month_start,
        "platforms": platforms,
        "posts": posts,
        "notes": notes,
    }


# ── Builder ───────────────────────────────────────────────────────────


def build_content_calendar_spec(params: dict) -> dict:
    """Content calendar workbook — every formula code-generated.

    Layout (rows computed here, never guessed by a model):

    Posts sheet:
      row 1      title text block
      row 2      usage hint
      row 4      headers: Date | Title | Platform | Topic | Status | Owner
      rows 5..   one row per post, padded with blank rows up to 8
    Summary sheet:
      rows 4-8   KPI label/value blocks (total / published / scheduled /
                 this month / upcoming 7 days)
      row 10     Posts by Platform table (title) → header 11, data 12..,
                 total row after
      row P      Posts by Status table → 4 data rows + total row
      chart      bar "Posts by Platform" anchored at D3
    """
    p = coerce_content_calendar_params(params)
    calendar_name: str = p["calendar_name"]
    first: date = p["month_start"]
    platforms: List[str] = p["platforms"]
    posts: List[dict] = p["posts"]
    notes = p["notes"]

    # ── geometry ────────────────────────────────────────────────────
    n_rows = max(len(posts), MIN_PAD_ROWS)
    r0 = 5  # first data row on the Posts sheet
    rN = 4 + n_rows  # last data row on the Posts sheet

    # Next month (for the COUNTIFS upper bound of "posts this month").
    y2, m2 = (first.year + 1, 1) if first.month == 12 else (first.year, first.month + 1)
    month_name = f"{_MONTHS[first.month - 1]} {first.year}"

    # ═════════════════════════════ Posts sheet ══════════════════════
    post_rows: List[List[Any]] = []
    for post in posts:
        post_rows.append(
            [
                post["date"] or None,
                post["title"],
                post["platform"] or None,
                post["topic"] or None,
                post["status"],
                post["owner"] or None,
            ]
        )
    while len(post_rows) < n_rows:  # blank editable slots
        post_rows.append([None] * 6)

    posts_sheet: Dict[str, Any] = {
        "name": "Posts",
        "tab_color": NAVY,
        "freeze_panes": "A5",
        "column_widths": {
            "A": 13,
            "B": 34,
            "C": 15,
            "D": 26,
            "E": 13,
            "F": 15,
        },
        "text_blocks": [
            {
                "cell": "A1",
                "text": calendar_name,
                "bold": True,
                "font_size": 14,
                "font_color": NAVY,
            },
            {
                "cell": "A2",
                "text": (
                    f"Content plan for {month_name} — pick a Status for each "
                    "post; the Summary sheet updates automatically."
                ),
                "italic": True,
                "font_color": MUTED,
            },
        ],
        "tables": [
            {
                "start_cell": "A4",
                "headers": ["Date", "Title", "Platform", "Topic", "Status", "Owner"],
                "rows": post_rows,
                "number_formats": {"A": _DATE_FMT},
                "alignments": {"A": "center", "C": "center", "E": "center"},
                "auto_filter": True,
            }
        ],
        "data_validation": [
            {
                "range": f"E{r0}:E{rN}",
                "values": list(STATUSES),
                "allow_blank": True,
                "prompt_title": "Pipeline status",
                "prompt": "Idea · Drafting · Scheduled · Published",
                "error_title": "Invalid status",
                "error": "Pick one of the four pipeline statuses.",
                "error_style": "stop",
            }
        ],
        # Entry order = priority: overdue red first (whole row), then
        # Published green / Scheduled amber on the status column.
        "conditional_formats": [
            {
                "range": f"A{r0}:F{rN}",
                "rules": [
                    {
                        "type": "formula",
                        "formula": (
                            'AND($A{r0}<>"",$A{r0}<TODAY(),$E{r0}<>"Published")'
                        ).format(r0=r0),
                        "fill": CF_RED_FILL,
                        "font_color": CF_RED_TEXT,
                        "stop_if_true": True,
                    }
                ],
            },
            {
                "range": f"E{r0}:E{rN}",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "Published",
                        "fill": CF_GREEN_FILL,
                        "font_color": CF_GREEN_TEXT,
                        "bold": True,
                        "stop_if_true": True,
                    },
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "Scheduled",
                        "fill": CF_GOLD_FILL,
                        "font_color": CF_GOLD_TEXT,
                        "stop_if_true": True,
                    },
                ],
            },
        ],
        "notes": notes
        or (
            "One row per planned post. Status dropdown: Idea / Drafting / "
            "Scheduled / Published. Published posts turn green, Scheduled "
            "amber, and any post dated before today that is not yet "
            "Published turns red (overdue). The Summary sheet counts "
            "platforms, statuses, this month's posts and the next 7 days "
            "live — add rows above the last post so every formula keeps "
            "covering them."
        ),
    }

    # Platform dropdown only when the platform list fits an inline list.
    platform_dv: List[dict] = []
    if platforms:
        joined = ",".join(platforms)
        if len(joined) <= 250:
            platform_dv.append(
                {
                    "range": f"C{r0}:C{rN}",
                    "values": platforms,
                    "allow_blank": True,
                    "prompt_title": "Platform",
                    "prompt": "Pick the channel this post goes out on.",
                }
            )
    posts_sheet["data_validation"] = platform_dv + posts_sheet["data_validation"]

    # ═════════════════════════════ Summary sheet ═════════════════════
    date_lo = f"DATE({first.year},{first.month},1)"
    date_hi = f"DATE({y2},{m2},1)"

    summary_blocks: List[dict] = [
        {
            "cell": "A1",
            "text": "Summary",
            "bold": True,
            "font_size": 14,
            "font_color": NAVY,
        },
        {
            "cell": "A2",
            "text": f"Live counts for {month_name} — they update as the Posts sheet changes.",
            "italic": True,
            "font_color": MUTED,
        },
        {"cell": "A4", "text": "Total Posts"},
        {
            "cell": "B4",
            "text": "=COUNTA(Posts!$B${r0}:$B${rN})".format(r0=r0, rN=rN),
            "number_format": _INT_FMT,
            "bold": True,
        },
        {"cell": "A5", "text": "Published"},
        {
            "cell": "B5",
            "text": '=COUNTIF(Posts!$E${r0}:$E${rN},"Published")'.format(r0=r0, rN=rN),
            "number_format": _INT_FMT,
        },
        {"cell": "A6", "text": "Scheduled"},
        {
            "cell": "B6",
            "text": '=COUNTIF(Posts!$E${r0}:$E${rN},"Scheduled")'.format(r0=r0, rN=rN),
            "number_format": _INT_FMT,
        },
        {"cell": "A7", "text": f"Posts in {month_name}"},
        {
            "cell": "B7",
            "text": (
                '=COUNTIFS(Posts!$A${r0}:$A${rN},">="&{lo},'
                'Posts!$A${r0}:$A${rN},"<"&{hi})'
            ).format(r0=r0, rN=rN, lo=date_lo, hi=date_hi),
            "number_format": _INT_FMT,
        },
        {"cell": "A8", "text": "Posts in Next 7 Days"},
        {
            "cell": "B8",
            "text": (
                '=COUNTIFS(Posts!$A${r0}:$A${rN},">="&TODAY(),'
                'Posts!$A${r0}:$A${rN},"<="&TODAY()+7)'
            ).format(r0=r0, rN=rN),
            "number_format": _INT_FMT,
        },
    ]

    summary_tables: List[dict] = []
    charts: List[dict] = []
    row = 10

    platform_chart = None
    if platforms:
        plat_first = row + 2
        plat_last = plat_first + len(platforms) - 1
        plat_total = plat_last + 1
        summary_tables.append(
            {
                "start_cell": f"A{row}",
                "title": "Posts by Platform",
                "headers": ["Platform", "Posts"],
                "rows": [
                    [
                        platform,
                        "=COUNTIF(Posts!$C${r0}:$C${rN},$A{r})".format(
                            r0=r0, rN=rN, r=plat_first + i
                        ),
                    ]
                    for i, platform in enumerate(platforms)
                ],
                "number_formats": {"B": _INT_FMT},
                "alignments": {"B": "center"},
                "total_row": [
                    "Total",
                    "=SUM(B{first_row}:B{last_row})".format(
                        first_row=plat_first, last_row=plat_last
                    ),
                ],
            }
        )
        platform_chart = {
            "type": "bar",
            "title": "Posts by Platform",
            "anchor": "D3",
            "width": 14,
            "height": 9,
            "categories_range": f"Summary!$A${plat_first}:$A${plat_last}",
            "series": [
                {
                    "name": "Posts",
                    "values_range": f"Summary!$B${plat_first}:$B${plat_last}",
                }
            ],
            "show_values": True,
            "value_numfmt": _INT_FMT,
        }
        row = plat_total + 2

    status_first = row + 2
    status_last = status_first + len(STATUSES) - 1
    status_total = status_last + 1  # noqa
    summary_tables.append(
        {
            "start_cell": f"A{row}",
            "title": "Posts by Status",
            "headers": ["Status", "Posts"],
            "rows": [
                [
                    status,
                    "=COUNTIF(Posts!$E${r0}:$E${rN},$A{r})".format(
                        r0=r0, rN=rN, r=status_first + i
                    ),
                ]
                for i, status in enumerate(STATUSES)
            ],
            "number_formats": {"B": _INT_FMT},
            "alignments": {"B": "center"},
            "total_row": [
                "Total",
                "=SUM(B{first_row}:B{last_row})".format(
                    first_row=status_first, last_row=status_last
                ),
            ],
        }
    )
    if platform_chart is not None:
        charts.append(platform_chart)

    summary_sheet: Dict[str, Any] = {
        "name": "Summary",
        "tab_color": STEEL,
        "no_freeze": True,
        "column_widths": {"A": 26, "B": 12},
        "text_blocks": summary_blocks,
        "tables": summary_tables,
        "notes": (
            "All counts are live formulas over the Posts sheet. Posts by "
            "Platform / Status use COUNTIF; 'Posts in {month}' uses "
            "COUNTIFS with the month's date bounds and 'Posts in Next 7 "
            "Days' counts dates from TODAY() through TODAY()+7. The bar "
            "chart reads the platform table."
        ).format(month=month_name),
    }
    if charts:
        summary_sheet["charts"] = charts

    fname_month = _MONTHS[first.month - 1][:3].lower()
    return {
        "filename": f"content_calendar_{fname_month}_{first.year}.xlsx",
        "sheets": [posts_sheet, summary_sheet],
    }


# ── Standard pattern entry points (used by the dynamic registry) ─────

coerce_params = coerce_content_calendar_params
build_spec = build_content_calendar_spec
