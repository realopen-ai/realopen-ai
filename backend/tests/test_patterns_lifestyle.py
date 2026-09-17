"""
Tests for the lifestyle pattern batch — content_calendar, meal_planner
and workout_log (app/services/patterns/), plus their routing integration
in services/excel_gen.py.

Same conventions as tests/test_excel_patterns.py:
  • param coercion (quoted numbers, alias keys, null → documented
    defaults like month_start / week_start computed in coerce)
  • builder layout math — every formula references the row the
    converter will actually render (title rows included!)
  • independent math verification: volume, Epley estimated 1RM and
    shopping-list to-buy quantities recomputed in pure Python from the
    request params and pinned against the emitted cell values/formulas
  • routing: classifier → template with the AI path skipped
"""

import json
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import excel_gen as eg  # noqa: E402
from app.services import patterns as ep  # noqa: E402
from openpyxl import load_workbook  # noqa: E402

# ── shared fixtures ───────────────────────────────────────────────────

CC_PARAMS = {
    "calendar_name": "Launch Content",
    "month_start": "2026-03",
    "platforms": ["Instagram", "Blog"],
    "posts": [
        {"title": "Announcement", "platform": "Blog", "date": "2025-12-01"},
        {
            "title": "Teaser reel",
            "platform": "Instagram",
            "date": "2026-03-05",
            "topic": "Launch",
            "status": "Idea",
            "owner": "Alex",
        },
        {
            "title": "Founder story",
            "platform": "Blog",
            "date": "2026-03-08",
            "topic": "Story",
            "status": "Drafting",
        },
        {
            "title": "FAQ post",
            "platform": "Instagram",
            "date": "2026-03-12",
            "topic": "FAQ",
            "status": "Scheduled",
            "owner": "Sam",
        },
        {
            "title": "Behind the scenes",
            "platform": "TikTok",
            "date": "2026-03-15",
            "status": "published",
        },
    ],
}

MEAL_PARAMS = {
    "week_start": "2026-03-11",  # a Wednesday → snaps to Monday 2026-03-09
    "meals": [
        {"day": "Monday", "meal": "Breakfast", "dish": "Oats with berries"},
        {"day": "Monday", "meal": "Dinner", "dish": "Chicken curry"},
        {"day": "Monday", "meal": "Dinner", "dish": "Rice"},
        {"day": "Tuesday", "meal": "Lunch", "dish": "Lentil soup"},
        {"day": "Wed", "meal": "breakfast", "dish": "Scrambled eggs"},
        {"day": "saturday", "meal": "Snack", "dish": "Greek yogurt"},
        {"day": "Tuesday", "meal": "Lunch", "dish": "Bread"},
    ],
    "shopping_list": [
        {
            "item": "Oats",
            "quantity": 500,
            "unit": "g",
            "category": "Pantry",
            "have_at_home": 200,
        },
        {"item": "Chicken breast", "quantity": 2, "unit": "pcs", "have_at_home": 0},
        {
            "item": "Greek yogurt",
            "quantity": "4",
            "unit": "pack",
            "category": "Dairy",
            "have_at_home": 6,
        },
        {"item": "Berries", "category": "Produce"},
        {"item": "Rice"},
    ],
}

WORKOUT_PARAMS = {
    "log_name": "Gym Log",
    "sessions": [
        {
            "date": "2026-03-09",
            "exercise": "Bench Press",
            "sets": 4,
            "reps": 8,
            "weight": 80,
        },
        {
            "date": "2026-03-09",
            "exercise": "Squat",
            "sets": 5,
            "reps": 5,
            "weight": 100,
        },
        {
            "date": "2026-03-11",
            "exercise": "Bench Press",
            "sets": 3,
            "reps": 6,
            "weight": 85,
        },
        {
            "date": "2026-03-11",
            "exercise": "Deadlift",
            "sets": 1,
            "reps": 5,
            "weight": 140,
        },
        {"date": "2026-03-13", "exercise": "Bench Press"},  # defaults 1/1/0
    ],
}


def _classify_response(payload):
    """Classifier LLM stub returning a fixed routing payload."""

    async def fake_llm(messages, model=None):
        assert "route spreadsheet requests" in messages[0]["content"]
        return json.dumps(payload)

    return fake_llm


# ── content_calendar: builder layout math is the contract ────────────


