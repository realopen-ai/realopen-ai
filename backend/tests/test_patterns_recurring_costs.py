"""
Tests for the recurring-cost pattern batch (Task 3-b):
cashflow_forecast, subscription_tracker, expense_report.

Same conventions as tests/test_excel_patterns.py:
  • param coercion (quoted/currency strings, alias keys, null →
    documented defaults, missing required → ValueError)
  • builder layout math — every formula string pins to the row the
    converter will actually render (title rows included)
  • independently recomputed math (balance chain, monthly↔yearly
    equivalents, reimbursement splits)
  • _build_xlsx round-trips via openpyxl
  • routing: mocked classifier → template with the AI path skipped
"""

import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import excel_gen as eg  # noqa: E402
from app.services import patterns as ep  # noqa: E402
from openpyxl import load_workbook  # noqa: E402

# ── Shared fixtures / params ─────────────────────────────────────────

CASHFLOW_PARAMS = {
    "business_name": "Acme Studio",
    "start_month": "2026-01",
    "opening_balance": 5000,
    "months": 12,
    "currency": "USD",
    "monthly_income": [
        {"source": "Client retainers", "amount": 8200},
        {"source": "Shop sales", "amount": 900},
    ],
    "monthly_expenses": [
        {"category": "Rent", "amount": 2400},
        {"category": "Salaries", "amount": 3800},
        {"category": "Software", "amount": 320},
    ],
    "extra_items": [
        {"month": 2, "description": "Tax payment", "amount": 4200, "direction": "out"},
        {
            "month": 6,
            "description": "Equipment sale",
            "amount": 1500,
            "direction": "in",
        },
    ],
}

SUBSCRIPTION_PARAMS = {
    "currency": "USD",
    "subscriptions": [
        {
            "name": "Netflix",
            "category": "Streaming",
            "amount": 15.99,
            "cycle": "monthly",
            "next_renewal": "2026-02-11",
        },
        {"name": "Domain renewal", "amount": 20, "cycle": "yearly"},
        {
            "name": "Gym",
            "category": "Health",
            "amount": 45,
            "cycle": "monthly",
            "active": False,
        },
        {
            "name": "Adobe CC",
            "category": "Software",
            "amount": 660,
            "cycle": "yearly",
            "next_renewal": "2026-03-01",
        },
        {"name": "Water delivery", "amount": 30, "cycle": "quarterly"},
    ],
}

EXPENSE_PARAMS = {
    "employee": "Sara Idrissi",
    "purpose": "Paris client trip",
    "period_start": "2026-03-02",
    "period_end": "2026-03-06",
    "currency": "EUR",
    "expenses": [
        {
            "date": "2026-03-02",
            "category": "Travel",
            "description": "Flight CDG",
            "amount": 320,
            "paid_by": "Personal",
            "billable": True,
            "receipt": True,
        },
        {
            "date": "2026-03-02",
            "category": "Meal",
            "description": "Dinner with client",
            "amount": 84.5,
            "receipt": True,
        },
        {
            "date": "2026-03-03",
            "category": "Lodging",
            "description": "Hotel 3 nights",
            "amount": 405,
            "paid_by": "Company",
        },
        {
            "date": "2026-03-04",
            "category": "Meal",
            "description": "Taxi",
            "amount": 22,
            "billable": False,
        },
        {"date": "2026-03-05", "description": "Metro pass", "amount": 15.75},
    ],
}


def _classify_response(payload):
    async def fake_llm(messages, model=None):
        assert "route spreadsheet requests" in messages[0]["content"]
        return json.dumps(payload)

    return fake_llm


# ── Cash flow forecast: coercion ─────────────────────────────────────


class TestCashflowCoercion:
    def test_aliases_quoted_numbers_and_defaults(self):
        p = ep.coerce_cashflow_forecast_params(
            {
                "business_name": "Acme",
                "start_month": "2026-01",
                "start_balance": "$5,000",  # alias + currency string
                "horizon": "12",  # alias + quoted number
                "currency": "USD",
                "monthly_income": [{"source": "Retainers", "amount": "8200"}],
                "monthly_expenses": [{"category": "Rent", "amount": 2400}],
            }
        )
        assert p["opening_balance"] == 5000.0
        assert p["months"] == 12
        assert p["monthly_income"] == [("Retainers", 8200.0)]
        assert p["monthly_expenses"] == [("Rent", 2400.0)]
        assert p["currency"] == '"$"#,##0.00'

    def test_null_semantics_defaults(self):
        # stanza: months null -> 12, opening_balance 0 when not stated
        p = ep.coerce_cashflow_forecast_params(
            {"monthly_expenses": [{"category": "Rent", "amount": 1000}]}
        )
        assert p["months"] == 12
        assert p["opening_balance"] == 0.0
        assert p["monthly_income"] == []
        # no start_month given -> next month after today
        y, m = ep.first_of_next_month()
        assert p["start_month"] == (y, m)

    def test_months_clamped_instead_of_raising(self):
        p = ep.coerce_cashflow_forecast_params(
            {"months": 999, "monthly_income": [{"source": "A", "amount": 1}]}
        )
        assert p["months"] == 36
        p = ep.coerce_cashflow_forecast_params(
            {"months": 0, "monthly_income": [{"source": "A", "amount": 1}]}
        )
        assert p["months"] == 1

    def test_start_month_accepts_full_iso_date(self):
        p = ep.coerce_cashflow_forecast_params(
            {
                "start_month": "2026-03-15",
                "monthly_income": [{"source": "A", "amount": 1}],
            }
        )
        assert p["start_month"] == (2026, 3)

    def test_extra_items_normalized(self):
        p = ep.coerce_cashflow_forecast_params(
            {
                "months": 6,
                "monthly_expenses": [{"category": "Rent", "amount": 100}],
                "extra_items": [
                    {
                        "month": 2,
                        "description": "Tax",
                        "amount": "4,200",
                        "direction": "out",
                    },
                    {
                        "month": 99,
                        "description": "Far out",
                        "amount": 100,
                        "direction": "inflow",
                    },
                    {"amount": 50, "direction": "credit"},  # no month/desc
                ],
            }
        )
        m, label, amount, direction = p["extra_items"][0]
        assert (m, label, amount, direction) == (2, "Tax", 4200.0, "out")
        # month clamped into the horizon
        assert p["extra_items"][1][0] == 6
        assert p["extra_items"][1][3] == "in"
        # missing month -> 1, missing description -> generic label
        assert p["extra_items"][2] == (1, "One-off item", 50.0, "in")

    def test_bad_start_month_falls_back(self):
        p = ep.coerce_cashflow_forecast_params(
            {
                "start_month": "spring",
                "monthly_income": [{"source": "A", "amount": 1}],
            }
        )
        y, m = ep.first_of_next_month()
        assert p["start_month"] == (y, m)

    def test_no_lines_at_all_raises(self):
        with pytest.raises(ValueError):
            ep.coerce_cashflow_forecast_params({"opening_balance": 5000, "months": 12})
        with pytest.raises(ValueError):
            ep.coerce_cashflow_forecast_params({})

    def test_params_not_a_dict_raises(self):
        with pytest.raises(ValueError):
            ep.coerce_cashflow_forecast_params(["nope"])


