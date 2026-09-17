"""
Tests for the two-stage pattern routing machinery in services/excel_gen.py:

  Stage 1  pure-Python keyword matching — the union pre-gate
           (_PATTERN_GATE_RE) and the ranked, capped shortlist
           (_shortlist_patterns) built from every pattern's
           PATTERN_KEYWORDS.
  Stage 2  classifier prompt assembly (_parse_classifier_prompt +
           _classifier_system_prompt) — only the shortlisted stanzas
           from prompts/pattern_classifier.md reach the small model.

These tests pin the machinery itself (registry↔prompt consistency,
keyword regex semantics, gate behavior, shortlist ranking/capping,
prompt size discipline, and every fallback path of _try_pattern_spec),
complementing the per-pattern routing tests in the pattern batch files.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import excel_gen as eg  # noqa: E402
from app.services import patterns as ep  # noqa: E402


def _classify_response(payload):
    """Fake _call_llm returning a canned classifier JSON object."""
    if isinstance(payload, Exception):
        async def failing_llm(messages, model=None):
            raise payload

        return failing_llm

    async def fake_llm(messages, model=None):
        return json.dumps(payload)

    return fake_llm


def _capture_llm(store):
    """Fake _call_llm capturing the messages and returning 'none'."""

    async def capturing_llm(messages, model=None):
        store.extend(messages)
        return json.dumps({"pattern": "none", "params": {}})

    return capturing_llm


# ── Registry ↔ prompt consistency ───────────────────────────────────


class TestStanzaRegistryConsistency:
    def test_every_stanza_has_a_builder(self):
        _, stanzas, _ = eg._parse_classifier_prompt()
        builders = set(ep.PATTERN_BUILDERS)
        stanza_names = set(stanzas) - {"none"}
        assert stanza_names <= builders, (
            f"stanzas without a registered builder: {sorted(stanza_names - builders)}"
        )

    def test_every_pattern_has_a_stanza(self):
        _, stanzas, _ = eg._parse_classifier_prompt()
        stanza_names = set(stanzas) - {"none"}
        builders = set(ep.PATTERN_BUILDERS)
        assert builders <= stanza_names, (
            f"registered patterns without a prompt stanza: {sorted(builders - stanza_names)}"
        )

    def test_none_stanza_exists(self):
        _, stanzas, _ = eg._parse_classifier_prompt()
        assert "none" in stanzas
        assert stanzas["none"].strip()

    def test_every_pattern_has_routing_keywords(self):
        # A pattern without keywords never enters the shortlist —
        # effectively unreachable via routing. Every registered pattern
        # must therefore carry at least one keyword.
        missing = [
            name for name in ep.PATTERN_BUILDERS if not ep.PATTERN_KEYWORDS.get(name)
        ]
        assert missing == [], f"patterns without routing keywords: {missing}"

    def test_every_pattern_has_a_description(self):
        for name, desc in ep.PATTERN_DESCRIPTIONS.items():
            assert isinstance(desc, str) and desc.strip(), (
                f"pattern {name!r} has no PATTERN_DESCRIPTION"
            )

    def test_registry_is_large(self):
        # The whole point of two-stage routing: 30+ patterns.
        assert len(ep.PATTERN_BUILDERS) >= 30

    def test_stanza_names_are_registry_keys(self):
        # Stanza markers must match PATTERN_NAME exactly (no drift like
        # "savings-goal" vs "savings_goal").
        _, stanzas, _ = eg._parse_classifier_prompt()
        for name in stanzas:
            assert name == name.strip()
            assert " " not in name and name == name.lower()

    def test_every_pattern_stanza_declares_params(self):
        _, stanzas, _ = eg._parse_classifier_prompt()
        for name, text in stanzas.items():
            if name == "none":
                continue
            assert "params:" in text, f"stanza {name!r} missing params contract"


# ── Keyword regex semantics ──────────────────────────────────────────


class TestKeywordRegex:
    def test_single_word_matches_suffixes(self):
        rx = eg._keyword_regex("budget")
        assert rx.search("monthly budgets")
        assert rx.search("budgeting for 2026")
        assert rx.search("my budget:")

    def test_single_word_needs_word_boundary(self):
        rx = eg._keyword_regex("budget")
        assert not rx.search("overbudgeting")  # no boundary inside a word

    def test_case_insensitive(self):
        rx = eg._keyword_regex("budget")
        assert rx.search("BUDGET PLANNER")

    def test_multiword_phrase_matches_verbatim(self):
        rx = eg._keyword_regex("shopping list")
        assert rx.search("a shopping list for the week")
        assert not rx.search("shopping lists")  # phrase must end at a boundary

    def test_stem_matching(self):
        rx = eg._keyword_regex("amortiz")
        assert rx.search("amortization")
        assert rx.search("amortized")
        assert rx.search("amortizing")


# ── The union pre-gate ──────────────────────────────────────────────


class TestGate:
    def test_gate_hits_common_keyword(self):
        assert eg._PATTERN_GATE_RE.search("plan a budget for june")

    def test_gate_is_case_insensitive(self):
        assert eg._PATTERN_GATE_RE.search("BUDGET planner")

    def test_gate_ignores_keyword_free_text(self):
        assert not eg._PATTERN_GATE_RE.search(
            "the quick brown fox jumps over the lazy dog"
        )

    def test_gate_matches_stems(self):
        assert eg._PATTERN_GATE_RE.search("amortization table for my loan")


# ── Shortlist ranking ───────────────────────────────────────────────


class TestShortlist:
    def test_amortization_brief_shortlists_amortization_first(self):
        brief = "loan amortization schedule for my mortgage"
        shortlist = eg._shortlist_patterns(brief)
        assert shortlist[0] == "amortization"

    def test_no_hits_means_empty(self):
        assert eg._shortlist_patterns("the quick brown fox jumps") == []

    def test_equal_scores_break_alphabetically(self):
        # "budget" and "invoice" each hit exactly one keyword → the
        # deterministic tie-break is name ascending.
        shortlist = eg._shortlist_patterns("budget and invoice")
        assert shortlist == ["budget", "invoice"]

    def test_more_hits_rank_higher(self):
        # amortization hits 3 distinct keywords; budget hits 1.
        brief = "loan amortization schedule for my mortgage and budget"
        shortlist = eg._shortlist_patterns(brief)
        assert shortlist[0] == "amortization"
        assert "budget" in shortlist

    def test_shortlist_capped_at_max(self):
        # Dynamically build a brief hitting the first keyword of 13
        # different patterns — the shortlist must cap at MAX_SHORTLIST.
        names = sorted(ep.PATTERN_KEYWORDS.keys())[:13]
        brief = " ".join(ep.PATTERN_KEYWORDS[n][0] for n in names)
        shortlist = eg._shortlist_patterns(brief)
        assert len(shortlist) == eg.MAX_SHORTLIST == 10

    def test_shortlist_is_deterministic(self):
        brief = "track my workout log, meal plan and budget"
        first = eg._shortlist_patterns(brief)
        second = eg._shortlist_patterns(brief)
        assert first == second

    def test_shortlist_only_contains_registered_patterns(self):
        brief = "loan amortization schedule for my mortgage and budget"
        for name in eg._shortlist_patterns(brief):
            assert name in ep.PATTERN_BUILDERS


# ── Classifier prompt assembly ──────────────────────────────────────


class TestClassifierPromptAssembly:
    def test_prompt_splits_into_header_stanzas_footer(self):
        header, stanzas, footer = eg._parse_classifier_prompt()
        assert header.strip()
        assert footer.strip()
        assert len(stanzas) == len(ep.PATTERN_BUILDERS) + 1  # + "none"

    def test_single_stanza_prompt_is_small(self):
        # The stage-2 saving: one shortlisted stanza keeps the prompt
        # near its six-pattern-era size even with 31 patterns.
        prompt = eg._classifier_system_prompt(["amortization"])
        assert len(prompt) < 6000

    def test_full_prompt_much_larger_than_shortlisted(self):
        header, stanzas, _ = eg._parse_classifier_prompt()
        full = header + "".join(s + "\n\n" for s in stanzas.values())
        single = eg._classifier_system_prompt(["amortization"])
        assert len(full) > 3 * len(single)

    def test_shortlisted_stanza_present_others_absent(self):
        prompt = eg._classifier_system_prompt(["amortization"])
        _, stanzas, _ = eg._parse_classifier_prompt()
        assert stanzas["amortization"].strip()[:80] in prompt
        assert stanzas["invoice"].strip()[:80] not in prompt
        assert stanzas["gradebook"].strip()[:80] not in prompt

    def test_none_stanza_always_included(self):
        prompt = eg._classifier_system_prompt(["amortization"])
        _, stanzas, _ = eg._parse_classifier_prompt()
        assert stanzas["none"].strip()[:60] in prompt

    def test_none_included_even_with_empty_shortlist(self):
        prompt = eg._classifier_system_prompt([])
        _, stanzas, _ = eg._parse_classifier_prompt()
        assert stanzas["none"].strip()[:60] in prompt
        # and no pattern stanza leaked in
        assert stanzas["invoice"].strip()[:80] not in prompt

    def test_footer_rules_included(self):
        # The .md footer carries the JSON output contract — it must
        # survive stanza selection.
        prompt = eg._classifier_system_prompt(["amortization"])
        _, _, footer = eg._parse_classifier_prompt()
        assert footer.strip()[:60] in prompt

    def test_each_shortlisted_stanza_exactly_once(self):
        header, stanzas, _ = eg._parse_classifier_prompt()
        shortlist = ["budget", "invoice", "amortization"]
        prompt = eg._classifier_system_prompt(shortlist)
        for name in shortlist:
            marker_line = f'- "{name}"'
            assert prompt.count(marker_line) == 1, (
                f"stanza {name!r} must appear exactly once"
            )
        # "none" stanza line must also appear exactly once
        assert prompt.count('- "none"') == 1


# ── _try_pattern_spec wiring ─────────────────────────────────────────


class TestTryPatternSpecWiring:
    @pytest.mark.asyncio
    async def test_no_keyword_no_digit_skips_classifier(self, monkeypatch):
        async def must_not_call(messages, model=None):
            raise AssertionError("classifier must not run without a gate hit")

        monkeypatch.setattr(eg, "_call_llm", must_not_call)
        result = await eg._try_pattern_spec(
            "the quick brown fox jumps", "make it pretty"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_digit_only_brief_skips_classifier(self, monkeypatch):
        async def must_not_call(messages, model=None):
            raise AssertionError(
                "digit-only brief with no pattern vocabulary must not reach the classifier"
            )

        monkeypatch.setattr(eg, "_call_llm", must_not_call)
        result = await eg._try_pattern_spec("summarize 3 things about nothing", "")
        assert result is None

    @pytest.mark.asyncio
    async def test_classifier_receives_only_shortlisted_stanzas(
        self, monkeypatch
    ):
        store: list = []
        monkeypatch.setattr(eg, "_call_llm", _capture_llm(store))
        result = await eg._try_pattern_spec(
            "loan amortization schedule for my mortgage", ""
        )
        assert result is None  # captured classifier answered "none"
        system_prompt = store[0]["content"]
        _, stanzas, _ = eg._parse_classifier_prompt()
        assert stanzas["amortization"].strip()[:80] in system_prompt
        assert stanzas["invoice"].strip()[:80] not in system_prompt

    @pytest.mark.asyncio
    async def test_unknown_pattern_falls_back(self, monkeypatch):
        monkeypatch.setattr(
            eg,
            "_call_llm",
            _classify_response({"pattern": "time_machine", "params": {}}),
        )
        result = await eg._try_pattern_spec(
            "loan amortization schedule for my mortgage", ""
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_insufficient_params_fall_back(self, monkeypatch):
        # savings_goal without a monthly contribution → ValueError →
        # AI path, never an exception.
        monkeypatch.setattr(
            eg,
            "_call_llm",
            _classify_response(
                {
                    "pattern": "savings_goal",
                    "params": {
                        "goal_name": "Emergency fund",
                        "target_amount": 10000,
                    },
                }
            ),
        )
        result = await eg._try_pattern_spec("savings goal calculator", "")
        assert result is None

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back(self, monkeypatch):
        monkeypatch.setattr(
            eg, "_call_llm", _classify_response(RuntimeError("llm down"))
        )
        result = await eg._try_pattern_spec(
            "loan amortization schedule for my mortgage", ""
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_garbage_classifier_response_falls_back(self, monkeypatch):
        async def garbage_llm(messages, model=None):
            return "not json at all <<"

        monkeypatch.setattr(eg, "_call_llm", garbage_llm)
        result = await eg._try_pattern_spec(
            "loan amortization schedule for my mortgage", ""
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_non_object_classifier_response_falls_back(self, monkeypatch):
        async def list_llm(messages, model=None):
            return json.dumps(["amortization"])

        monkeypatch.setattr(eg, "_call_llm", list_llm)
        result = await eg._try_pattern_spec(
            "loan amortization schedule for my mortgage", ""
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_matching_pattern_returns_normalized_spec(self, monkeypatch):
        monkeypatch.setattr(
            eg,
            "_call_llm",
            _classify_response(
                {
                    "pattern": "break_even",
                    "params": {
                        "product_name": "Widget",
                        "fixed_costs": 12000,
                        "price_per_unit": 90,
                        "variable_cost_per_unit": 36,
                    },
                }
            ),
        )
        result = await eg._try_pattern_spec(
            "break even analysis for my widget", ""
        )
        assert result is not None
        normalized, name = result
        assert name == "break_even"
        assert isinstance(normalized, dict)
        assert normalized["sheets"]
        # The returned spec is already normalized (formulas populated).
        errors, _ = eg.validate_workbook_spec(normalized)
        assert errors == []

    @pytest.mark.asyncio
    async def test_requirements_forwarded_to_classifier(self, monkeypatch):
        store: list = []
        monkeypatch.setattr(eg, "_call_llm", _capture_llm(store))
        await eg._try_pattern_spec(
            "loan amortization schedule for my mortgage",
            "highlight the final payment row",
        )
        user_content = store[1]["content"]
        assert "highlight the final payment row" in user_content
        assert "Request:" in user_content


# ── End-to-end: a NEW pattern routes through the full pipeline ───────


class TestNewPatternEndToEndRouting:
    @pytest.mark.asyncio
    async def test_break_even_routes_through_generate_spreadsheet(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            eg,
            "_call_llm",
            _classify_response(
                {
                    "pattern": "break_even",
                    "params": {
                        "product_name": "Sourdough loaf",
                        "fixed_costs": 2500,
                        "price_per_unit": 7,
                        "variable_cost_per_unit": 2.5,
                    },
                }
            ),
        )

        async def must_not_run(brief, requirements, model=None):
            raise AssertionError("AI path must not run when pattern matches")

        monkeypatch.setattr(eg, "_generate_workbook_json", must_not_run)
        monkeypatch.setattr(eg, "_get_reports_dir", lambda: tmp_path)
        result = await eg.generate_spreadsheet(
            "break even analysis for my bakery's sourdough loaf"
        )
        assert result["pattern"] == "break_even"
        assert (tmp_path / f"{result['report_id']}.xlsx").exists()
        assert "break_even template" in result["summary"]