class TestContentCalendarBuilder:
    def setup_method(self):
        self.spec = ep.build_content_calendar_spec(dict(CC_PARAMS))
        self.norm = eg._normalize_spec(self.spec)

    def test_spec_validates_clean(self):
        errors, warnings = eg.validate_workbook_spec(self.spec)
        assert errors == []
        assert not [w for w in warnings if "above the table" in w]

    def test_geometry_and_formula_lattice(self):
        posts = self.norm["sheets"][0]
        assert posts["name"] == "Posts"
        table = posts["tables"][0]
        # 5 posts padded to 8 rows: header 4, data 5..12
        assert table["start_cell"] == "A4"
        assert len(table["rows"]) == 8
        # sorted by date: the 2025 announcement first, undated last
        assert table["rows"][0][0] == date(2025, 12, 1)
        assert table["rows"][0][1] == "Announcement"
        # lowercase "published" canonicalized for the CF/Dropdown match
        assert table["rows"][4][4] == "Published"
        # padding rows stay blank but inside the formula band
        assert table["rows"][5] == [None] * 6

        # dropdowns cover the whole padded band
        ranges = {dv["range"]: dv for dv in posts["data_validation"]}
        assert set(ranges["C5:C12"]["values"]) == {"Instagram", "Blog", "TikTok"}
        assert ranges["E5:E12"]["values"] == [
            "Idea",
            "Drafting",
            "Scheduled",
            "Published",
        ]

        # conditional formats: overdue red over the whole row band
        # first (higher priority), then Published green / Scheduled
        # amber on the status column
        cf_overdue, cf_status = posts["conditional_formats"]
        assert cf_overdue["range"] == "A5:F12"
        assert cf_overdue["rules"][0]["value"] == (
            'AND($A5<>"",$A5<TODAY(),$E5<>"Published")'
        )
        assert cf_status["range"] == "E5:E12"
        assert cf_status["rules"][0]["value"] == "Published"
        assert cf_status["rules"][1]["value"] == "Scheduled"

    def test_summary_formula_lattice(self):
        summary = self.norm["sheets"][1]
        blocks = {b["cell"]: b["text"] for b in summary["text_blocks"]}
        # every KPI pins to the exact rendered Posts rows 5..12
        assert blocks["B4"] == "=COUNTA(Posts!$B$5:$B$12)"
        assert blocks["B5"] == '=COUNTIF(Posts!$E$5:$E$12,"Published")'
        assert blocks["B6"] == '=COUNTIF(Posts!$E$5:$E$12,"Scheduled")'
        assert blocks["B7"] == (
            '=COUNTIFS(Posts!$A$5:$A$12,">="&DATE(2026,3,1),'
            'Posts!$A$5:$A$12,"<"&DATE(2026,4,1))'
        )
        assert blocks["B8"] == (
            '=COUNTIFS(Posts!$A$5:$A$12,">="&TODAY(),'
            'Posts!$A$5:$A$12,"<="&TODAY()+7)'
        )

        # platform table (3 platforms after the union with post
        # platforms): title 10, header 11, data 12..14, total 15
        plat = summary["tables"][0]
        assert plat["start_cell"] == "A10"
        assert plat["title"] == "Posts by Platform"
        assert plat["rows"][0] == ["Instagram", "=COUNTIF(Posts!$C$5:$C$12,$A12)"]
        assert plat["rows"][1] == ["Blog", "=COUNTIF(Posts!$C$5:$C$12,$A13)"]
        assert plat["rows"][2] == ["TikTok", "=COUNTIF(Posts!$C$5:$C$12,$A14)"]
        assert plat["total_row"] == ["Total", "=SUM(B12:B14)"]

        # status table: title 17, header 18, data 19..22, total 23
        status = summary["tables"][1]
        assert status["start_cell"] == "A17"
        assert [r[0] for r in status["rows"]] == [
            "Idea",
            "Drafting",
            "Scheduled",
            "Published",
        ]
        assert status["rows"][0][1] == "=COUNTIF(Posts!$E$5:$E$12,$A19)"
        assert status["total_row"] == ["Total", "=SUM(B19:B22)"]

        # the bar chart reads the platform table's data rows only
        chart = summary["charts"][0]
        assert chart["type"] == "bar"
        assert chart["categories_range"] == "Summary!$A$12:$A$14"
        assert chart["series"][0]["values_range"] == "Summary!$B$12:$B$14"

    def test_math_verified_independently(self):
        """Recompute the summary counts in pure Python from the params."""
        platform_counts = Counter(
            p.get("platform") for p in CC_PARAMS["posts"] if p.get("platform")
        )
        assert platform_counts == {"Blog": 2, "Instagram": 2, "TikTok": 1}
        # platforms param wins + union with post platforms
        plat_table = self.norm["sheets"][1]["tables"][0]
        assert [r[0] for r in plat_table["rows"]] == ["Instagram", "Blog", "TikTok"]

        status_counts = Counter(
            (p.get("status") or "Idea").capitalize() for p in CC_PARAMS["posts"]
        )
        assert status_counts == {
            "Idea": 2,
            "Drafting": 1,
            "Scheduled": 1,
            "Published": 1,
        }
        this_month = sum(
            1
            for p in CC_PARAMS["posts"]
            if str(p.get("date", "")).startswith("2026-03")
        )
        assert this_month == 4  # the 2025-12-01 announcement is excluded

    def test_no_platforms_omits_platform_table(self):
        spec = ep.build_content_calendar_spec(
            {
                "posts": [
                    {"title": "Only post", "date": "2026-04-01"},
                    {
                        "title": "Second post",
                        "date": "2026-04-02",
                        "status": "Scheduled",
                    },
                ]
            }
        )
        errors, _ = eg.validate_workbook_spec(spec)
        assert errors == []
        norm = eg._normalize_spec(spec)
        summary = norm["sheets"][1]
        # platform table + chart omitted; the status table remains
        assert len(summary["tables"]) == 1
        assert "charts" not in summary
        assert summary["tables"][0]["title"] == "Posts by Status"

    def test_built_workbook(self, tmp_path):
        out = tmp_path / "content.xlsx"
        eg._build_xlsx(self.norm, out)
        wb = load_workbook(out)
        assert wb.sheetnames == ["Posts", "Summary"]
        ws = wb["Posts"]
        assert ws.freeze_panes == "A5"
        assert ws.auto_filter.ref == "A4:F12"
        assert ws["A5"].value.date() == date(2025, 12, 1)
        assert ws["A5"].number_format == "yyyy-mm-dd"
        assert ws["E9"].value == "Published"
        assert len(ws.data_validations.dataValidation) == 2
        dv_formulas = {dv.formula1 for dv in ws.data_validations.dataValidation}
        assert dv_formulas == {
            '"Instagram,Blog,TikTok"',
            '"Idea,Drafting,Scheduled,Published"',
        }
        assert len(list(ws.conditional_formatting)) == 2
        summary = wb["Summary"]
        assert summary["B4"].value == "=COUNTA(Posts!$B$5:$B$12)"
        assert len(summary._charts) == 1

    def test_heal_never_fires_on_template(self):
        before = [r for t in self.norm["sheets"][0]["tables"] for r in t["rows"]]
        eg._heal_off_by_one_formula_rows(self.norm)
        after = [r for t in self.norm["sheets"][0]["tables"] for r in t["rows"]]
        assert before == after


