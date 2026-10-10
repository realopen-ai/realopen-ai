import uuid
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import pytest
from pydantic import ValidationError

from app.db.models import QuizAttempt
from app.learn.quizzes import QuizInput, Question, attempt_dict
from app.learn.quiz_grading import grade
from app.services import model_prefs, providers
from app.api.quizzes import router
from app.db.session import get_db
from app.db.models import Flashcard, FlashcardProgress, FlashcardReview
from app.db.models import Conversation, StudyNote, Document
from app.learn.quiz_generation import GenerateQuiz, from_note
from app.agent.tools.quizzes import CreateQuizTool
from app.agent.tools.tool_defaults import default_tags
import test_notebooks as notebook_tests
from test_flashcards import AsyncAdapter

database = notebook_tests.database


@pytest.mark.asyncio
async def test_grading_bounds_context_without_truncating_answers(monkeypatch):
    question = {"id": "large", "type": "short_answer", "prompt": "q" * 5000, "answer": "a" * 5000}
    chat = AsyncMock()
    monkeypatch.setattr(providers, "chat_once", chat)
    result = await grade([question], {"large": "b" * 5000})
    assert result["large"]["correct"] is None
    chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_note_generation_model_resolution_failure_is_safe(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(
        model_prefs, "resolve_task_model", AsyncMock(side_effect=RuntimeError("offline"))
    )
    with pytest.raises(HTTPException) as error:
        await from_note(
            SimpleNamespace(title="Note", content="A triangle has three sides."), GenerateQuiz()
        )
    assert error.value.status_code == 502


@pytest.mark.asyncio
async def test_tool_validation_feedback_identifies_missing_fields():
    result = await CreateQuizTool().execute(title="Invalid", questions=[{"type": "short_answer"}])
    assert not result.success
    assert "questions.0.prompt" in result.output


@pytest.fixture
def client(database):
    for model in (QuizAttempt, Flashcard, FlashcardProgress, FlashcardReview):
        model.__table__.create(database)
    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def dependency():
        with Session(database, expire_on_commit=False) as session:
            try:
                yield AsyncAdapter(session)
                session.commit()
            except Exception:
                session.rollback()
                raise

    app.dependency_overrides[get_db] = dependency
    with TestClient(app) as result:
        yield result


def create(client):
    payload = {
        "title": "Math",
        "questions": [
            {
                "type": "multiple_choice",
                "prompt": "What is 2 + 2?",
                "options": ["Three", "Four"],
                "correct_option": 1,
                "explanation": "Two pairs",
            },
            {
                "type": "short_answer",
                "prompt": "Name the shape with three sides",
                "answer": "Triangle",
                "explanation": "Three edges",
            },
        ],
    }
    response = client.post("/api/learn/quizzes", json=payload)
    assert response.status_code == 201, response.text
    return response.json(), payload


def test_rest_snapshot_revision_and_grading(client):
    quiz, payload = create(client)
    path = f"/api/learn/quizzes/{quiz['id']}"
    assert "answer" not in quiz["questions"][0]
    attempt = client.post(path + "/attempts").json()
    questions = attempt["snapshot"]["questions"]
    assert "correct_option" not in questions[0]
    answers = {questions[0]["id"]: 0, questions[1]["id"]: "TRIANGLE"}
    draft = client.put(path + f"/attempts/{attempt['id']}/answers", json={"answers": answers})
    assert draft.status_code == 200
    assert draft.json()["results"] is None
    payload["questions"][0]["correct_option"] = 0
    assert client.put(path, json=payload).status_code == 409
    payload["expected_revision"] = 1
    assert client.put(path, json=payload).status_code == 200
    result = client.post(path + f"/attempts/{attempt['id']}/submit", json={"answers": answers})
    assert result.status_code == 200, result.text
    result = result.json()
    assert result["score"] == 1
    assert result["snapshot"]["revision"] == 1
    assert result["snapshot"]["questions"][0]["correct_option"] == 1
    repeat = client.post(path + f"/attempts/{attempt['id']}/submit", json={"answers": {}}).json()
    assert repeat == result
    assert (
        client.put(path + f"/attempts/{attempt['id']}/answers", json={"answers": {}}).status_code
        == 409
    )


def test_attempt_boundaries_and_override(client):
    quiz, _ = create(client)
    other, _ = create(client)
    path = f"/api/learn/quizzes/{quiz['id']}"
    attempt = client.post(path + "/attempts").json()
    assert (
        client.get(f"/api/learn/quizzes/{other['id']}/attempts/{attempt['id']}").status_code == 404
    )
    question = attempt["snapshot"]["questions"][1]["id"]
    override = path + f"/attempts/{attempt['id']}/override/{question}"
    assert client.put(override, json={"correct": True}).status_code == 409
    assert (
        client.post(
            path + f"/attempts/{attempt['id']}/submit", json={"answers": {"invented": "x"}}
        ).status_code
        == 422
    )
    client.post(path + f"/attempts/{attempt['id']}/submit", json={"answers": {}})
    assert client.put(override, json={"correct": True}).json()["score"] == 1
    assert client.put(override, json={"correct": False}).json()["score"] == 0


def test_mistakes_preview_and_idempotent_conversion(client):
    quiz, _ = create(client)
    path = f"/api/learn/quizzes/{quiz['id']}"
    attempt = client.post(path + "/attempts").json()
    target = path + f"/attempts/{attempt['id']}"
    assert client.get(target + "/flashcards").status_code == 409
    client.post(target + "/submit", json={"answers": {}})
    cards = client.get(target + "/flashcards").json()
    assert cards[0]["back"] == "Four\n\nTwo pairs"
    body = {"title": "Math review", "question_ids": [c["question_id"] for c in cards]}
    result = client.post(target + "/flashcards", json=body)
    assert result.status_code == 201, result.text
    assert len(result.json()["cards"]) == 2
    assert client.post(target + "/flashcards", json=body).json()["id"] == result.json()["id"]


def test_invalid_sources_and_question_types(client):
    _, payload = create(client)
    payload["questions"][0]["source_document_id"] = str(uuid.uuid4())
    assert client.post("/api/learn/quizzes", json=payload).status_code == 404
    payload["questions"][0].pop("source_document_id")
    payload["questions"][0]["correct_option"] = True
    assert client.post("/api/learn/quizzes", json=payload).status_code == 422


def test_editor_history_delete_and_resumable_start(client):
    quiz, _ = create(client)
    path = f"/api/learn/quizzes/{quiz['id']}"
    assert client.get(path + "/editor").json()["questions"][0]["correct_option"] == 1
    first = client.post(path + "/attempts").json()
    assert client.post(path + "/attempts").json()["id"] == first["id"]
    assert client.get(path + "/attempts").json()[0]["id"] == first["id"]
    assert client.get("/api/learn/quizzes?q=Math").json()[0]["id"] == quiz["id"]
    assert client.delete(path).status_code == 204
    assert client.get(path).status_code == 404
    assert client.get(path + f"/attempts/{first['id']}").status_code == 404


def test_question_answer_type_rejection(client):
    quiz, _ = create(client)
    path = f"/api/learn/quizzes/{quiz['id']}"
    attempt = client.post(path + "/attempts").json()
    mc, short = attempt["snapshot"]["questions"]
    for answers in ({mc["id"]: True}, {mc["id"]: 9}, {short["id"]: 2}, {short["id"]: "x" * 2001}):
        assert (
            client.post(
                path + f"/attempts/{attempt['id']}/submit", json={"answers": answers}
            ).status_code
            == 422
        )


@pytest.mark.asyncio
async def test_note_generation_is_focused_and_preserves_trusted_origin(monkeypatch):
    note = SimpleNamespace(
        id=uuid.uuid4(),
        title="Shapes",
        content="A triangle has three sides. Unrelated section.",
        source_conversation_id=uuid.uuid4(),
        source_document_id=None,
        source_page=None,
        source_chunk_id=None,
        source_artifact=None,
    )
    response = {
        "title": "Shapes",
        "questions": [
            {
                "type": "short_answer",
                "prompt": "How many sides does a triangle have?",
                "answer": "Three",
                "source_document_id": "invented",
            }
        ],
    }
    mock = AsyncMock(return_value={"message": {"content": json.dumps(response)}})
    monkeypatch.setattr(providers, "chat_once", mock)
    monkeypatch.setattr(model_prefs, "resolve_task_model", AsyncMock(return_value="local"))
    result = await from_note(note, GenerateQuiz(count=1, selection="A triangle has three sides."))
    assert result.source_note_id == note.id
    assert result.source_conversation_id == note.source_conversation_id
    assert result.questions[0].source_document_id is None
    assert "Unrelated section" not in mock.call_args.args[1][1]["content"]
    with pytest.raises(Exception, match="Selection no longer"):
        await from_note(note, GenerateQuiz(selection="Invented selection"))


@pytest.mark.asyncio
async def test_note_generation_route(client, database, monkeypatch):
    with Session(database) as session:
        conv = Conversation(title="Origin")
        session.add(conv)
        session.flush()
        note = StudyNote(
            title="Math", content="Two plus two is four.", source_conversation_id=conv.id
        )
        session.add(note)
        session.commit()
        note_id, conv_id = note.id, conv.id
    monkeypatch.setattr(model_prefs, "resolve_task_model", AsyncMock(return_value="local"))
    monkeypatch.setattr(
        providers,
        "chat_once",
        AsyncMock(
            return_value={
                "message": {
                    "content": json.dumps(
                        {
                            "title": "Math",
                            "questions": [
                                {
                                    "type": "short_answer",
                                    "prompt": "What is two plus two?",
                                    "answer": "Four",
                                }
                            ],
                        }
                    )
                }
            }
        ),
    )
    result = client.post(f"/api/learn/quizzes/from-note/{note_id}", json={"count": 1})
    assert result.status_code == 201, result.text
    assert result.json()["source_note_id"] == str(note_id)
    assert result.json()["source_conversation_id"] == str(conv_id)


def test_native_tool_schema_is_small_and_reference_free():
    tool = CreateQuizTool()
    schema = tool.get_parameters()
    assert tool.get_required_params() == ["title", "questions"]
    assert "$ref" not in str(schema)
    assert "id" not in schema["questions"]["items"]["properties"]
    assert "quiz" in default_tags("create_quiz")


def question(**kwargs):
    return dict(
        id=str(uuid.uuid4()),
        type="short_answer",
        prompt="What is 2 + 2?",
        answer="Four",
        options=[],
        explanation="Addition",
        **kwargs,
    )


def test_validation():
    with pytest.raises(ValidationError):
        Question(type="short_answer", prompt=" ", answer="x")
    with pytest.raises(ValidationError):
        Question(type="multiple_choice", prompt="Q", options=["a", "A"], correct_option=0)
    with pytest.raises(ValidationError):
        QuizInput(title="T", questions=[Question(type="short_answer", prompt="Q", answer="A")] * 2)


def test_attempt_keys_hidden_until_submission():
    q = question()
    attempt = QuizAttempt(
        id=uuid.uuid4(), quiz_id=uuid.uuid4(), snapshot={"title": "T", "questions": [q]}, answers={}
    )
    public = attempt_dict(attempt)
    for key in ("answer", "correct_option", "explanation"):
        assert key not in public["snapshot"]["questions"][0]
    attempt.submitted_at = datetime.now(timezone.utc)
    assert attempt_dict(attempt)["snapshot"]["questions"][0]["answer"] == "Four"


@pytest.mark.asyncio
async def test_exact_does_not_call_model(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(providers, "chat_once", mock)
    q = question()
    result = await grade([q], {q["id"]: "  FOUR "})
    assert result[q["id"]]["correct"] is True
    mock.assert_not_called()


@pytest.mark.asyncio
async def test_semantic_and_failure(monkeypatch):
    q = question()
    monkeypatch.setattr(model_prefs, "resolve_task_model", AsyncMock(return_value="local"))
    monkeypatch.setattr(
        providers,
        "chat_once",
        AsyncMock(
            return_value={
                "message": {
                    "content": '{"grades":[{"id":"'
                    + q["id"]
                    + '","correct":true,"feedback":"Equivalent"}]}'
                }
            }
        ),
    )
    assert (await grade([q], {q["id"]: "4"}))[q["id"]]["correct"] is True
    monkeypatch.setattr(providers, "chat_once", AsyncMock(side_effect=RuntimeError("offline")))
    assert (await grade([q], {q["id"]: "4"}))[q["id"]]["correct"] is None


@pytest.mark.asyncio
async def test_fenced_grading_and_invalid_identifiers(monkeypatch):
    q = question()
    monkeypatch.setattr(model_prefs, "resolve_task_model", AsyncMock(return_value="local"))
    data = {"grades": [{"id": q["id"], "correct": True, "feedback": "Equivalent"}]}
    mock = AsyncMock(
        return_value={"message": {"content": "```json\n" + json.dumps(data) + "\n```"}}
    )
    monkeypatch.setattr(providers, "chat_once", mock)
    assert (await grade([q], {q["id"]: "4"}))[q["id"]]["correct"] is True
    assert mock.call_args.kwargs["format"]["properties"]["grades"]["items"]["properties"]["id"][
        "enum"
    ] == [q["id"]]
    data["grades"][0]["id"] = "invented"
    mock.return_value = {"message": {"content": json.dumps(data)}}
    assert (await grade([q], {q["id"]: "4"}))[q["id"]]["correct"] is None


def test_private_document_access_and_page_provenance(client, database):
    document_id = notebook_tests.document(database)

    with Session(database) as session:
        conversation_id = session.get(Document, document_id).conversation_id
    _, payload = create(client)
    payload["questions"][0]["source_document_id"] = str(document_id)
    payload["questions"][0]["source_page"] = 2
    assert client.post("/api/learn/quizzes", json=payload).status_code == 403
    payload["source_conversation_id"] = str(conversation_id)
    result = client.post("/api/learn/quizzes", json=payload)
    assert result.status_code == 201, result.text
    assert result.json()["grounded"] is True
    assert result.json()["questions"][0]["source_page"] == 2


@pytest.mark.asyncio
async def test_native_tool_persists_without_exposing_answers(database, monkeypatch):
    from contextlib import asynccontextmanager
    from app.agent.tools import quizzes as tool_module

    with Session(database) as session:
        conv = Conversation(title="Quiz request")
        session.add(conv)
        session.commit()
        conversation_id = conv.id

    @asynccontextmanager
    async def factory():
        with Session(database, expire_on_commit=False) as session:
            yield AsyncAdapter(session)

    monkeypatch.setattr(tool_module, "async_session_factory", factory)
    result = await CreateQuizTool().execute(
        title="Shapes",
        conversation_id=str(conversation_id),
        questions=[
            {
                "type": "short_answer",
                "prompt": "How many sides does a triangle have?",
                "answer": "Three",
            }
        ],
    )
    assert result.success
    output = json.loads(result.output)
    assert output["question_count"] == 1
    assert "Three" not in result.output
    assert output["type"] == "quiz"


@pytest.mark.asyncio
async def test_no_cloud_grading(monkeypatch):
    q = question()
    monkeypatch.setattr(model_prefs, "resolve_task_model", AsyncMock(return_value="groq:test"))
    monkeypatch.setattr(providers, "is_groq_model", lambda _: True)
    mock = AsyncMock()
    monkeypatch.setattr(providers, "chat_once", mock)
    assert (await grade([q], {q["id"]: "4"}))[q["id"]]["correct"] is None
    mock.assert_not_called()