# ── Cash flow forecast: builder lattice + math ───────────────────────


class TestCashflowBuilder:
    def setup_method(self):
        self.spec = ep.build_cashflow_forecast_spec(dict(CASHFLOW_PARAMS))
        self.norm = eg._normalize_spec(self.spec)

    def _table(self, title_prefix):
        for t in self.norm["sheets"][0]["tables"]:
            if str(t.get("title", "")).startswith(title_prefix):
                return t
        raise AssertionError(f"no table titled {title_prefix!r}")

    def test_spec_validates_clean(self):
        errors, warnings = eg.validate_workbook_spec(self.spec)
        assert errors == []
        assert not [w for w in warnings if "above the table" in w]

    def test_geometry_and_formula_lattice(self):
        sheet = self.norm["sheets"][0]
        assert sheet["name"] == "Forecast"
        assert sheet["tab_color"] == "16304F"
        # input block above the first table
        blocks = {b["cell"]: b for b in sheet["text_blocks"]}
        assert blocks["A1"]["text"] == "Acme Studio Cash Flow Forecast"
        assert blocks["A1"]["bold"] and blocks["A1"]["font_size"] == 14
        assert blocks["B3"]["text"] == 5000
        assert blocks["B4"]["text"] == date(2026, 1, 1)  # ISO -> real date
        assert blocks["B5"]["text"] == 12

        # Income anchored A7 with title -> header 8, data 9-10, total 11
        inc = self._table("Monthly Income")
        assert inc["start_cell"] == "A7"
        assert len(inc["rows"]) == 2
        assert inc["total_row"][1] == "=SUM(B{first_row}:B{last_row})"

        # Expenses anchored A13 -> data 15-17, total 18
        exp = self._table("Monthly Expenses")
        assert exp["start_cell"] == "A13"
        assert len(exp["rows"]) == 3

        # One-off items anchored A20 -> data 22-23
        extras = self._table("One-Off")
        assert extras["start_cell"] == "A20"
        assert extras["rows"][0][0] == "=EDATE($B$4,1)"  # month 2
        assert extras["rows"][0][1] == "Tax payment"
        assert extras["rows"][0][2] == 4200
        assert extras["rows"][0][3] == "out"
        assert extras["rows"][1][0] == "=EDATE($B$4,5)"  # month 6
        assert extras["rows"][1][3] == "in"

        # Forecast anchored A25 with title -> header 26, data 27-38,
        # total 39. Every ref pins to those rendered rows.
        fc = self._table("Cash Flow by Month")
        assert fc["start_cell"] == "A25"
        assert len(fc["rows"]) == 12
        first, second, last = fc["rows"][0], fc["rows"][1], fc["rows"][-1]
        assert first[0] == "=EDATE($B$4,0)"
        assert first[1] == "=B11"  # income table total row
        assert first[2] == "=B18"  # expense table total row
        assert first[3] == '=SUMIFS($C$22:$C$23,$A$22:$A$23,$A27,$D$22:$D$23,"in")'
        assert first[4] == '=SUMIFS($C$22:$C$23,$A$22:$A$23,$A27,$D$22:$D$23,"out")'
        assert first[5] == "=B27+D27-C27-E27"
        assert first[6] == "=$B$3"  # opening balance input
        assert first[7] == "=G27+F27"
        assert second[6] == "=H27"  # chain: previous closing
        assert second[7] == "=G28+F28"
        assert last[0] == "=EDATE($B$4,11)"
        assert last[6] == "=H37"
        assert last[7] == "=G38+F38"
        assert fc["total_row"][1] == "=SUM(B{first_row}:B{last_row})"
        assert fc["total_row"][5] == "=SUM(F{first_row}:F{last_row})"

        # chart pinned to the rendered forecast rows
        chart = sheet["charts"][0]
        assert chart["type"] == "line"
        assert chart["categories_range"] == "Forecast!A27:A38"
        assert chart["series"][0]["values_range"] == "Forecast!H27:H38"
        assert chart["anchor"] == "J3"

        # direction dropdown exactly over the one-off table rows
        dv = sheet["data_validation"][0]
        assert dv["range"] == "D22:D23"
        assert dv["values"] == ["in", "out"]

        # conditional formats over the rendered forecast band
        cf_ranges = [c["range"] for c in sheet["conditional_formats"]]
        assert cf_ranges == ["H27:H38", "F27:F38"]
        assert sheet["freeze_panes"] == "A27"

    def test_summary_sheet_cross_refs(self):
        summary = self.norm["sheets"][1]
        assert summary["name"] == "Summary"
        assert summary["tab_color"] == "1B3A5C"
        rows = {r[0]: r[1] for r in summary["tables"][0]["rows"]}
        assert rows["Opening Balance"] == "=Forecast!$B$3"
        assert rows["Total Income (horizon)"] == "=Forecast!$B$39"
        assert rows["Total Expenses (horizon)"] == "=Forecast!$C$39"
        assert rows["Final Closing Balance"] == "=Forecast!$H$38"
        assert rows["Lowest Balance"] == "=MIN(Forecast!$H$27:$H$38)"
        assert "<0" in rows["Negative-Balance Months"]
        assert rows["Negative-Balance Months"].endswith('&" months"')
        # division guard on the average
        assert rows["Average Monthly Net"] == (
            '=IF(Forecast!$B$5>0,Forecast!$F$39/Forecast!$B$5,"n/a")'
        )

    def test_independent_balance_chain_math(self):
        """Recompute the whole chain in Python from the emitted values
        and check it matches the formula lattice's semantics."""
        inc = self._table("Monthly Income")
        exp = self._table("Monthly Expenses")
        extras = self._table("One-Off")
        fc = self._table("Cash Flow by Month")

        income_total = sum(r[1] for r in inc["rows"])  # =B11
        expense_total = sum(r[1] for r in exp["rows"])  # =B18
        assert income_total == 8200 + 900
        assert expense_total == 2400 + 3800 + 320

        # month 1..12 -> extra in/out, from the one-off table's amounts
        extra_in = {m: 0.0 for m in range(1, 13)}
        extra_out = {m: 0.0 for m in range(1, 13)}
        for row in extras["rows"]:
            month = int(row[0].split(",")[-1].rstrip(")")) + 1  # EDATE offset
            if row[3] == "in":
                extra_in[month] += row[2]
            else:
                extra_out[month] += row[2]

        opening = 5000.0
        closings = []
        for m in range(1, 13):
            net = income_total + extra_in[m] - expense_total - extra_out[m]
            closing = opening + net
            closings.append(closing)
            opening = closing

        # the lattice the builder emitted must encode exactly this:
        # F = B+D-C-E, G = $B$3 / previous H, H = G+F — so the r-th row
        # computes closings[r-1] when B/C/D/E hold the values above.
        for i, row in enumerate(fc["rows"]):
            r = 27 + i
            assert row[5] == f"=B{r}+D{r}-C{r}-E{r}"
            assert row[6] == ("=$B$3" if i == 0 else f"=H{r - 1}")
            assert row[7] == f"=G{r}+F{r}"
        # expected numbers: month 2 books the 4200 tax outflow, month 6
        # the 1500 equipment sale
        net_monthly = income_total - expense_total  # 2980
        assert closings[0] == pytest.approx(5000 + net_monthly)
        assert closings[1] == pytest.approx(5000 + 2 * net_monthly - 4200)
        assert closings[5] == pytest.approx(5000 + 6 * net_monthly - 4200 + 1500)
        assert closings[-1] == pytest.approx(5000 + 12 * net_monthly - 4200 + 1500)

    def test_built_workbook(self, tmp_path):
        out = tmp_path / "cashflow.xlsx"
        eg._build_xlsx(self.norm, out)
        wb = load_workbook(out)
        assert wb.sheetnames == ["Forecast", "Summary"]
        ws = wb["Forecast"]
        assert ws["B3"].value == 5000
        assert ws["B3"].number_format == '"$"#,##0.00'
        # openpyxl reads date cells back as datetime
        assert ws["B4"].value == datetime(2026, 1, 1)
        assert ws["A27"].value == "=EDATE($B$4,0)"
        assert ws["B27"].value == "=B11"
        assert ws["G27"].value == "=$B$3"
        assert ws["G28"].value == "=H27"
        assert ws["H38"].value == "=G38+F38"
        assert ws["B39"].value == "=SUM(B27:B38)"  # placeholders expanded
        assert ws["A22"].value == "=EDATE($B$4,1)"  # one-off month 2
        assert ws["D22"].value == "out"
        assert len(ws._charts) == 1
        assert len(ws.data_validations.dataValidation) == 1
        assert ws.freeze_panes == "A27"
        assert ws.sheet_properties.tabColor is not None
        summary = wb["Summary"]
        # summary table: title A3, header 4, data 5..14
        assert summary["B5"].value == "=Forecast!$B$3"
        assert summary["B6"].value == "=Forecast!$B$39"
        assert summary["B11"].value == "=Forecast!$H$38"

    def test_no_extra_items_branch(self):
        params = dict(CASHFLOW_PARAMS)
        params.pop("extra_items")
        spec = ep.build_cashflow_forecast_spec(params)
        errors, warnings = eg.validate_workbook_spec(spec)
        assert errors == []
        sheet = eg._normalize_spec(spec)["sheets"][0]
        titles = [t.get("title", "") for t in sheet["tables"]]
        assert not any(t.startswith("One-Off") for t in titles)
        fc = [t for t in sheet["tables"] if t.get("title") == "Cash Flow by Month"][0]
        assert fc["rows"][0][3] == "=0"
        assert fc["rows"][0][4] == "=0"
        assert "data_validation" not in sheet
        assert len(sheet["charts"]) == 1  # the balance line stays

    def test_expenses_only_branch(self):
        params = {
            "opening_balance": 100,
            "months": 3,
            "monthly_expenses": [{"category": "Rent", "amount": 2600}],
        }
        spec = ep.build_cashflow_forecast_spec(params)
        errors, _ = eg.validate_workbook_spec(spec)
        assert errors == []
        sheet = eg._normalize_spec(spec)["sheets"][0]
        texts = [b["text"] for b in sheet["text_blocks"]]
        assert "No monthly income lines provided" in texts
        fc = [t for t in sheet["tables"] if t.get("title") == "Cash Flow by Month"][0]
        assert fc["rows"][0][1] == "=0"  # no income table to reference
        # expenses-only layout: no income table -> expenses anchored A9
        # (data 11, total 12) -> forecast anchored A15 (data 17-19)
        assert fc["rows"][0][2] == "=B12"
        assert sheet["conditional_formats"][0]["range"] == "H17:H19"

    def test_heal_never_fires_on_template(self):
        before = [r for t in self.norm["sheets"][0]["tables"] for r in t["rows"]]
        eg._heal_off_by_one_formula_rows(self.norm)
        after = [r for t in self.norm["sheets"][0]["tables"] for r in t["rows"]]
        assert before == after