# ── content_calendar: param coercion ─────────────────────────────────


class TestContentCalendarCoercion:
    def test_defaults(self):
        p = ep.coerce_content_calendar_params({"posts": [{"title": "Hi"}]})
        assert p["calendar_name"] == "Content Calendar"
        today = date.today()
        assert p["month_start"] == date(today.year, today.month, 1)
        assert p["posts"][0]["status"] == "Idea"
        # platforms come from the posts when not given
        assert p["platforms"] == []

    def test_month_start_forms(self):
        p = ep.coerce_content_calendar_params(
            {"month_start": "2026-03", "posts": [{"title": "Hi"}]}
        )
        assert p["month_start"] == date(2026, 3, 1)
        # a full date collapses to its month
        p = ep.coerce_content_calendar_params(
            {"month_start": "2026-03-25", "posts": [{"title": "Hi"}]}
        )
        assert p["month_start"] == date(2026, 3, 1)
        # unusable values fall back to the current month
        p = ep.coerce_content_calendar_params(
            {"month_start": "next month", "posts": [{"title": "Hi"}]}
        )
        today = date.today()
        assert p["month_start"] == date(today.year, today.month, 1)

    def test_status_canonicalization(self):
        p = ep.coerce_content_calendar_params(
            {
                "posts": [
                    {"title": "A", "status": "SCHEDULED"},
                    {"title": "B", "status": "in review"},
                ]
            }
        )
        assert p["posts"][0]["status"] == "Scheduled"
        assert p["posts"][1]["status"] == "in review"  # unknown words kept

    def test_platforms_union_and_dedup(self):
        p = ep.coerce_content_calendar_params(
            {
                "platforms": ["Instagram", "instagram", "Blog"],
                "posts": [
                    {"title": "A", "platform": "Blog"},
                    {"title": "B", "platform": "Newsletter"},
                ],
            }
        )
        # case-insensitive dedup (first casing wins) + union with posts
        assert p["platforms"] == ["Instagram", "Blog", "Newsletter"]

    def test_posts_sorted_and_capped(self):
        posts = [
            {"title": "Undated B"},
            {"title": "Dated", "date": "2026-01-02"},
            {"title": "Undated A"},
            {"title": "Earlier", "date": "2026-01-01"},
        ]
        p = ep.coerce_content_calendar_params({"posts": posts})
        assert [x["title"] for x in p["posts"]] == [
            "Earlier",
            "Dated",
            "Undated A",
            "Undated B",
        ]

    def test_missing_posts_raise(self):
        with pytest.raises(ValueError):
            ep.coerce_content_calendar_params({})
        with pytest.raises(ValueError):
            ep.coerce_content_calendar_params({"posts": []})
        with pytest.raises(ValueError):
            ep.coerce_content_calendar_params({"posts": "none"})
        with pytest.raises(ValueError):
            ep.coerce_content_calendar_params(None)

    def test_quoted_and_alias_keys(self):
        p = ep.coerce_content_calendar_params(
            {
                "name": "Spring Plan",
                "start_month": "2026-04",
                "platform_list": ["Instagram"],
                "post_list": [
                    {
                        "post": "Hello",
                        "channel": "Instagram",
                        "publish_date": "2026-04-01",
                        "state": "drafting",
                    }
                ],
            }
        )
        assert p["calendar_name"] == "Spring Plan"
        assert p["month_start"] == date(2026, 4, 1)
        assert p["platforms"] == ["Instagram"]
        assert p["posts"][0]["title"] == "Hello"
        assert p["posts"][0]["platform"] == "Instagram"
        assert p["posts"][0]["date"] == "2026-04-01"
        assert p["posts"][0]["status"] == "Drafting"


# ── meal_planner: builder layout math is the contract ────────────────


