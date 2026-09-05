"""
Tests for the deterministic template layer (services/excel_patterns.py)
and its routing integration in services/excel_gen.py.

The templates exist because models hand-writing formula lattices emit
refs that don't match the rendered layout (off by 1..N rows) — see the
round-3 user report (interest reading the balance three rows back,
starting balance on an empty cell → negative-balance spiral). These
tests pin down:
  • param coercion (quoted/currency/percent strings from classifiers)
  • builder layout math — every formula references the row the
    converter will actually render (title rows included!)
  • routing: classifier → template with the AI path skipped; every
    failure mode falls back to the AI path without raising
  • the new validation warning that flags the off-by-N class
  • the heal no longer false-positives on cross-sheet qualified refs
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import excel_gen as eg  # noqa: E402
from app.services import excel_patterns as ep  # noqa: E402
from openpyxl import load_workbook  # noqa: E402

# ── Param coercion ───────────────────────────────────────────────────


class TestCoercion:
    def test_to_number_handles_strings(self):
        assert ep.to_number("25000") == 25000.0
        assert ep.to_number("$25,000") == 25000.0
        assert ep.to_number("25 000 €") == 25000.0
        assert ep.to_number("6.5") == 6.5
        assert ep.to_number(7) == 7.0
        assert ep.to_number("abc") is None
        assert ep.to_number(None) is None
        assert ep.to_number(True) is None

    def test_to_rate_percent_semantics(self):
        assert ep.to_rate("6.5%") == 0.065
        assert ep.to_rate(6.5) == 0.065  # bare > 1 → percent
        assert ep.to_rate(0.065) == 0.065  # decimal stays
        assert ep.to_rate("0.065") == 0.065
        assert ep.to_rate(0) == 0.0
        assert ep.to_rate(None) is None

    def test_to_int(self):
        assert ep.to_int("36") == 36
        assert ep.to_int(3.0) == 3
        assert ep.to_int(3.5) is None

    def test_to_iso_date(self):
        assert ep.to_iso_date("2026-09-01") == "2026-09-01"
        assert ep.to_iso_date("2026-9-1") == "2026-09-01"
        assert ep.to_iso_date("2026-13-01") is None
        assert ep.to_iso_date("sept 2026") is None

    def test_amortization_params_aliases_and_defaults(self):
        p = ep.coerce_amortization_params(
            {"principal": "$25,000", "interest_rate": "6.5%", "term_years": 3}
        )
        assert p["loan_amount"] == 25000.0
        assert p["annual_rate"] == 0.065
        assert p["term_months"] == 36
        assert p["payment"] is None

    def test_amortization_params_missing_raise(self):
        with pytest.raises(ValueError):
            ep.coerce_amortization_params({"loan_amount": 25000})
        with pytest.raises(ValueError):
            ep.coerce_amortization_params(
                {"loan_amount": -5, "annual_rate": 0.05, "term_months": 12}
            )


# ── Amortization builder: layout math is the contract ────────────────


class TestAmortizationBuilder:
    def setup_method(self):
        self.spec = ep.build_amortization_spec(
            dict(
                loan_amount=25000,
                annual_rate=0.065,
                term_months=36,
                start_date="2026-09-01",
            )
        )
        self.norm = eg._normalize_spec(self.spec)

    def test_spec_validates_clean(self):
        errors, warnings = eg.validate_workbook_spec(self.spec)
        assert errors == []
        assert not [w for w in warnings if "above the table" in w]

    def test_geometry_and_formula_lattice(self):
        sched = self.norm["sheets"][0]
        assert sched["name"] == "Amortization"
        table = sched["tables"][0]
        # start A7, no table title → header row 7, data 8..43, total 44
        assert table["start_cell"] == "A7"
        assert len(table["rows"]) == 36
        first, second = table["rows"][0], table["rows"][1]
        # every ref points at the row the converter actually renders
        assert first[1] == "=$B$5"
        assert first[2] == "=B8-D8"
        assert first[3] == "=ROUND($B$2*$B$3/12,2)"
        assert first[4] == "=$B$2-C8"
        assert second[3] == "=ROUND(E8*$B$3/12,2)"
        assert second[4] == "=E8-C9"
        # params blocks populate B2..B5 above the table
        blocks = {b["cell"]: b["text"] for b in sched["text_blocks"]}
        assert blocks["B2"] == 25000.0
        assert blocks["B3"] == 0.065
        assert blocks["B4"] == 36
        assert blocks["B5"] == "=ROUND(-PMT(B3/12,B4,B2),2)"

    def test_summary_sheet_cross_refs(self):
        summary = self.norm["sheets"][1]
        assert summary["name"] == "Summary"
        rows = summary["tables"][0]["rows"]
        refs = {r[0]: r[1] for r in rows}
        assert refs["Loan Amount"] == "=Amortization!$B$2"
        # 36 data rows: 8..43 → totals row 44
        assert refs["Total Paid"] == "=Amortization!$B$44"
        assert refs["Total Interest"] == "=Amortization!$D$44"
        assert refs["Final Balance"] == "=Amortization!$E$43"

    def test_chart_ranges_match_layout(self):
        chart = self.norm["sheets"][0]["charts"][0]
        assert chart["categories_range"] == "Amortization!A8:A43"
        assert chart["series"][0]["values_range"] == "Amortization!E8:E43"

    def test_built_workbook_formulas(self, tmp_path):
        out = tmp_path / "amort.xlsx"
        eg._build_xlsx(self.norm, out)
        wb = load_workbook(out)
        ws = wb["Amortization"]
        assert ws["B5"].value == "=ROUND(-PMT(B3/12,B4,B2),2)"
        assert ws["B2"].value == 25000
        assert ws["B3"].value == 0.065
        assert ws["D9"].value == "=ROUND(E8*$B$3/12,2)"
        assert ws["E9"].value == "=E8-C9"
        assert ws["D43"].value == "=ROUND(E42*$B$3/12,2)"
        # total row: placeholders expanded to the real data rows
        assert ws["B44"].value == "=SUM(B8:B43)"
        assert ws["D44"].value == "=SUM(D8:D43)"

    def test_heal_never_fires_on_template(self):
        # the normalized spec must be unchanged by a second heal pass —
        # formulas never reference the header row (row 7)
        before = [r for t in self.norm["sheets"][0]["tables"] for r in t["rows"]]
        eg._heal_off_by_one_formula_rows(self.norm)
        after = [r for t in self.norm["sheets"][0]["tables"] for r in t["rows"]]
        assert before == after


# ── Invoice builder ──────────────────────────────────────────────────


class TestInvoiceBuilder:
    @staticmethod
    def _blocks_by_text(sheet):
        return {b["text"]: b["cell"] for b in sheet["text_blocks"]}

    def test_layout_and_totals(self):
        spec = ep.build_invoice_spec(
            dict(
                seller="Acme\n12 Rue",
                client="Client SARL",
                invoice_number="INV-1",
                date="2026-02-10",
                due_date="2026-03-12",
                items=[
                    {"description": "Logo", "quantity": 1, "unit_price": 1200},
                    {"description": "Consulting", "quantity": 7.5, "unit_price": 90},
                ],
                tax_rate=0.2,
            )
        )
        errors, _ = eg.validate_workbook_spec(spec)
        assert errors == []
        sheet = eg._normalize_spec(spec)["sheets"][0]
        table = sheet["tables"][0]
        # From (2 lines) + Bill To (1 line) end at row 13 → start 14
        items_start = 14
        first_data = items_start + 1
        assert table["start_cell"] == f"A{items_start}"
        assert table["rows"][0][3] == f"=B{first_data}*C{first_data}"
        assert table["rows"][1][3] == f"=B{first_data + 1}*C{first_data + 1}"
        assert table["total_row"][3] == "=SUM(D{first_row}:D{last_row})"
        blocks = {b["text"]: b["cell"] for b in sheet["text_blocks"]}
        subtotal_row = first_data + 2  # data 15-16 → total row 17
        assert blocks["Tax (20%)"] == f"C{subtotal_row + 1}"  # label col C
        assert blocks["TOTAL DUE"] == f"C{subtotal_row + 2}"
        by_cell = {b["cell"]: b for b in sheet["text_blocks"]}
        assert (
            by_cell[f"D{subtotal_row + 1}"]["text"] == f"=ROUND(D{subtotal_row}*0.2,2)"
        )
        assert (
            by_cell[f"D{subtotal_row + 2}"]["text"]
            == f"=D{subtotal_row}+D{subtotal_row + 1}"
        )

    def test_tax_zero_discount_positive_no_row_collision(self):
        # regression: tax=0 + discount>0 must not stack Discount and
        # TOTAL DUE on the same row
        spec = ep.build_invoice_spec(
            dict(
                items=[{"description": "A", "quantity": 1, "unit_price": 100}],
                discount=10,
            )
        )
        sheet = eg._normalize_spec(spec)["sheets"][0]
        by_cell = {b["cell"]: b for b in sheet["text_blocks"]}
        disc_row = int(
            next(c for c, b in by_cell.items() if b["text"] == "Discount")[1:]
        )  # label C15
        total_row = int(
            next(c for c, b in by_cell.items() if b["text"] == "TOTAL DUE")[1:]
        )  # label C16
        assert total_row == disc_row + 1
        assert by_cell[f"D{total_row}"]["text"] == f"=D{disc_row - 1}+D{disc_row}"

    def test_missing_items_raise(self):
        with pytest.raises(ValueError):
            ep.build_invoice_spec({"seller": "x"})


# ── Budget builder ───────────────────────────────────────────────────


class TestBudgetBuilder:
    def test_title_row_math(self):
        # regression: tables have title fields — data starts TWO rows
        # below the anchor, the total row right after the data
        spec = ep.build_budget_spec(
            dict(
                period="monthly",
                income=[
                    {"source": "Salary", "amount": 6500},
                    {"source": "Freelance", "amount": 800},
                ],
                expenses=[
                    {"category": "Rent", "amount": 1800},
                    {"category": "Food", "amount": 550},
                    {"category": "Bus", "amount": 220},
                ],
            )
        )
        errors, _ = eg.validate_workbook_spec(spec)
        assert errors == []
        sheet = eg._normalize_spec(spec)["sheets"][0]
        income_t, expenses_t, summary_t = sheet["tables"]
        # Income anchored at 3 with title → data 5-6, total 7
        assert income_t["start_cell"] == "A3"
        assert income_t["total_row"][0] == "Total Income"
        # Expenses anchored at 9 → data 11-13, total 14
        assert expenses_t["start_cell"] == "A9"
        # Summary anchored at 16 → data 18-21 referencing B7 / B14
        rows = {r[0]: r[1] for r in summary_t["rows"]}
        assert rows["Total Income"] == "=B7"
        assert rows["Total Expenses"] == "=B14"
        assert rows["Net (Income - Expenses)"] == "=B7-B14"
        assert 'TEXT((B7-B14)/B7,"0.0%")' in rows["Savings Rate"]

    def test_expenses_only_budget(self):
        spec = ep.build_budget_spec(
            dict(expenses=[{"category": "Rent", "amount": 1000}])
        )
        sheet = eg._normalize_spec(spec)["sheets"][0]
        rows = {r[0]: r[1] for r in sheet["tables"][-1]["rows"]}
        assert rows["Total Income"] == "=0"
        assert rows["Net (Income - Expenses)"].startswith("=0-")

    def test_no_lines_raise(self):
        with pytest.raises(ValueError):
            ep.build_budget_spec({"period": "monthly"})


# ── Routing integration ──────────────────────────────────────────────


def _classify_response(payload):
    import json

    async def fake_llm(messages):
        assert "route spreadsheet requests" in messages[0]["content"]
        return json.dumps(payload)

    return fake_llm


class TestPatternRouting:
    @pytest.mark.asyncio
    async def test_amortization_routes_to_template(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            eg,
            "_call_llm",
            _classify_response(
                {
                    "pattern": "amortization",
                    "params": {
                        "loan_amount": 25000,
                        "annual_rate": 0.065,
                        "term_months": 36,
                        "start_date": "2026-09-01",
                    },
                }
            ),
        )

        async def must_not_run(brief, requirements):
            raise AssertionError("AI path must not run when pattern matches")

        monkeypatch.setattr(eg, "_generate_workbook_json", must_not_run)
        monkeypatch.setattr(eg, "_get_reports_dir", lambda: tmp_path)
        result = await eg.generate_spreadsheet("loan amortization $25,000 6.5% 3 years")
        assert result["pattern"] == "amortization"
        assert result["sheet_count"] == 2
        assert (tmp_path / f"{result['report_id']}.xlsx").exists()

    @pytest.mark.asyncio
    async def test_none_falls_back(self, tmp_path, monkeypatch):
        import json

        async def fake_llm(messages):
            return json.dumps({"pattern": "none", "params": {}})

        async def fake_ai(brief, requirements):
            return eg._normalize_spec(
                {
                    "sheets": [
                        {
                            "name": "Data",
                            "tables": [
                                {"start_cell": "A1", "headers": ["A"], "rows": [[1]]}
                            ],
                        }
                    ]
                }
            )

        monkeypatch.setattr(eg, "_call_llm", fake_llm)
        monkeypatch.setattr(eg, "_generate_workbook_json", fake_ai)
        monkeypatch.setattr(eg, "_get_reports_dir", lambda: tmp_path)
        result = await eg.generate_spreadsheet("compile budget data")
        assert result["pattern"] is None

    @pytest.mark.asyncio
    async def test_gate_skips_classifier(self, tmp_path, monkeypatch):
        async def must_not_run_llm(messages):
            raise AssertionError("classifier must be skipped without keywords/digits")

        async def fake_ai(brief, requirements):
            return eg._normalize_spec(
                {
                    "sheets": [
                        {
                            "name": "S",
                            "tables": [
                                {"start_cell": "A1", "headers": ["A"], "rows": [[1]]}
                            ],
                        }
                    ]
                }
            )

        monkeypatch.setattr(eg, "_call_llm", must_not_run_llm)
        monkeypatch.setattr(eg, "_generate_workbook_json", fake_ai)
        monkeypatch.setattr(eg, "_get_reports_dir", lambda: tmp_path)
        result = await eg.generate_spreadsheet("grocery list")
        assert result["pattern"] is None

    @pytest.mark.asyncio
    async def test_insufficient_params_fall_back(self, tmp_path, monkeypatch):
        async def fake_ai(brief, requirements):
            return eg._normalize_spec(
                {
                    "sheets": [
                        {
                            "name": "S",
                            "tables": [
                                {"start_cell": "A1", "headers": ["A"], "rows": [[1]]}
                            ],
                        }
                    ]
                }
            )

        monkeypatch.setattr(
            eg,
            "_call_llm",
            _classify_response(
                {"pattern": "amortization", "params": {"loan_amount": 25000}}
            ),
        )
        monkeypatch.setattr(eg, "_generate_workbook_json", fake_ai)
        monkeypatch.setattr(eg, "_get_reports_dir", lambda: tmp_path)
        result = await eg.generate_spreadsheet("loan request")
        assert result["pattern"] is None

    @pytest.mark.asyncio
    async def test_garbage_classifier_falls_back(self, tmp_path, monkeypatch):
        async def fake_llm(messages):
            return "I cannot answer that"

        async def fake_ai(brief, requirements):
            return eg._normalize_spec(
                {
                    "sheets": [
                        {
                            "name": "S",
                            "tables": [
                                {"start_cell": "A1", "headers": ["A"], "rows": [[1]]}
                            ],
                        }
                    ]
                }
            )

        monkeypatch.setattr(eg, "_call_llm", fake_llm)
        monkeypatch.setattr(eg, "_generate_workbook_json", fake_ai)
        monkeypatch.setattr(eg, "_get_reports_dir", lambda: tmp_path)
        result = await eg.generate_spreadsheet("budget for my salary")
        assert result["pattern"] is None


# ── New off-by-N warning + heal cross-sheet fix ──────────────────────


class TestOffByNDetection:
    def _sheet_with_formula(self, formula, blocks=None):
        return {
            "name": "S",
            "text_blocks": blocks or [],
            "tables": [
                {
                    "start_cell": "A2",
                    "title": "T",
                    "headers": ["A", "B"],
                    "rows": [[1, formula]],
                }
            ],
        }

    def test_unpopulated_above_header_ref_warns(self):
        # column B is inside the table band (headers A,B); B2 sits on
        # the title row — only the anchor A2 holds text, so B2 renders
        # empty and the formula silently reads 0
        spec = {"sheets": [self._sheet_with_formula("=B2*0.5")]}
        _errors, warnings = eg.validate_workbook_spec(spec)
        assert any("above the table" in w for w in warnings)

    def test_populated_ref_does_not_warn(self):
        spec = {
            "sheets": [
                self._sheet_with_formula(
                    "=$B$1*0.5", blocks=[{"cell": "B1", "text": 25000}]
                )
            ]
        }
        _errors, warnings = eg.validate_workbook_spec(spec)
        assert not [w for w in warnings if "above the table" in w]

    def test_cross_sheet_qualified_ref_does_not_warn(self):
        # a qualified ref targets another sheet — not our header row
        spec = {"sheets": [self._sheet_with_formula("=Other!$B$2*0.5")]}
        _errors, warnings = eg.validate_workbook_spec(spec)
        assert not [w for w in warnings if "above the table" in w]

    def test_heal_ignores_cross_sheet_refs_to_own_header_row(self):
        # regression (found via the template Summary sheet): a table on
        # "Summary" whose formulas reference Amortization!$B$4 must NOT
        # be detected as "references own header row 4"
        spec = {
            "sheets": [
                {
                    "name": "Amortization",
                    "tables": [
                        {
                            "start_cell": "A7",
                            "headers": ["M", "P"],
                            "rows": [[1, 2]],
                        }
                    ],
                },
                {
                    "name": "Summary",
                    "tables": [
                        {
                            "start_cell": "A3",
                            "title": "Loan Summary",
                            "headers": ["Item", "Value"],
                            "rows": [
                                ["Term", "=Amortization!$B$7"],
                                [
                                    "X",
                                    "=Amortization!$B$4",
                                ],  # row 4 = Summary's header row
                            ],
                        }
                    ],
                },
            ]
        }
        norm = eg._normalize_spec(spec)
        summary_rows = norm["sheets"][1]["tables"][0]["rows"]
        # heal must not shift the cross-sheet refs (no false positive)
        assert summary_rows[1][1] == "=Amortization!$B$4"