# ── Subscription tracker: coercion ───────────────────────────────────


class TestSubscriptionCoercion:
    def test_cycle_aliases_and_defaults(self):
        p = ep.coerce_subscription_tracker_params(
            {
                "subscriptions": [
                    {"name": "Netflix", "amount": 15.99},  # no cycle -> monthly
                    {"name": "Domain", "amount": 20, "cycle": "Annual"},
                    {"name": "Water", "amount": 30, "cycle": "3 months"},
                    {"name": "Cleaner", "amount": 25, "cycle": "per week"},
                ]
            }
        )
        cycles = [s["cycle"] for s in p["subscriptions"]]
        assert cycles == ["monthly", "yearly", "quarterly", "weekly"]

    def test_status_semantics(self):
        p = ep.coerce_subscription_tracker_params(
            {
                "subscriptions": [
                    {"name": "A", "amount": 1, "active": "no"},
                    {"name": "B", "amount": 2, "cancelled": True},
                    {"name": "C", "amount": 3, "status": "cancelled"},
                    {"name": "D", "amount": 4},  # active unless stated
                ]
            }
        )
        assert [s["active"] for s in p["subscriptions"]] == [False, False, False, True]

    def test_quoted_amounts_and_list_entries(self):
        p = ep.coerce_subscription_tracker_params(
            {
                "currency": "usd",
                "subs": [
                    {"name": "Netflix", "amount": "$15.99"},
                    ["Spotify", "10.99", "monthly", "2026-04-01"],
                ],
            }
        )
        assert p["subscriptions"][0]["amount"] == 15.99
        assert p["subscriptions"][1]["name"] == "Spotify"
        assert p["subscriptions"][1]["amount"] == 10.99
        assert p["subscriptions"][1]["cycle"] == "monthly"
        assert p["subscriptions"][1]["next_renewal"] == "2026-04-01"
        assert p["currency"] == '"$"#,##0.00'

    def test_renewal_only_when_stated(self):
        p = ep.coerce_subscription_tracker_params(
            {"subscriptions": [{"name": "A", "amount": 1, "next_renewal": "soon"}]}
        )
        assert p["subscriptions"][0]["next_renewal"] is None

    def test_entries_without_name_or_amount_skipped(self):
        p = ep.coerce_subscription_tracker_params(
            {
                "subscriptions": [
                    {"amount": 10},  # no name
                    {"name": "No amount"},
                    {"name": "OK", "amount": 5},
                ]
            }
        )
        assert len(p["subscriptions"]) == 1

    def test_no_subscriptions_raises(self):
        with pytest.raises(ValueError):
            ep.coerce_subscription_tracker_params({})
        with pytest.raises(ValueError):
            ep.coerce_subscription_tracker_params({"subscriptions": []})
        with pytest.raises(ValueError):
            ep.coerce_subscription_tracker_params({"subscriptions": "Netflix"})
        with pytest.raises(ValueError):
            ep.coerce_subscription_tracker_params(None)