class TestMealPlannerBuilder:
    def setup_method(self):
        self.spec = ep.build_meal_planner_spec(dict(MEAL_PARAMS))
        self.norm = eg._normalize_spec(self.spec)

    def test_spec_validates_clean(self):
        errors, warnings = eg.validate_workbook_spec(self.spec)
        assert errors == []
        assert not [w for w in warnings if "above the table" in w]

    def test_geometry_and_grid(self):
        plan = self.norm["sheets"][0]
        assert plan["name"] == "Plan"
        table = plan["tables"][0]
        # header 4, Monday..Sunday rows 5..11, total row 12
        assert table["start_cell"] == "A4"
        assert len(table["rows"]) == 7
        assert [r[0] for r in table["rows"]] == [
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ]
        # Wednesday week_start snapped back to its Monday
        assert table["rows"][0][1] == date(2026, 3, 9)
        assert table["rows"][6][1] == date(2026, 3, 15)
        # grid cells: day aliases placed, two dinners joined with ", "
        assert table["rows"][0][2] == "Oats with berries"
        assert table["rows"][0][4] == "Chicken curry, Rice"
        assert table["rows"][1][3] == "Lentil soup, Bread"
        assert table["rows"][2][2] == "Scrambled eggs"
        assert table["rows"][5][5] == "Greek yogurt"
        # unplanned slots stay empty
        assert table["rows"][3][2] is None

        # coverage: per-day planned count formulas over the row band
        assert table["rows"][0][6] == '=SUMPRODUCT(--(C5:F5<>""))'
        assert table["rows"][6][6] == '=SUMPRODUCT(--(C11:F11<>""))'
        # total row: per-meal-type counts + the day-count sum
        assert table["total_row"][0] == "Total"
        assert table["total_row"][2] == '=SUMPRODUCT(--(C5:C11<>""))'
        assert table["total_row"][3] == '=SUMPRODUCT(--(D5:D11<>""))'
        assert table["total_row"][4] == '=SUMPRODUCT(--(E5:E11<>""))'
        assert table["total_row"][5] == '=SUMPRODUCT(--(F5:F11<>""))'
        assert table["total_row"][6] == "=SUM(G5:G11)"

        # fully-planned day turns green
        cf = plan["conditional_formats"][0]
        assert cf["range"] == "G5:G11"
        assert cf["rules"][0]["value"] == 4

        # chart reads the day names + live planned counts
        chart = plan["charts"][0]
        assert chart["type"] == "bar"
        assert chart["categories_range"] == "Plan!$A$5:$A$11"
        assert chart["series"][0]["values_range"] == "Plan!$G$5:$G$11"

    def test_shopping_list_lattice(self):
        shop = self.norm["sheets"][1]
        assert shop["name"] == "Shopping List"
        table = shop["tables"][0]
        # 5 items padded to 8 rows: header 4, data 5..12
        assert table["start_cell"] == "A4"
        assert len(table["rows"]) == 8
        # category grouping via sorted emission
        assert [r[0] for r in table["rows"][:5]] == [
            "Greek yogurt",
            "Chicken breast",
            "Rice",
            "Oats",
            "Berries",
        ]
        assert [r[3] for r in table["rows"][:5]] == [
            "Dairy",
            "Other",
            "Other",
            "Pantry",
            "Produce",
        ]
        # quoted "4" → 4.0; unstated quantity → blank; have null → 0
        assert table["rows"][0][1] == 4.0
        assert table["rows"][1][1] == 2.0
        assert table["rows"][2][1] is None
        assert table["rows"][1][4] == 0.0
        # unit defaults + normalization
        assert [r[2] for r in table["rows"][:5]] == ["pack", "pcs", "pcs", "g", "pcs"]
        # To Buy = MAX(0, quantity − have), blank until quantity exists
        assert table["rows"][0][5] == '=IF($B5="","",MAX(0,$B5-$E5))'
        assert table["rows"][3][5] == '=IF($B8="","",MAX(0,$B8-$E8))'
        assert table["rows"][7][5] == '=IF($B12="","",MAX(0,$B12-$E12))'
        # quantity formats on the numeric columns
        assert table["number_formats"]["B"] == "#,##0.##"
        assert table["number_formats"]["F"] == "#,##0.##"

        # unit dropdown over the whole padded band
        dv = shop["data_validation"][0]
        assert dv["range"] == "C5:C12"
        assert dv["values"] == ["g", "kg", "ml", "L", "pcs", "pack"]

        # to-buy highlighting + live counters below the table
        assert shop["conditional_formats"][0]["range"] == "F5:F12"
        blocks = {b["cell"]: b["text"] for b in shop["text_blocks"]}
        assert blocks["B14"] == "=COUNTA($A$5:$A$12)"
        assert blocks["B15"] == '=COUNTIF($F$5:$F$12,">0")'

    def test_math_verified_independently(self):
        """To-buy quantities recomputed in pure Python from the params."""
        expected = {}
        for item in MEAL_PARAMS["shopping_list"]:
            qty = item.get("quantity")
            have = item.get("have_at_home") or 0
            expected[item["item"]] = (
                None if qty is None else max(0.0, float(qty) - float(have))
            )
        assert expected == {
            "Oats": 300.0,
            "Chicken breast": 2.0,
            "Greek yogurt": 0.0,  # MAX(0, 4 − 6) floors at zero
            "Berries": None,
            "Rice": None,
        }
        # the workbook pins exactly those inputs next to the MAX formula
        table = self.norm["sheets"][1]["tables"][0]
        by_item = {r[0]: r for r in table["rows"] if r[0]}
        for item, want in expected.items():
            row = by_item[item]
            qty, have = row[1], row[4]
            assert (None if qty is None else max(0.0, qty - have)) == want

        # grid coverage recomputed: Monday 2, Tuesday 1, Wednesday 1,
        # Saturday 1 — everything else empty
        grid_expected = [2, 1, 1, 0, 0, 1, 0]
        grid = self.norm["sheets"][0]["tables"][0]["rows"]
        computed = [sum(1 for c in r[2:6] if c is not None) for r in grid]
        assert computed == grid_expected
        assert sum(grid_expected) == 5

    def test_empty_shopping_list_still_builds(self):
        spec = ep.build_meal_planner_spec(
            {"meals": [{"day": "Monday", "meal": "Dinner", "dish": "Soup"}]}
        )
        errors, _ = eg.validate_workbook_spec(spec)
        assert errors == []
        norm = eg._normalize_spec(spec)
        table = norm["sheets"][1]["tables"][0]
        assert len(table["rows"]) == 8  # fully blank editable list
        assert table["rows"][0][5] == '=IF($B5="","",MAX(0,$B5-$E5))'

    def test_built_workbook(self, tmp_path):
        out = tmp_path / "meals.xlsx"
        eg._build_xlsx(self.norm, out)
        wb = load_workbook(out)
        assert wb.sheetnames == ["Plan", "Shopping List"]
        plan = wb["Plan"]
        assert plan.freeze_panes == "C5"
        assert plan["A5"].value == "Monday"
        assert plan["B5"].value.date() == date(2026, 3, 9)
        assert plan["B5"].number_format == "yyyy-mm-dd"
        assert plan["E5"].value == "Chicken curry, Rice"
        assert plan["G5"].value == '=SUMPRODUCT(--(C5:F5<>""))'
        assert len(plan._charts) == 1
        shop = wb["Shopping List"]
        assert shop.freeze_panes == "A5"
        assert shop["F8"].value == '=IF($B8="","",MAX(0,$B8-$E8))'
        assert shop["B8"].number_format == "#,##0.##"
        assert len(shop.data_validations.dataValidation) == 1
        assert (
            shop.data_validations.dataValidation[0].formula1 == '"g,kg,ml,L,pcs,pack"'
        )
        assert len(list(shop.conditional_formatting)) == 1

    def test_heal_never_fires_on_template(self):
        before = [r for t in self.norm["sheets"][0]["tables"] for r in t["rows"]]
        eg._heal_off_by_one_formula_rows(self.norm)
        after = [r for t in self.norm["sheets"][0]["tables"] for r in t["rows"]]
        assert before == after


