"""Argument rebuild shape, context preservation and capability negotiation."""

import json
from copy import deepcopy

import httpx
import pytest

from app.agent.argument_repair import (
    native_rebuild,
    parse_rebuild,
    rebuild_view,
    required_shape,
    response_schema,
    conditional_shapes,
    field_repair,
)
from app.agent.protocol import assistant_call_message, tool_result_message
from app.agent.tools.quizzes import CreateQuizTool
from app.agent.tools.flashcards import CreateFlashcardDeckTool
from app.agent.tools.notes import CreateStudyNoteTool
from app.services import providers, structured_outputs
from app.config import settings


def schema(tool):
    return {
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": {
                "type": "object",
                "properties": tool.get_parameters(),
                "required": tool.get_required_params(),
            },
        }
    }


@pytest.mark.parametrize(
    "tool", [CreateQuizTool(), CreateFlashcardDeckTool(), CreateStudyNoteTool()]
)
def test_rebuild_schema_has_exact_tool_and_real_nested_types(tool):
    original = schema(tool)
    snapshot = deepcopy(original)
    decoder = response_schema(original)
    assert decoder["properties"]["tool"]["enum"] == [tool.name]
    assert decoder["properties"]["args"] == original["function"]["parameters"]
    assert decoder["additionalProperties"] is False
    shape = required_shape(decoder["properties"]["args"])
    assert set(original["function"]["parameters"]["required"]) <= set(shape)
    assert original == snapshot


def test_shape_separates_conditional_required_fields_not_source_ids():
    parameters = schema(CreateQuizTool())["function"]["parameters"]
    shape = required_shape(parameters)
    item = shape["questions"][0]
    assert set(item) == {"type", "prompt", "explanation"}
    variants = {
        example["when"]["type"]: example for example in conditional_shapes(parameters)
    }
    mc = variants["multiple_choice"]["shape"]
    short = variants["short_answer"]["shape"]
    assert set(mc) == {"type", "prompt", "explanation", "options", "correct_option"}
    assert isinstance(mc["options"], list)
    assert mc["type"] == "multiple_choice"
    assert set(short) == {"type", "prompt", "explanation", "answer"}
    assert short["type"] == "short_answer"
    assert "source_document_id" not in item
    assert "source_artifact" not in item


def test_rebuild_view_keeps_context_successes_and_untrusted_errors_not_bad_payload():
    rejected = {
        "id": "bad",
        "tool": "create_quiz",
        "args": {"questions": "BROKEN_PAYLOAD"},
    }
    completed = {
        "id": "ok",
        "tool": "read_artifact",
        "args": {"artifact_id": "real-id"},
    }
    source = {
        "role": "user",
        "content": "Source data",
        "metadata": {"agent_context": True},
    }
    messages = [
        {"role": "system", "content": "Keep security and notebook rules"},
        {"role": "user", "content": "Create four questions from that source"},
        source,
        assistant_call_message("", [completed]),
        tool_result_message(completed, "Retrieved content"),
        assistant_call_message("", [rejected], "Repeat BROKEN_PAYLOAD"),
        tool_result_message(rejected, "arguments.questions: expected array"),
    ]
    original = deepcopy(messages)
    view = rebuild_view(
        messages,
        schema(CreateQuizTool()),
        {"bad"},
        "arguments.questions: expected array; received BROKEN_PAYLOAD",
    )
    assert messages == original
    assert "BROKEN_PAYLOAD" not in json.dumps(view)
    assert source in view
    assert "Keep security and notebook rules" in view[0]["content"]
    assert any(m.get("tool_call_id") == "ok" for m in view)
    assert not any(m.get("tool_call_id") == "bad" for m in view)
    assert "arguments.questions: expected array" in view[-2]["content"]
    assert "UNTRUSTED_SOURCE_DATA" in view[-2]["content"]
    assert "ARGUMENT REBUILD ROUND" in view[-1]["content"]