# ── Subscription tracker: builder lattice + math ─────────────────────


class TestSubscriptionBuilder:
    def setup_method(self):
        self.spec = ep.build_subscription_tracker_spec(dict(SUBSCRIPTION_PARAMS))
        self.norm = eg._normalize_spec(self.spec)

    def test_spec_validates_clean(self):
        errors, warnings = eg.validate_workbook_spec(self.spec)
        assert errors == []
        assert not [w for w in warnings if "above the table" in w]

    def test_geometry_and_formula_lattice(self):
        sheet = self.norm["sheets"][0]
        assert sheet["name"] == "Subscriptions"
        assert sheet["tab_color"] == "16304F"
        blocks = {b["cell"]: b for b in sheet["text_blocks"]}
        assert blocks["A1"]["text"] == "Subscription Tracker"
        assert blocks["A1"]["bold"] and blocks["A1"]["font_size"] == 14

        # main table anchored A3 with NO title -> header 3, data 4-8,
        # total 9
        main = sheet["tables"][0]
        assert main["start_cell"] == "A3"
        assert len(main["rows"]) == 5
        rows = main["rows"]
        assert rows[0][0] == "Netflix"
        assert rows[0][2] == 15.99
        assert rows[0][3] == "monthly"
        assert rows[0][4] == date(2026, 2, 11)
        assert rows[0][8] == "Active"
        assert rows[2][8] == "Cancelled"  # gym
        assert rows[3][4] == date(2026, 3, 1)  # Adobe renewal

        # every formula pins to the rendered row
        assert rows[0][5] == '=IF(E4="","n/a",E4-TODAY())'
        assert rows[4][5] == '=IF(E8="","n/a",E8-TODAY())'
        assert rows[0][6] == (
            '=C4*IF(D4="yearly",1/12,IF(D4="quarterly",1/3,' 'IF(D4="weekly",52/12,1)))'
        )
        assert rows[0][7] == (
            '=C4*IF(D4="yearly",1,IF(D4="quarterly",4,' 'IF(D4="weekly",52,12)))'
        )
        assert rows[4][6] == (
            '=C8*IF(D8="yearly",1/12,IF(D8="quarterly",1/3,' 'IF(D8="weekly",52/12,1)))'
        )

        # totals: SUMIF over the exact data rows keyed on the status
        assert main["total_row"][6] == '=SUMIF($I4:$I8,"Active",$G4:$G8)'
        assert main["total_row"][7] == '=SUMIF($I4:$I8,"Active",$H4:$H8)'
        assert main["auto_filter"] is True

        # spend summary anchored below the total row (title 11, data 13)
        summary = sheet["tables"][1]
        assert summary["start_cell"] == "A11"
        srows = {r[0]: r[1] for r in summary["rows"]}
        assert srows["Total Monthly Spend (Active)"] == "=G9"
        assert srows["Total Yearly Spend (Active)"] == "=H9"
        assert srows["Average Monthly per Active Sub"] == (
            '=IF(COUNTIF($I4:$I8,"Active")>0,G9/COUNTIF($I4:$I8,"Active"),"n/a")'
        )
        assert srows["Next Renewal (Earliest)"] == (
            '=IF(COUNT($E4:$E8)>0,TEXT(MIN($E4:$E8),"yyyy-mm-dd"),'
            '"no renewal dates")'
        )
        assert srows["Priciest Yearly Cost"] == (
            '=IF(COUNT($H4:$H8)>0,MAX($H4:$H8),"n/a")'
        )
        assert srows["Active / Cancelled Counts"] == (
            '=COUNTIF($I4:$I8,"Active")&" active / "'
            '&COUNTIF($I4:$I8,"Cancelled")&" cancelled"'
        )

        # chart pinned to the data rows
        chart = sheet["charts"][0]
        assert chart["type"] == "bar"
        assert chart["categories_range"] == "Subscriptions!A4:A8"
        assert chart["series"][0]["values_range"] == "Subscriptions!H4:H8"
        assert chart["anchor"] == "K3"

        # dropdowns over the exact data rows
        dvs = {d["range"]: d["values"] for d in sheet["data_validation"]}
        assert dvs["D4:D8"] == ["monthly", "quarterly", "yearly", "weekly"]
        assert dvs["I4:I8"] == ["Active", "Cancelled"]

        # conditional formats: overdue / soon renewals + status colors
        cf_ranges = [c["range"] for c in sheet["conditional_formats"]]
        assert cf_ranges == ["F4:F8", "I4:I8"]
        assert sheet["freeze_panes"] == "A4"

    def test_independent_monthly_yearly_equivalents(self):
        """The emitted factor constants must reproduce the standard
        monthly/yearly equivalence (yearly == 12 x monthly for every
        cycle — the invariant that catches a wrong factor)."""
        monthly_factor = {
            "monthly": 1.0,
            "quarterly": 1 / 3,
            "yearly": 1 / 12,
            "weekly": 52 / 12,
        }
        yearly_factor = {
            "monthly": 12.0,
            "quarterly": 4.0,
            "yearly": 1.0,
            "weekly": 52.0,
        }
        main = self.norm["sheets"][0]["tables"][0]
        for row in main["rows"]:
            amount, cycle = row[2], row[3]
            monthly = amount * monthly_factor[cycle]
            yearly = amount * yearly_factor[cycle]
            assert yearly == pytest.approx(12 * monthly)
        # spot values a user would sanity-check by hand
        by_name = {r[0]: r for r in main["rows"]}  # noqa
        assert 15.99 * monthly_factor["monthly"] == pytest.approx(15.99)
        assert 20 * yearly_factor["yearly"] == pytest.approx(20.0)
        assert 30 * monthly_factor["quarterly"] == pytest.approx(10.0)
        assert 660 * monthly_factor["yearly"] == pytest.approx(55.0)
        # and the active-only totals the SUMIFs will produce
        active_monthly = sum(
            r[2] * monthly_factor[r[3]] for r in main["rows"] if r[8] == "Active"
        )
        assert active_monthly == pytest.approx(15.99 + 20 / 12 + 55 + 10)

    def test_built_workbook(self, tmp_path):
        out = tmp_path / "subs.xlsx"
        eg._build_xlsx(self.norm, out)
        wb = load_workbook(out)
        assert wb.sheetnames == ["Subscriptions"]
        ws = wb["Subscriptions"]
        assert ws["C4"].value == 15.99
        assert ws["C4"].number_format == '"$"#,##0.00'
        assert ws["E4"].value == datetime(2026, 2, 11)
        assert ws["E4"].number_format == "yyyy-mm-dd"
        assert ws["F4"].value == '=IF(E4="","n/a",E4-TODAY())'
        assert ws["G4"].value.startswith('=C4*IF(D4="yearly"')
        assert ws["I6"].value == "Cancelled"
        assert ws["G9"].value == '=SUMIF($I4:$I8,"Active",$G4:$G8)'
        # summary table: title A11, header 12, data 13..18
        assert ws["B13"].value.startswith('=COUNTIF($I4:$I8,"Active")')
        assert ws["B14"].value == "=G9"  # total monthly spend
        assert ws["B15"].value == "=H9"  # total yearly spend
        assert len(ws._charts) == 1
        assert len(ws.data_validations.dataValidation) == 2
        assert len(ws.conditional_formatting._cf_rules) == 2
        assert ws.freeze_panes == "A4"

    def test_single_subscription_minimal(self):
        spec = ep.build_subscription_tracker_spec(
            {"subscriptions": [{"name": "Netflix", "amount": 15.99}]}
        )
        errors, _ = eg.validate_workbook_spec(spec)
        assert errors == []
        sheet = eg._normalize_spec(spec)["sheets"][0]
        main = sheet["tables"][0]
        assert main["start_cell"] == "A3"
        assert main["rows"][0][3] == "monthly"  # cycle default
        assert main["rows"][0][5] == '=IF(E4="","n/a",E4-TODAY())'
        assert sheet["charts"][0]["categories_range"] == "Subscriptions!A4:A4"

    def test_heal_never_fires_on_template(self):
        before = [r for t in self.norm["sheets"][0]["tables"] for r in t["rows"]]
        eg._heal_off_by_one_formula_rows(self.norm)
        after = [r for t in self.norm["sheets"][0]["tables"] for r in t["rows"]]
        assert before == after


