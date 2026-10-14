"""Quiz Markdown validation must not modify program text or literal escapes."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.agent.tools.quizzes import CreateQuizTool
from app.learn.quizzes import Question
from app.learn.quiz_generation import GenerateQuiz, from_note
from app.prompts import format_prompt
from app.services import model_prefs, providers


@pytest.mark.parametrize(
    "text",
    [
        'What prints?\n\n```python\nprint(r"\\n")\n```',
        'What prints?\n\n```python\nprint("A) one B) two")\n```',
        'What prints?\n\n```python\nif True:\n    print("\\n")\n```',
        '```javascript\nconst sample = "```python\\n";\n```',
        "~~~sql\nSELECT 1;\n~~~",
        "````markdown\n```python\nprint(1)\n```\n````",
        "```\nplain code\n```",
        "`np.zeros(3)` creates an array. Literal `\\n` is an escape.",
        "Inline ```code``` is not a fenced block.",
    ],
)
def test_preserve_valid_markdown_and_code(text):
    # Exercise the real transport and shared API/tool validation boundary.
    data = json.loads(
        json.dumps({"type": "short_answer", "prompt": text, "answer": "Result"})
    )
    assert Question.model_validate(data).prompt == text


@pytest.mark.parametrize(
    "text",
    [
        r"What prints?\n\n```python\nprint(1)\n```",
        "What prints?\n\n```python\\nprint(1)\\n```",
        "What prints?\n\n```python\nprint(1)```",
        "What prints?\n\n```python print(1)```",
        "What prints?\n\n```python\nprint(1)",
        "What prints?\n\n```python\nprint(1)\n~~~",
        "What prints?\n\n````python\nprint(1)\n```",
    ],
)
def test_reject_ambiguous_layout_instead_of_rewriting_code(text):
    with pytest.raises(ValidationError, match="line|newline"):
        Question(type="short_answer", prompt=text, answer="Result")


@pytest.mark.parametrize("field", ["prompt", "answer", "explanation", "options"])
def test_validate_all_rendered_question_fields(field):
    data = {
        "type": "multiple_choice",
        "prompt": "What prints?",
        "options": ["1", "2"],
        "correct_option": 0,
    }
    malformed = "```python\nprint(1)```"
    data[field] = [malformed, "2"] if field == "options" else malformed
    with pytest.raises(ValidationError) as error:
        Question.model_validate(data)
    assert error.value.errors()[0]["loc"] == (field,)


@pytest.mark.asyncio
async def test_tool_returns_actionable_markdown_repair_feedback():
    result = await CreateQuizTool().execute(
        title="Code quiz",
        questions=[
            {
                "type": "short_answer",
                "prompt": r"What prints?\n\n```python\nprint(1)\n```",
                "answer": "1",
            }
        ],
    )
    assert not result.success
    assert result.failure_kind == "validation"
    assert "questions.0.prompt" in result.output
    assert "real line breaks" in result.output


def test_shared_writing_prompt_demonstrates_decoded_multiline_markdown():
    guidance = format_prompt("quiz_writing")
    assert "```python\nx = [1, 2]\nprint(len(x))\n```" in guidance
    assert "encode line breaks once" in guidance


@pytest.mark.parametrize("fence", ["```python", "~~~~sql", "````javascript"])
def test_inline_opening_fence_reports_correct_location_not_missing_closer(fence):
    prompt = (
        f"What prints?{fence}\nprint(1)\n{fence.rstrip('abcdefghijklmnopqrstuvwxyz')}"
    )
    with pytest.raises(ValidationError) as error:
        Question(type="short_answer", prompt=prompt, answer="1")
    message = error.value.errors()[0]["msg"]
    assert "Misplaced opening code fence at line 1, column 13" in message
    assert "newline before the opening fence" in message
    assert "Unclosed" not in message


@pytest.mark.asyncio
async def test_tool_reports_structured_paths_for_safe_targeted_correction():
    result = await CreateQuizTool().execute(
        title="Formatting",
        questions=[
            {
                "type": "short_answer",
                "prompt": "What prints?```python\nprint(1)\n```",
                "answer": "1",
            }
        ],
    )
    assert result.validation_issues[0]["path"] == ["questions", 0, "prompt"]
    assert "Misplaced opening" in result.validation_issues[0]["message"]


def test_shared_example_is_valid_and_used_by_the_agent():
    guidance = format_prompt("quiz_writing")
    example = json.loads(guidance.split("```json\n", 1)[1].split("\n```", 1)[0])
    Question.model_validate(example["questions"][0])
    assert guidance in CreateQuizTool.description


@pytest.mark.parametrize(
    "suffix",
    [
        " Options: A) First value, B) Other value",
        " Options A) First value B) Other value",
        " Choices: A. First value B. Other value",
        "\nAnswer choices:\nA) First value\nB) Other value",
        "\nOPTIONS: A) First value B) Other value",
    ],
)
def test_remove_unambiguous_list_heading_and_paraphrased_choices(suffix):
    stem = 'What prints?\n\n```python\nprint("A) one B) two")\n```\n\nWhich result?'
    question = Question(
        type="multiple_choice",
        prompt=stem + suffix,
        options=["First", "Second"],
        correct_option=1,
    )
    assert question.prompt == stem
    assert question.options == ["First", "Second"]
    assert question.correct_option == 1


@pytest.mark.parametrize(
    "stem,suffix",
    [
        ("What value? Choose one:", " Options A) 1 B) 2"),
        ("Calculate this value.", "\nOptions: A) 1 B) 2"),
        ("Which output follows?", " Options: A) (2, 4), B) (4,)"),
    ],
)
def test_keep_question_instructions_before_choice_heading(stem, suffix):
    question = Question(
        type="multiple_choice",
        prompt=stem + suffix,
        options=["First", "Second"],
        correct_option=0,
    )
    assert question.prompt == stem


@pytest.mark.parametrize(
    "prompt",
    [
        "Discuss options A) first B) second in the diagram.",
        "Choices: A) first B) second",
        "Which? Options: A) first C) second",
        "Which? Options: A) first B. second",
        "Which? Options: A) first B) second C) third",
        "Which? Options: A) first B) second\nExplain why.",
        "Which? Options: A) first B) second What else?",
        "Which? Options: A) first B) ",
    ],
)
def test_keep_rejecting_ambiguous_choice_lists(prompt):
    with pytest.raises(ValidationError, match="Keep answer choices only in options"):
        Question(
            type="multiple_choice",
            prompt=prompt,
            options=["First", "Second"],
            correct_option=0,
        )


@pytest.mark.parametrize(
    "opening,closing", [("````python", "`````"), ("~~~python", "~~~~")]
)
def test_choice_cleanup_preserves_longer_fences_and_code_labels(opening, closing):
    stem = f'Which result?\n\n{opening}\nprint("Options: A) one B) two")\n{closing}'
    question = Question(
        type="multiple_choice",
        prompt=stem + "\nOptions: A) one B) two",
        options=["One", "Two"],
        correct_option=0,
    )
    assert question.prompt == stem


@pytest.mark.parametrize(
    "options",
    [
        ["First\nSecond\nThird", "Second"],
        ["First", "First\nSecond\nThird"],
        [
            "Returns an error\nAdds to each element\nCreates a dimension",
            "Adds to each element",
        ],
        ["\nFirst\n  SECOND  \nThird\n", "second"],
        ["First\r\nSecond\r\nThird", "Second"],
        ["First\nSecond", "Second"],
    ],
)
def test_reject_bundled_choices_without_reindexing(options):
    original = list(options)
    with pytest.raises(ValidationError, match="bundled answer choices") as error:
        Question(
            type="multiple_choice",
            prompt="Which answer?",
            options=options,
            correct_option=1,
        )
    assert "repaired array" in str(error.value)
    assert options == original


@pytest.mark.parametrize(
    "options",
    [
        ["A response spanning\nseveral lines of prose.", "A different response."],
        ["First\nSecond\nThird", "Fourth"],
        ["First and Second\nA supporting explanation", "Second"],
        ["```text\nFirst\nSecond\nThird\n```", "Second"],
        ["~~~text\nFirst\nSecond\nThird\n~~~", "Second"],
        ['```python\nif True:\n    print("Second")\n```', "Second"],
        ["```python\nprint('a  b')\n```", "```python\nprint('a b')\n```"],
    ],
)
def test_preserve_independent_multiline_and_code_choices(options):
    question = Question(
        type="multiple_choice",
        prompt="Which answer?",
        options=options,
        correct_option=1,
    )
    assert question.options == options
    assert question.correct_option == 1


@pytest.mark.asyncio
async def test_tool_rejects_bundled_choices_with_schema_repair_feedback():
    result = await CreateQuizTool().execute(
        title="Options quiz",
        questions=[
            {
                "type": "multiple_choice",
                "prompt": "Which answer?",
                "options": ["First\nSecond\nThird", "Second"],
                "correct_option": 1,
                "explanation": "The second value is correct.",
            }
        ],
    )
    assert not result.success
    assert result.failure_kind == "validation"
    assert "questions.0" in result.output
    assert "options.0 contains options.1" in result.output
    assert "correct_option" in result.output


@pytest.mark.asyncio
async def test_note_generation_repairs_bundled_choices(monkeypatch):
    invalid = {
        "title": "Colors",
        "questions": [
            {
                "type": "multiple_choice",
                "prompt": "Which color is a primary color of light?",
                "options": ["Red\nYellow\nPurple", "Red"],
                "correct_option": 1,
                "explanation": "Red is a primary color of light.",
            }
        ],
    }
    valid = json.loads(json.dumps(invalid))
    valid["questions"][0].update(options=["Red", "Yellow", "Purple"], correct_option=0)
    chat = AsyncMock(
        side_effect=[
            {"message": {"content": json.dumps(invalid)}},
            {"message": {"content": json.dumps(valid)}},
        ]
    )
    monkeypatch.setattr(
        model_prefs, "resolve_task_model", AsyncMock(return_value="local")
    )
    monkeypatch.setattr(providers, "chat_once", chat)
    note = SimpleNamespace(
        title="Colors",
        content="Red, green, and blue are the primary colors of light.",
        id=None,
        source_conversation_id=None,
        source_document_id=None,
        source_page=None,
        source_chunk_id=None,
        source_artifact=None,
    )
    result = await from_note(note, GenerateQuiz(count=1, type="multiple_choice"))
    assert result.questions[0].options == ["Red", "Yellow", "Purple"]
    assert result.questions[0].correct_option == 0
    assert chat.await_count == 2
    assert "bundled answer choices" in chat.call_args.args[1][-1]["content"]


def test_prompt_and_schema_require_one_choice_per_array_entry():
    guidance = format_prompt("quiz_writing")
    assert "One options entry is one independent answer" in guidance
    assert "one answer per array entry" in (
        CreateQuizTool()
        .get_parameters()["questions"]["items"]["properties"]["options"]["description"]
        .lower()
    )