# ── meal_planner: param coercion ──────────────────────────────────────


class TestMealPlannerCoercion:
    def test_week_start_defaults_to_next_monday(self):
        p = ep.coerce_meal_planner_params(
            {"meals": [{"day": "Monday", "meal": "Lunch", "dish": "Salad"}]}
        )
        today = date.today()
        expected = today + timedelta(days=(7 - today.weekday()) or 7)
        assert p["week_start"] == expected
        assert p["week_start"].weekday() == 0
        assert p["week_start"] > today

    def test_week_start_snaps_to_monday(self):
        p = ep.coerce_meal_planner_params(
            {
                "week_start": "2026-03-13",  # a Friday
                "meals": [{"day": "Monday", "meal": "Lunch", "dish": "Salad"}],
            }
        )
        assert p["week_start"] == date(2026, 3, 9)
        # already a Monday stays itself
        p = ep.coerce_meal_planner_params(
            {
                "week_start": "2026-03-09",
                "meals": [{"day": "Monday", "meal": "Lunch", "dish": "Salad"}],
            }
        )
        assert p["week_start"] == date(2026, 3, 9)

    def test_day_and_meal_aliases(self):
        p = ep.coerce_meal_planner_params(
            {
                "meals": [
                    {"day": "Thurs", "meal": "supper", "dish": "Pasta"},
                    {"day": "SUN", "meal": "snack", "dish": "Nuts"},
                    {"day": "Brunchday", "meal": "Lunch", "dish": "Skipped"},
                    {"day": "Friday", "meal": "Brunch", "dish": "Skipped"},
                ]
            }
        )
        # Thursday dinner + Sunday snack placed; unknown day/meal skipped
        assert p["grid"][3][2] == "Pasta"
        assert p["grid"][6][3] == "Nuts"
        placed = sum(1 for row in p["grid"] for cell in row if cell)
        assert placed == 2

    def test_shopping_aliases_and_defaults(self):
        p = ep.coerce_meal_planner_params(
            {
                "meals": [{"day": "Monday", "meal": "Lunch", "dish": "Salad"}],
                "groceries": [
                    {
                        "name": "Flour",
                        "qty": "1.5",
                        "uom": "kg",
                        "group": "Baking",
                        "in_stock": "0.5",
                    },
                    {"name": "Milk"},
                ],
            }
        )
        flour, milk = p["items"]
        assert flour == {
            "item": "Flour",
            "quantity": 1.5,
            "unit": "kg",
            "category": "Baking",
            "have": 0.5,
        }
        assert milk["quantity"] is None
        assert milk["unit"] == "pcs"
        assert milk["category"] == "Other"
        assert milk["have"] == 0.0

    def test_negative_quantities_clamped(self):
        p = ep.coerce_meal_planner_params(
            {
                "meals": [{"day": "Monday", "meal": "Lunch", "dish": "Salad"}],
                "shopping_list": [{"item": "X", "quantity": -5, "have_at_home": -2}],
            }
        )
        assert p["items"][0]["quantity"] == 0.0
        assert p["items"][0]["have"] == 0.0

    def test_missing_meals_raise(self):
        with pytest.raises(ValueError):
            ep.coerce_meal_planner_params({})
        with pytest.raises(ValueError):
            ep.coerce_meal_planner_params({"meals": []})
        with pytest.raises(ValueError):
            ep.coerce_meal_planner_params({"meals": [{"day": "Monday"}]})  # no dish
        with pytest.raises(ValueError):
            ep.coerce_meal_planner_params({"meals": "none"})
        with pytest.raises(ValueError):
            ep.coerce_meal_planner_params(None)

    def test_notes_passthrough(self):
        p = ep.coerce_meal_planner_params(
            {
                "meals": [{"day": "Monday", "meal": "Lunch", "dish": "Salad"}],
                "notes": "  Family of four.  ",
            }
        )
        assert p["notes"] == "Family of four."


# ── workout_log: builder layout math is the contract ─────────────────