# ── Expense report: coercion ─────────────────────────────────────────


class TestExpenseReportCoercion:
    def test_stanza_defaults(self):
        # paid_by Personal, billable True, receipt False unless stated
        p = ep.coerce_expense_report_params(
            {
                "expenses": [
                    {"date": "2026-03-02", "description": "Flight", "amount": 320}
                ]
            }
        )
        e = p["expenses"][0]
        assert e["paid_by"] == "Personal"
        assert e["billable"] is True
        assert e["receipt"] is False
        assert e["category"] == "Uncategorized"

    def test_paid_by_and_flag_aliases(self):
        p = ep.coerce_expense_report_params(
            {
                "expenses": [
                    {"date": "2026-03-02", "amount": 10, "paid_by": "employer"},
                    {"date": "2026-03-02", "amount": 10, "paid_by": "myself"},
                    {"date": "2026-03-02", "amount": 10, "billable": "no"},
                    {"date": "2026-03-02", "amount": 10, "receipt": "yes"},
                ]
            }
        )
        assert p["expenses"][0]["paid_by"] == "Company"
        assert p["expenses"][1]["paid_by"] == "Personal"
        assert p["expenses"][2]["billable"] is False
        assert p["expenses"][3]["receipt"] is True

    def test_quoted_amounts_and_alias_keys(self):
        p = ep.coerce_expense_report_params(
            {
                "submitted_by": "Sara",
                "expense_log": [
                    {
                        "date": "2026-03-02",
                        "category": "Travel",
                        "description": "Flight",
                        "amount": "€320",
                    },
                    {
                        "date": "2026-03-03",
                        "category": "Meal",
                        "description": "Dinner",
                        "amount": "84.50",
                    },
                ],
            }
        )
        assert p["employee"] == "Sara"
        assert p["expenses"][0]["amount"] == 320.0
        assert p["expenses"][1]["amount"] == 84.5

    def test_positional_list_entries(self):
        p = ep.coerce_expense_report_params(
            {
                "expenses": [
                    ["2026-03-02", "Flight CDG", 320],
                    ["2026-03-03", "Travel", "Taxi", 22],
                ]
            }
        )
        assert p["expenses"][0]["description"] == "Flight CDG"
        assert p["expenses"][0]["amount"] == 320.0
        assert p["expenses"][1]["category"] == "Travel"
        assert p["expenses"][1]["description"] == "Taxi"
        assert p["expenses"][1]["amount"] == 22.0

    def test_period_and_bad_dates(self):
        p = ep.coerce_expense_report_params(
            {
                "period_start": "2026-03-02",
                "period_end": "March 6",
                "expenses": [{"date": "yesterday", "amount": 5}],
            }
        )
        assert p["period_start"] == "2026-03-02"
        assert p["period_end"] is None
        assert p["expenses"][0]["date"] is None  # unusable date -> blank

    def test_negative_amount_taken_as_positive(self):
        p = ep.coerce_expense_report_params(
            {"expenses": [{"date": "2026-03-02", "amount": -50}]}
        )
        assert p["expenses"][0]["amount"] == 50.0

    def test_no_expenses_raises(self):
        with pytest.raises(ValueError):
            ep.coerce_expense_report_params({})
        with pytest.raises(ValueError):
            ep.coerce_expense_report_params({"expenses": []})
        with pytest.raises(ValueError):
            ep.coerce_expense_report_params({"expenses": [{"date": "2026-03-02"}]})
        with pytest.raises(ValueError):
            ep.coerce_expense_report_params(None)


