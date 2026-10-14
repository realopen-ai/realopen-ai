"""Execution facts and preflight validation do not depend on model summaries."""

import json

import pytest

from app.agent.execution_state import ExecutionState
from app.agent.validation import argument_errors, normalize_structured_arguments
from app.learn.quizzes import QUESTION_SCHEMA
from app.services.context_relevance import relevant_context, topic_tokens


def test_execution_ledger_preserves_success_ids_and_latest_failures():
    state = ExecutionState()
    state.record(
        {"id": "failed", "tool": "create"},
        False,
        "questions.2.options: minItems=2",
        validation=True,
    )
    state.record(
        {"id": "saved", "tool": "create"},
        True,
        json.dumps(
            {"quiz_id": "returned-id", "title": "Topic", "questions": ["huge body"]}
        ),
    )
    state.record({"id": "other", "tool": "read"}, False, "permission denied")
    msg = state.message()
    assert "returned-id" in msg["content"]
    assert "permission denied" in msg["content"]
    assert "huge body" not in msg["content"]
    assert "not a new user request" in msg["content"]
    assert msg["metadata"]["agent_context"]
    assert state.validation_failures["create"] == 0
    assert "create" not in state.failures
    assert state.attempt_counts["create"] == {"successful": 1, "failed": 1}


def test_ledgers_are_request_local():
    first, second = ExecutionState(), ExecutionState()
    first.record({"id": "a", "tool": "x"}, True, "success")
    assert second.message() is None


def test_nested_result_and_deliverable_ids_survive_without_bodies():
    state = ExecutionState()
    state.record(
        {"id": "a", "tool": "export"},
        True,
        '{"artifact":{"id":"nested-id","version":2,"content":"secret long document body"}}\nAdditional metadata',
        generated=[
            {
                "report_id": "report-id",
                "artifact_id": "registered-id",
                "content": "binary body",
            }
        ],
    )
    content = state.message()["content"]
    assert all(
        identifier in content
        for identifier in ("nested-id", "report-id", "registered-id")
    )
    assert "secret long document body" not in content and "binary body" not in content


def test_list_outputs_retain_identifiers():
    state = ExecutionState()
    state.record(
        {"id": "a", "tool": "generate"},
        True,
        '[{"id":"existing-result","content":"not copied"}]',
    )
    assert "existing-result" in state.message()["content"]
    assert "not copied" not in state.message()["content"]


@pytest.mark.parametrize(
    "value,error",
    [
        (
            {"type": "multiple_choice", "prompt": "Q", "explanation": "E"},
            "correct_option",
        ),
        (
            {
                "type": "multiple_choice",
                "prompt": "Q",
                "explanation": "E",
                "options": ["one"],
                "correct_option": 0,
            },
            "minItems=2",
        ),
        ({"type": "short_answer", "prompt": "Q", "explanation": "E"}, "answer"),
        (
            {
                "type": "multiple_choice",
                "prompt": "Q",
                "explanation": "E",
                "options": ["one", "two"],
                "correct_option": True,
            },
            "expected integer",
        ),
    ],
)
def test_nested_schema_errors_are_actionable(value, error):
    errors = argument_errors(
        [value], {"type": "array", "items": QUESTION_SCHEMA}, "arguments.questions"
    )
    assert any(error in message for message in errors)
    assert all("arguments.questions.0" in message for message in errors)


def test_valid_question_and_optional_nulls_are_accepted():
    question = {
        "type": "multiple_choice",
        "prompt": "Q",
        "options": ["one", "two"],
        "correct_option": 0,
        "explanation": "E",
        "source_document_id": None,
    }
    assert argument_errors(question, QUESTION_SCHEMA) == []


def test_preflight_does_not_rewrite_or_decode_arguments():
    value = {"questions": '[{"prompt":"Q"}]'}
    assert argument_errors(
        value, {"type": "object", "properties": {"questions": {"type": "array"}}}
    )
    assert value["questions"] == '[{"prompt":"Q"}]'


def test_schema_based_decoding_is_lossless_and_retains_original_attempt():
    original = {"items": '[{"config":"{\\"value\\":3}","front":"123"}]'}
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "config": {"type": "object"},
                        "front": {"type": "string"},
                    },
                },
            }
        },
    }
    normalized = normalize_structured_arguments(original, schema)
    assert normalized == {"items": [{"config": {"value": 3}, "front": "123"}]}
    assert isinstance(original["items"], str)
    assert argument_errors(normalized, schema) == []


@pytest.mark.parametrize("value", ["{bad", '"still a string"', "1", "null"])
def test_decoding_never_fabricates_invalid_structured_arguments(value):
    assert normalize_structured_arguments(value, {"type": "array"}) == value


def test_nullable_schema_types_are_not_coerced_or_crashed():
    schema = {"type": ["string", "null"]}
    assert normalize_structured_arguments("literal", schema) == "literal"
    assert argument_errors(None, schema) == []


def test_automatic_context_rejects_request_boilerplate_and_weak_unrelated_matches():
    query = "Make for me a quiz with 5 multiple-choice questions about NumPy basics. Include Python code snippets."
    assert not relevant_context(query, "Convert the invoice to Excel", 0.7)
    assert relevant_context(query, "Previous NumPy array shapes", 0.7)
    assert relevant_context("Explain neural attention", "Mécanisme d'attention", 0.85)
    assert not topic_tokens("Hello!")
    assert not relevant_context("Hello!", "Old invoices", 0.99)