def test_parallel_success_is_retained_while_failed_call_and_thinking_are_removed():
    calls = [
        {"id": "bad", "tool": "create_quiz", "args": {"questions": "BROKEN"}},
        {"id": "ok", "tool": "read_artifact", "args": {"artifact_id": "real-id"}},
    ]
    view = rebuild_view(
        [
            assistant_call_message("", calls, "BROKEN thinking"),
            tool_result_message(calls[0], "bad"),
            tool_result_message(calls[1], "source"),
        ],
        schema(CreateQuizTool()),
        {"bad"},
        "expected array",
    )
    retained = next(m for m in view if m.get("tool_calls"))
    assert [c["id"] for c in retained["tool_calls"]] == ["ok"]
    assert retained["thinking"] == ""
    assert "BROKEN" not in json.dumps(view)


@pytest.mark.parametrize(
    "text",
    [
        "Not JSON",
        '{"tool":"other","args":{}}',
        '{"tool":"create_quiz","args":"{}"}',
        '{"tool":"create_quiz","args":{},"extra":1}',
        '{"tool":"create_quiz","args":{}} trailing',
    ],
)
def test_bad_rebuilds_cannot_change_tool_or_invent_missing_structure(text):
    call = parse_rebuild(text, "create_quiz")
    assert call["tool"] == "create_quiz"
    assert call["args"] == {}
    assert call["parse_error"]


@pytest.mark.parametrize("wrapper", ["{}", "```json\n{}\n```", "```tool\n{}\n```"])
def test_rebuild_parse_preserves_code_quotes_and_unicode(wrapper):
    value = {
        "tool": "create_study_note",
        "args": {"title": "ملاحظة", "content": '```python\nprint(r"\\n")\n```'},
    }
    call = parse_rebuild(wrapper.format(json.dumps(value)), "create_study_note")
    assert call["args"] == value["args"]
    assert "parse_error" not in call


def test_native_compatible_reply_cannot_execute_parallel_or_other_tools():
    for calls in (
        [],
        [{"tool": "other", "args": {}}],
        [{"tool": "create_quiz", "args": {}}] * 2,
    ):
        assert "parse_error" in native_rebuild(calls, "create_quiz")


def sample_repair():
    original = {
        "title": "Unchanged title",
        "questions": [
            {
                "type": "short_answer",
                "prompt": 'What prints?```python\nprint(r"\\n")\n```',
                "answer": "Literal escape",
                "explanation": "Unchanged",
            },
            {
                "type": "multiple_choice",
                "prompt": "Untouched question",
                "options": ["A", "B"],
                "correct_option": 1,
                "explanation": "Keep this",
            },
        ],
    }
    repair = field_repair(
        original,
        schema(CreateQuizTool())["function"]["parameters"],
        [
            {
                "path": ["questions", 0, "prompt"],
                "message": "Opening fence needs its own line",
            },
        ],
    )
    assert repair is not None
    return original, repair


def test_field_corrections_preserve_unaffected_arguments_and_literal_code():
    original, repair = sample_repair()
    snapshot = deepcopy(original)
    fixed = 'What prints?\n\n```python\nprint(r"\\n")\n```'
    text = json.dumps(
        {
            "tool": "create_quiz",
            "corrections": [{"path": "/questions/0/prompt", "value": fixed}],
        }
    )
    call = parse_rebuild(text, "create_quiz", repair)
    assert "parse_error" not in call
    assert call["args"]["questions"][0]["prompt"] == fixed
    assert call["args"]["title"] == original["title"]
    assert call["args"]["questions"][1:] == original["questions"][1:]
    assert call["args"]["questions"][0]["answer"] == original["questions"][0]["answer"]
    assert original == snapshot and repair.arguments == snapshot
    decoder = response_schema(schema(CreateQuizTool()), repair)
    assert set(decoder["properties"]) == {"tool", "corrections"}
    assert (
        decoder["properties"]["corrections"]["items"]["anyOf"][0]["properties"][
            "value"
        ]["type"]
        == "string"
    )