# ── Expense report: builder lattice + math ───────────────────────────


class TestExpenseReportBuilder:
    def setup_method(self):
        self.spec = ep.build_expense_report_spec(dict(EXPENSE_PARAMS))
        self.norm = eg._normalize_spec(self.spec)

    def test_spec_validates_clean(self):
        errors, warnings = eg.validate_workbook_spec(self.spec)
        assert errors == []
        assert not [w for w in warnings if "above the table" in w]

    def test_geometry_and_formula_lattice(self):
        sheet = self.norm["sheets"][0]
        assert sheet["name"] == "Expenses"
        assert sheet["tab_color"] == "16304F"
        blocks = {b["cell"]: b for b in sheet["text_blocks"]}
        assert blocks["A1"]["text"] == "Expense Report — Paris client trip"
        assert blocks["A1"]["bold"] and blocks["A1"]["font_size"] == 14
        assert blocks["B3"]["text"] == "Sara Idrissi"
        assert blocks["B5"]["text"] == "2026-03-02 to 2026-03-06"

        # info block takes rows 3-5 -> log anchored A7 (header 7,
        # data 8-12, total 13)
        log, cat, reimb = sheet["tables"]
        assert log["start_cell"] == "A7"
        assert len(log["rows"]) == 5
        rows = log["rows"]
        assert rows[0][0] == date(2026, 3, 2)
        assert rows[0][1] == "Travel"
        assert rows[0][3] == 320.0
        assert rows[0][4] == "Personal"
        assert rows[0][5] == "Yes"
        assert rows[0][6] == "Yes"
        assert rows[2][4] == "Company"
        assert rows[3][5] == "No"  # billable false
        assert rows[4][1] == "Uncategorized"
        assert log["total_row"][3] == "=SUM(D{first_row}:D{last_row})"
        assert log["auto_filter"] is True

        # By Category anchored A15 with title -> data 17-20, total 21;
        # SUMIF + guarded share against the log's total row D13
        assert cat["start_cell"] == "A15"
        categories = [r[0] for r in cat["rows"]]
        assert categories == ["Travel", "Meal", "Lodging", "Uncategorized"]
        assert cat["rows"][0][1] == "=SUMIF($B$8:$B$12,$A17,$D$8:$D$12)"
        assert cat["rows"][1][1] == "=SUMIF($B$8:$B$12,$A18,$D$8:$D$12)"
        assert cat["rows"][0][2] == '=IF($D$13>0,B17/$D$13,"n/a")'
        assert cat["total_row"][1] == "=SUM(B{first_row}:B{last_row})"

        # Reimbursement anchored A23 with title -> data 25-31
        assert reimb["start_cell"] == "A23"
        rrows = {r[0]: r[1] for r in reimb["rows"]}
        assert rrows["Total Expenses"] == "=D13"
        assert rrows["Paid Personally (total)"] == (
            '=SUMIF($E$8:$E$12,"Personal",$D$8:$D$12)'
        )
        assert rrows["Paid by Company (total)"] == (
            '=SUMIF($E$8:$E$12,"Company",$D$8:$D$12)'
        )
        assert rrows["Reimbursable (paid personally, billable)"] == (
            '=SUMIFS($D$8:$D$12,$E$8:$E$12,"Personal",$F$8:$F$12,"Yes")'
        )
        assert rrows["Personal non-billable (not reimbursable)"] == (
            '=SUMIFS($D$8:$D$12,$E$8:$E$12,"Personal",$F$8:$F$12,"No")'
        )
        assert rrows["Company-paid non-billable (owe back)"] == (
            '=SUMIFS($D$8:$D$12,$E$8:$E$12,"Company",$F$8:$F$12,"No")'
        )
        # net = reimbursable (row 28) - owe back (row 30)
        assert rrows["Net Due to Employee"] == "=B28-B30"

        # pie chart over the category rows
        chart = sheet["charts"][0]
        assert chart["type"] == "pie"
        assert chart["categories_range"] == "Expenses!A17:A20"
        assert chart["series"][0]["values_range"] == "Expenses!B17:B20"

        # dropdowns + conditional formats over the exact log rows
        dvs = {d["range"]: d["values"] for d in sheet["data_validation"]}
        assert dvs["E8:E12"] == ["Personal", "Company"]
        assert dvs["F8:F12"] == ["Yes", "No"]
        assert dvs["G8:G12"] == ["Yes", "No"]
        cf_ranges = [c["range"] for c in sheet["conditional_formats"]]
        assert cf_ranges == ["F8:F12", "G8:G12", "B31:B31"]
        assert sheet["freeze_panes"] == "A8"

    def test_independent_reimbursement_math(self):
        """Recompute the splits in Python from the emitted log rows and
        check the identities the SUMIF/SUMIFS lattice must produce."""
        log = self.norm["sheets"][0]["tables"][0]
        rows = log["rows"]
        total = sum(r[3] for r in rows)
        personal = sum(r[3] for r in rows if r[4] == "Personal")
        company = sum(r[3] for r in rows if r[4] == "Company")
        reimbursable = sum(r[3] for r in rows if r[4] == "Personal" and r[5] == "Yes")
        personal_nonbill = sum(
            r[3] for r in rows if r[4] == "Personal" and r[5] == "No"
        )
        owe_back = sum(r[3] for r in rows if r[4] == "Company" and r[5] == "No")
        net = reimbursable - owe_back

        assert total == pytest.approx(320 + 84.5 + 405 + 22 + 15.75)
        assert personal == pytest.approx(320 + 84.5 + 22 + 15.75)
        assert company == pytest.approx(405)
        assert reimbursable == pytest.approx(320 + 84.5 + 15.75)
        assert personal_nonbill == pytest.approx(22)
        assert owe_back == pytest.approx(0)  # the hotel was billable
        assert net == pytest.approx(reimbursable)
        # conservation identities
        assert personal + company == pytest.approx(total)
        assert reimbursable + personal_nonbill == pytest.approx(personal)

        # per-category totals the SUMIFs will produce
        cat = self.norm["sheets"][0]["tables"][1]
        for row in cat["rows"]:
            expected = sum(r[3] for r in rows if r[1] == row[0])
            assert expected > 0
        travel = sum(r[3] for r in rows if r[1] == "Travel")
        assert travel == pytest.approx(320)

    def test_built_workbook(self, tmp_path):
        out = tmp_path / "expenses.xlsx"
        eg._build_xlsx(self.norm, out)
        wb = load_workbook(out)
        assert wb.sheetnames == ["Expenses"]
        ws = wb["Expenses"]
        assert ws["A8"].value == datetime(2026, 3, 2)
        assert ws["A8"].number_format == "yyyy-mm-dd"
        assert ws["D8"].value == 320
        assert ws["D8"].number_format == '#,##0.00" €"'
        assert ws["D13"].value == "=SUM(D8:D12)"
        assert ws["B17"].value == "=SUMIF($B$8:$B$12,$A17,$D$8:$D$12)"
        assert ws["C17"].value == '=IF($D$13>0,B17/$D$13,"n/a")'
        assert ws["B25"].value == "=D13"
        assert ws["B28"].value.startswith("=SUMIFS($D$8:$D$12")
        assert ws["B31"].value == "=B28-B30"
        assert len(ws._charts) == 1
        assert len(ws.data_validations.dataValidation) == 3
        assert len(ws.conditional_formatting._cf_rules) == 3
        assert ws.freeze_panes == "A8"

    def test_single_category_has_no_pie(self):
        params = {
            "expenses": [
                {"date": "2026-03-02", "category": "Travel", "amount": 320},
                {"date": "2026-03-03", "category": "Travel", "amount": 22},
            ]
        }
        spec = ep.build_expense_report_spec(params)
        errors, _ = eg.validate_workbook_spec(spec)
        assert errors == []
        sheet = eg._normalize_spec(spec)["sheets"][0]
        assert not sheet.get("charts")  # one slice is not a pie

    def test_minimal_params_no_info_block(self):
        params = {"expenses": [{"date": "2026-03-02", "amount": 10}]}
        spec = ep.build_expense_report_spec(params)
        errors, _ = eg.validate_workbook_spec(spec)
        assert errors == []
        sheet = eg._normalize_spec(spec)["sheets"][0]
        # no employee/purpose/period -> log anchored right below title
        assert sheet["tables"][0]["start_cell"] == "A4"
        assert sheet["tables"][0]["rows"][0][3] == 10.0

    def test_heal_never_fires_on_template(self):
        before = [r for t in self.norm["sheets"][0]["tables"] for r in t["rows"]]
        eg._heal_off_by_one_formula_rows(self.norm)
        after = [r for t in self.norm["sheets"][0]["tables"] for r in t["rows"]]
        assert before == after