class TestWorkoutLogBuilder:
    def setup_method(self):
        self.spec = ep.build_workout_log_spec(dict(WORKOUT_PARAMS))
        self.norm = eg._normalize_spec(self.spec)

    def test_spec_validates_clean(self):
        errors, warnings = eg.validate_workbook_spec(self.spec)
        assert errors == []
        assert not [w for w in warnings if "above the table" in w]

    def test_geometry_and_formula_lattice(self):
        log = self.norm["sheets"][0]
        assert log["name"] == "Log"
        table = log["tables"][0]
        # 5 sessions: header 4, data 5..9, total row 10
        assert table["start_cell"] == "A4"
        assert len(table["rows"]) == 5
        # sorted by (date, exercise): Deadlift after Bench Press
        assert [r[1] for r in table["rows"]] == [
            "Bench Press",
            "Squat",
            "Bench Press",
            "Deadlift",
            "Bench Press",
        ]
        # volume & Epley formulas pin to each rendered row
        for i, row in enumerate(table["rows"]):
            r = 5 + i
            assert row[5] == "=C{r}*D{r}*E{r}".format(r=r)
            assert row[6] == "=E{r}*(1+D{r}/30)".format(r=r)
        # defaults for the entry without numbers
        assert table["rows"][4][2] == 1
        assert table["rows"][4][3] == 1
        assert table["rows"][4][4] == 0.0
        # totals: sets / reps / volume; weight & e1RM stay blank
        assert table["total_row"] == [
            "Total",
            None,
            "=SUM(C5:C9)",
            "=SUM(D5:D9)",
            None,
            "=SUM(F5:F9)",
            None,
        ]
        # number formats: one-decimal weight/e1RM, grouped volume
        assert table["number_formats"]["E"] == "0.0"
        assert table["number_formats"]["F"] == "#,##0.##"
        assert table["number_formats"]["G"] == "0.0"

        # PR highlight on the e1RM column
        cf = log["conditional_formats"][0]
        assert cf["range"] == "G5:G9"
        assert cf["rules"][0]["value"] == (
            "AND($G5>0,$G5>=SUMPRODUCT(MAX(($B$5:$B$9=$B5)*$G$5:$G$9)))"
        )

    def test_summary_and_calc_lattice(self):
        summary = self.norm["sheets"][1]
        assert summary["name"] == "Summary"
        table = summary["tables"][0]
        # title 4, header 5, data 6..9 (4 distinct exercises), total 10
        assert table["start_cell"] == "A4"
        assert table["title"] == "Per-Exercise Summary"
        assert [r[0] for r in table["rows"]] == [
            "Bench Press",
            "Squat",
            "Deadlift",
        ]
        # first-appearance order preserved (Bench, Squat, Deadlift)
        bench = table["rows"][0]
        assert bench[1] == "=COUNTIF(Log!$B$5:$B$9,$A6)"
        assert bench[2] == "=SUMIF(Log!$B$5:$B$9,$A6,Log!$C$5:$C$9)"
        assert bench[3] == "=SUMIF(Log!$B$5:$B$9,$A6,Log!$F$5:$F$9)"
        assert bench[4] == ("=SUMPRODUCT(MAX((Log!$B$5:$B$9=$A6)*Log!$G$5:$G$9))")
        assert bench[5] == (
            '=IF(SUMIF(Log!$B$5:$B$9,$A6,Log!$C$5:$C$9)=0,"n/a",'
            "SUMPRODUCT((Log!$B$5:$B$9=$A6)*Log!$C$5:$C$9*Log!$E$5:$E$9)"
            "/SUMIF(Log!$B$5:$B$9,$A6,Log!$C$5:$C$9))"
        )
        assert table["total_row"] == [
            "Total",
            "=SUM(B6:B8)",
            "=SUM(C6:C8)",
            "=SUM(D6:D8)",
            None,
            None,
        ]

        # hidden Calc sheet: one row per distinct date, SUMIF by date
        calc = self.norm["sheets"][2]
        assert calc["name"] == "Calc"
        assert calc["hidden"] is True
        calc_table = calc["tables"][0]
        assert calc_table["start_cell"] == "A3"
        assert [str(r[0]) for r in calc_table["rows"]] == [
            "2026-03-09",
            "2026-03-11",
            "2026-03-13",
        ]
        assert calc_table["rows"][0][1] == ("=SUMIF(Log!$A$5:$A$9,$A4,Log!$F$5:$F$9)")
        assert calc_table["rows"][2][1] == ("=SUMIF(Log!$A$5:$A$9,$A6,Log!$F$5:$F$9)")

        # line chart reads the Calc date/volume lattice
        chart = summary["charts"][0]
        assert chart["type"] == "line"
        assert chart["categories_range"] == "Calc!$A$4:$A$6"
        assert chart["series"][0]["values_range"] == "Calc!$B$4:$B$6"

    def test_math_verified_independently(self):
        """Volume and Epley e1RM recomputed in pure Python."""
        sessions = sorted(
            WORKOUT_PARAMS["sessions"], key=lambda s: (s["date"], s["exercise"])
        )
        expected_volume = []
        expected_e1rm = []
        for s in sessions:
            sets = s.get("sets", 1)
            reps = s.get("reps", 1)
            weight = s.get("weight", 0)
            expected_volume.append(sets * reps * weight)
            expected_e1rm.append(weight * (1 + reps / 30))
        assert expected_volume == [2560, 2500, 1530, 700, 0]
        assert expected_e1rm == pytest.approx(
            [
                80 * (1 + 8 / 30),
                100 * (1 + 5 / 30),
                85 * (1 + 6 / 30),
                140 * (1 + 5 / 30),
                0.0,
            ]
        )

        # the emitted rows carry exactly those inputs next to the
        # product/Epley formulas — so the formulas must evaluate to
        # the Python-verified numbers
        table = self.norm["sheets"][0]["tables"][0]
        for row, vol, e1rm in zip(table["rows"], expected_volume, expected_e1rm):
            assert row[2] * row[3] * row[4] == vol
            assert row[4] * (1 + row[3] / 30) == pytest.approx(e1rm)

        # per-exercise aggregates for Bench Press
        bench = [s for s in sessions if s["exercise"] == "Bench Press"]
        assert len(bench) == 3
        assert sum(s.get("sets", 1) for s in bench) == 8
        assert (
            sum(s.get("sets", 1) * s.get("reps", 1) * s.get("weight", 0) for s in bench)
            == 4090
        )
        assert max(
            s.get("weight", 0) * (1 + s.get("reps", 1) / 30) for s in bench
        ) == pytest.approx(85 * (1 + 6 / 30))
        # avg weight per set = Σ(sets × weight) ÷ Σsets
        assert (
            sum(s.get("sets", 1) * s.get("weight", 0) for s in bench)
            / sum(s.get("sets", 1) for s in bench)
        ) == pytest.approx((4 * 80 + 3 * 85 + 1 * 0) / 8)

    def test_built_workbook(self, tmp_path):
        out = tmp_path / "gym.xlsx"
        eg._build_xlsx(self.norm, out)
        wb = load_workbook(out)
        assert wb.sheetnames == ["Log", "Summary", "Calc"]
        log = wb["Log"]
        assert log.freeze_panes == "A5"
        assert log.auto_filter.ref == "A4:G10"
        assert log["A5"].value.date() == date(2026, 3, 9)
        assert log["A5"].number_format == "yyyy-mm-dd"
        assert log["F5"].value == "=C5*D5*E5"
        assert log["G5"].value == "=E5*(1+D5/30)"
        assert log["G5"].number_format == "0.0"
        assert log["F10"].value == "=SUM(F5:F9)"
        assert len(list(log.conditional_formatting)) == 1
        assert wb["Calc"].sheet_state == "hidden"
        summary = wb["Summary"]
        assert summary["B6"].value == "=COUNTIF(Log!$B$5:$B$9,$A6)"
        assert summary["E6"].value == (
            "=SUMPRODUCT(MAX((Log!$B$5:$B$9=$A6)*Log!$G$5:$G$9))"
        )
        assert len(summary._charts) == 1
        assert wb["Calc"]["B4"].value == "=SUMIF(Log!$A$5:$A$9,$A4,Log!$F$5:$F$9)"

    def test_single_session_workbook(self):
        spec = ep.build_workout_log_spec(
            {"sessions": [{"exercise": "Squat", "sets": 5, "reps": 5, "weight": 100}]}
        )
        errors, _ = eg.validate_workbook_spec(spec)
        assert errors == []
        norm = eg._normalize_spec(spec)
        # defaults: log_name → Workout Log, date → today
        assert norm["filename"] == "workout_log.xlsx"
        table = norm["sheets"][0]["tables"][0]
        assert table["rows"][0][0] == date.today()
        assert table["rows"][0][5] == "=C5*D5*E5"
        # single exercise / single date still yields chart + summary
        assert len(norm["sheets"][1]["charts"]) == 1
        assert norm["sheets"][1]["charts"][0]["categories_range"] == "Calc!$A$4:$A$4"

    def test_heal_never_fires_on_template(self):
        before = [r for t in self.norm["sheets"][0]["tables"] for r in t["rows"]]
        eg._heal_off_by_one_formula_rows(self.norm)
        after = [r for t in self.norm["sheets"][0]["tables"] for r in t["rows"]]
        assert before == after