@pytest.mark.parametrize(
    "corrections",
    [
        [],
        [{"path": "/title", "value": "Hijacked"}],
        [{"path": "/questions/0/prompt", "value": "Fixed"}] * 2,
        [{"path": "/questions/0/prompt", "value": 42}],
        [{"path": "/questions/-1/prompt", "value": "Bad index"}],
        [{"path": "/questions/0/prompt", "value": "Fixed", "op": "remove"}],
    ],
)
def test_unsafe_incomplete_and_wrong_type_corrections_cannot_execute(corrections):
    original, repair = sample_repair()
    call = parse_rebuild(
        json.dumps({"tool": "create_quiz", "corrections": corrections}),
        "create_quiz",
        repair,
    )
    assert call["parse_error"] and call["args"] == {}
    assert call["repair_mode"] == "fields"
    assert repair.arguments == original


def test_unknown_root_missing_and_overlapping_issue_paths_need_rebuilding():
    original, _ = sample_repair()
    parameters = schema(CreateQuizTool())["function"]["parameters"]
    for paths in [
        [],
        [[]],
        [["invented"]],
        [["questions", 30, "prompt"]],
        [["questions", 0], ["questions", 0, "prompt"]],
        [["questions", True]],
    ]:
        assert (
            field_repair(original, parameters, [{"path": path} for path in paths])
            is None
        )


def test_repair_context_isolates_protocol_and_retains_full_writing_guidance():
    messages = [
        {"role": "system", "content": "Core security policy"},
        {
            "role": "user",
            "content": "Use native tools and discovery",
            "metadata": {"agent_tool_protocol": True},
        },
        {"role": "user", "content": "Keep source", "metadata": {"agent_context": True}},
        assistant_call_message(
            "",
            [
                {
                    "id": "deferred",
                    "tool": "create_quiz",
                    "args": {"query": "GUESSED_SCHEMA"},
                }
            ],
        ),
        tool_result_message(
            {"id": "deferred", "tool": "create_quiz"}, "Schema loaded, no execution"
        ),
    ]
    original = deepcopy(messages)
    _, repair = sample_repair()
    view = rebuild_view(
        messages, schema(CreateQuizTool()), {"deferred"}, "Misplaced fence", repair
    )
    joined = json.dumps(view)
    assert (
        "Use native tools and discovery" not in joined
        and "GUESSED_SCHEMA" not in joined
    )
    assert "Keep source" in joined
    assert view[0] == messages[0] and messages == original
    assert "Code-block example" in view[-1]["content"]
    assert "INSIDE string values" in view[-1]["content"]
    assert "do not escape apostrophes" in view[-1]["content"]
    assert "Unchanged title" not in joined and "Untouched question" not in joined
    assert "UNTRUSTED_SOURCE_DATA" in view[-2]["content"]


@pytest.mark.parametrize(
    "status,message,expected",
    [
        (400, "JSON schema format is not supported", True),
        (
            422,
            "cannot unmarshal object into Go struct field ChatRequest.format of type string",
            True,
        ),
        (400, "Invalid schema: bad properties", False),
        (401, "structured outputs not supported", False),
        (503, "format is unsupported", False),
        (400, "Model does not support tools", False),
    ],
)
def test_only_explicit_schema_protocol_rejections_trigger_fallback(
    status, message, expected
):
    error = httpx.HTTPStatusError(
        "failed",
        request=httpx.Request("POST", "http://local/api/chat"),
        response=httpx.Response(status, text=message),
    )
    assert structured_outputs.schema_rejected(error) is expected


def test_schema_rejection_is_endpoint_and_model_scoped(monkeypatch):
    monkeypatch.setattr(structured_outputs, "_unsupported", set())
    monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "http://first")
    assert structured_outputs.should_try_schema("model")
    structured_outputs.remember_rejection("model")
    assert not structured_outputs.should_try_schema("model")
    assert structured_outputs.should_try_schema("other")
    monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "http://second")
    assert structured_outputs.should_try_schema("model")
    monkeypatch.setattr(providers, "is_groq_model", lambda model: model == "cloud")
    assert not structured_outputs.should_try_schema("cloud")