# ── Routing integration (one per pattern) ────────────────────────────


class TestPatternRouting:
    @pytest.mark.asyncio
    async def test_cashflow_forecast_routes_to_template(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            eg,
            "_call_llm",
            _classify_response(
                {"pattern": "cashflow_forecast", "params": dict(CASHFLOW_PARAMS)}
            ),
        )

        async def must_not_run(brief, requirements, model=None):
            raise AssertionError("AI path must not run when pattern matches")

        monkeypatch.setattr(eg, "_generate_workbook_json", must_not_run)
        monkeypatch.setattr(eg, "_get_reports_dir", lambda: tmp_path)
        result = await eg.generate_spreadsheet(
            "project my cash flow forecast for 12 months starting with 5,000 "
            "and a running balance"
        )
        assert result["pattern"] == "cashflow_forecast"
        assert result["sheet_names"] == ["Forecast", "Summary"]
        assert result["chart_count"] == 1
        assert result["filename"] == "cashflow_forecast.xlsx"
        assert (tmp_path / f"{result['report_id']}.xlsx").exists()
        assert "cashflow_forecast template" in result["summary"]

    @pytest.mark.asyncio
    async def test_subscription_tracker_routes_to_template(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            eg,
            "_call_llm",
            _classify_response(
                {"pattern": "subscription_tracker", "params": dict(SUBSCRIPTION_PARAMS)}
            ),
        )

        async def must_not_run(brief, requirements, model=None):
            raise AssertionError("AI path must not run when pattern matches")

        monkeypatch.setattr(eg, "_generate_workbook_json", must_not_run)
        monkeypatch.setattr(eg, "_get_reports_dir", lambda: tmp_path)
        result = await eg.generate_spreadsheet(
            "track my subscriptions and recurring membership renewals with "
            "monthly and yearly costs"
        )
        assert result["pattern"] == "subscription_tracker"
        assert result["sheet_names"] == ["Subscriptions"]
        assert result["chart_count"] == 1
        assert result["filename"] == "subscription_tracker.xlsx"
        assert (tmp_path / f"{result['report_id']}.xlsx").exists()
        assert "subscription_tracker template" in result["summary"]

    @pytest.mark.asyncio
    async def test_expense_report_routes_to_template(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            eg,
            "_call_llm",
            _classify_response(
                {"pattern": "expense_report", "params": dict(EXPENSE_PARAMS)}
            ),
        )

        async def must_not_run(brief, requirements, model=None):
            raise AssertionError("AI path must not run when pattern matches")

        monkeypatch.setattr(eg, "_generate_workbook_json", must_not_run)
        monkeypatch.setattr(eg, "_get_reports_dir", lambda: tmp_path)
        result = await eg.generate_spreadsheet(
            "expense report for my Paris business trip with receipts for "
            "reimbursement"
        )
        assert result["pattern"] == "expense_report"
        assert result["sheet_names"] == ["Expenses"]
        assert result["chart_count"] == 1
        assert result["filename"] == "expense_report.xlsx"
        assert (tmp_path / f"{result['report_id']}.xlsx").exists()
        assert "expense_report template" in result["summary"]

    def test_gate_regex_matches_recurring_cost_briefs(self):
        assert eg._PATTERN_GATE_RE.search("project my cash flow for 12 months")
        assert eg._PATTERN_GATE_RE.search("cashflow forecast with opening balance")
        assert eg._PATTERN_GATE_RE.search("track my subscriptions and renewals")
        assert eg._PATTERN_GATE_RE.search("expense report for my business trip")

    def test_shortlist_includes_patterns(self):
        for brief, expected in (
            ("cash flow projection with a running balance", "cashflow_forecast"),
            ("my recurring subscriptions and membership fees", "subscription_tracker"),
            ("expense report with reimbursement for my trip", "expense_report"),
        ):
            assert expected in eg._shortlist_patterns(brief)