# ── workout_log: param coercion ───────────────────────────────────────


class TestWorkoutLogCoercion:
    def test_quoted_numbers(self):
        p = ep.coerce_workout_log_params(
            {
                "sessions": [
                    {
                        "date": "2026-03-09",
                        "exercise": "Bench Press",
                        "sets": "4",
                        "reps": "8",
                        "weight": "80",
                    }
                ]
            }
        )
        assert p["sessions"][0]["sets"] == 4
        assert p["sessions"][0]["reps"] == 8
        assert p["sessions"][0]["weight"] == 80.0

    def test_missing_numbers_use_documented_defaults(self):
        p = ep.coerce_workout_log_params(
            {"sessions": [{"exercise": "Pull-Up"}, {"exercise": "Row", "sets": 3}]}
        )
        assert p["sessions"][0]["sets"] == 1
        assert p["sessions"][0]["reps"] == 1
        assert p["sessions"][0]["weight"] == 0.0
        assert p["sessions"][1]["reps"] == 1
        assert p["sessions"][1]["weight"] == 0.0

    def test_missing_date_defaults_to_today(self):
        p = ep.coerce_workout_log_params(
            {"sessions": [{"exercise": "Squat", "sets": 5, "reps": 5, "weight": 100}]}
        )
        assert p["sessions"][0]["date"] == date.today().isoformat()

    def test_log_name_default_and_alias(self):
        p = ep.coerce_workout_log_params({"sessions": [{"exercise": "Squat"}]})
        assert p["log_name"] == "Workout Log"
        p = ep.coerce_workout_log_params(
            {"name": "5x5 Strength", "entries": [{"exercise": "Squat"}]}
        )
        assert p["log_name"] == "5x5 Strength"

    def test_clamps_and_skips(self):
        p = ep.coerce_workout_log_params(
            {
                "sessions": [
                    {"exercise": "Row", "sets": 500, "reps": 0, "weight": -20},
                    "just a string",
                    {"sets": 3, "reps": 5},  # no exercise name → skipped
                ]
            }
        )
        assert len(p["sessions"]) == 1
        assert p["sessions"][0]["sets"] == 99
        assert p["sessions"][0]["reps"] == 1
        assert p["sessions"][0]["weight"] == 0.0

    def test_missing_sessions_raise(self):
        with pytest.raises(ValueError):
            ep.coerce_workout_log_params({})
        with pytest.raises(ValueError):
            ep.coerce_workout_log_params({"sessions": []})
        with pytest.raises(ValueError):
            ep.coerce_workout_log_params({"sessions": "none"})
        with pytest.raises(ValueError):
            ep.coerce_workout_log_params(None)

    def test_filename_from_log_name(self):
        spec = ep.build_workout_log_spec(
            {"log_name": "Push Day A", "sessions": [{"exercise": "Bench Press"}]}
        )
        assert spec["filename"] == "push_day_a.xlsx"


# ── routing: classifier → template, AI path skipped ──────────────────


class TestLifestyleRouting:
    @pytest.mark.asyncio
    async def test_content_calendar_routes_to_template(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            eg,
            "_call_llm",
            _classify_response(
                {
                    "pattern": "content_calendar",
                    "params": {
                        "calendar_name": "Instagram Content",
                        "month_start": "2026-03",
                        "platforms": ["Instagram"],
                        "posts": [
                            {
                                "title": "Teaser reel",
                                "platform": "Instagram",
                                "date": "2026-03-05",
                                "topic": "Launch",
                                "status": "Idea",
                                "owner": "Alex",
                            },
                            {
                                "title": "Founder story",
                                "platform": "Instagram",
                                "date": "2026-03-08",
                                "status": "Scheduled",
                            },
                        ],
                    },
                }
            ),
        )

        async def must_not_run(brief, requirements, model=None):
            raise AssertionError("AI path must not run when pattern matches")

        monkeypatch.setattr(eg, "_generate_workbook_json", must_not_run)
        monkeypatch.setattr(eg, "_get_reports_dir", lambda: tmp_path)
        result = await eg.generate_spreadsheet(
            "content calendar for my Instagram posts this month"
        )
        assert result["pattern"] == "content_calendar"
        assert result["sheet_names"] == ["Posts", "Summary"]
        assert result["chart_count"] == 1
        assert (tmp_path / f"{result['report_id']}.xlsx").exists()
        assert "content_calendar template" in result["summary"]

    @pytest.mark.asyncio
    async def test_meal_planner_routes_to_template(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            eg,
            "_call_llm",
            _classify_response(
                {
                    "pattern": "meal_planner",
                    "params": {
                        "meals": [
                            {"day": "Monday", "meal": "Breakfast", "dish": "Oats"},
                            {
                                "day": "Monday",
                                "meal": "Dinner",
                                "dish": "Chicken curry",
                            },
                            {"day": "Tuesday", "meal": "Lunch", "dish": "Lentil soup"},
                        ],
                        "shopping_list": [
                            {
                                "item": "Oats",
                                "quantity": 500,
                                "unit": "g",
                                "category": "Pantry",
                                "have_at_home": 200,
                            },
                            {"item": "Chicken breast", "quantity": 2, "unit": "pcs"},
                        ],
                    },
                }
            ),
        )

        async def must_not_run(brief, requirements, model=None):
            raise AssertionError("AI path must not run when pattern matches")

        monkeypatch.setattr(eg, "_generate_workbook_json", must_not_run)
        monkeypatch.setattr(eg, "_get_reports_dir", lambda: tmp_path)
        result = await eg.generate_spreadsheet(
            "plan my meals for the week with a shopping list"
        )
        assert result["pattern"] == "meal_planner"
        assert result["sheet_names"] == ["Plan", "Shopping List"]
        assert result["chart_count"] == 1
        assert (tmp_path / f"{result['report_id']}.xlsx").exists()
        assert "meal_planner template" in result["summary"]

    @pytest.mark.asyncio
    async def test_workout_log_routes_to_template(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            eg,
            "_call_llm",
            _classify_response(
                {
                    "pattern": "workout_log",
                    "params": {
                        "log_name": "Gym Log",
                        "sessions": [
                            {
                                "date": "2026-03-09",
                                "exercise": "Bench Press",
                                "sets": 4,
                                "reps": 8,
                                "weight": 80,
                            },
                            {
                                "date": "2026-03-09",
                                "exercise": "Squat",
                                "sets": 5,
                                "reps": 5,
                                "weight": 100,
                            },
                        ],
                    },
                }
            ),
        )

        async def must_not_run(brief, requirements, model=None):
            raise AssertionError("AI path must not run when pattern matches")

        monkeypatch.setattr(eg, "_generate_workbook_json", must_not_run)
        monkeypatch.setattr(eg, "_get_reports_dir", lambda: tmp_path)
        result = await eg.generate_spreadsheet(
            "workout log for bench press 4 sets of 8 at 80 kg and squats"
        )
        assert result["pattern"] == "workout_log"
        assert result["sheet_names"] == ["Log", "Summary", "Calc"]
        assert result["chart_count"] == 1
        assert (tmp_path / f"{result['report_id']}.xlsx").exists()
        assert "workout_log template" in result["summary"]

    def test_gate_regex_matches_lifestyle_briefs(self):
        assert eg._PATTERN_GATE_RE.search("content calendar for my instagram")
        assert eg._PATTERN_GATE_RE.search("plan my meals for the week")
        assert eg._PATTERN_GATE_RE.search("workout log bench press 4 sets of 8")
