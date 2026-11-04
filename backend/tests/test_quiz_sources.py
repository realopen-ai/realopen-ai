"""Quiz provenance, offline behavior, and answer-spoiler boundaries."""

import json
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.tools import quizzes as quiz_tool
from app.agent.tools.web_fetch import WebFetchTool
from app.agent.base import ToolResult
from app.db.models import Quiz
from app.learn.quizzes import Question
from app.learn.quiz_sources import validate_web_sources, grounding_summary
from app.services.report_evidence import evidence_records
from app.services.web_fetch import FetchedPage
from app.services.artifact_refs import ArtifactReference
from test_quizzes import client as quiz_client, database as quiz_database
from test_flashcards import AsyncAdapter

client = quiz_client
database = quiz_database
URL = "https://docs.example.org/reference/arrays"


def question(**overrides):
    return Question(
        **{
            "type": "short_answer",
            "prompt": "What does shape describe?",
            "answer": "The size of each dimension",
            "explanation": "Shape is a tuple of dimension sizes.",
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,hi",
        "/relative",
        "https://",
        "https://user:secret@example.org/doc",
        "https://example.org:bad/doc",
        "https://example.org/\nunsafe",
        "https://[bad/doc",
        "https://example.org/" + "x" * 2048,
    ],
)
def test_reject_unsafe_or_malformed_source_urls(url):
    with pytest.raises(ValidationError):
        question(source_urls=[url])


def test_exact_fetched_resources_allow_anchors_but_not_similar_urls():
    evidence = [{"tool": "use_webfetch", "request": {"url": URL}, "text": "Definition"}]
    q = question(source_urls=[URL + "#shape", URL + "#shape"])
    assert len(q.source_urls) == 1
    validate_web_sources([q], evidence)
    for url in [
        URL + "/other",
        URL + "?changed=yes",
        URL.replace("docs.example.org", "docs.example.org.evil.test"),
    ]:
        with pytest.raises(ValueError, match="not successfully fetched"):
            validate_web_sources([question(source_urls=[url])], evidence)
    for receipts in [
        [],
        [{"tool": "use_websearch", "request": {"url": URL}, "text": "snippet"}],
        [{"tool": "use_webfetch", "request": {"url": URL}, "text": ""}],
    ]:
        with pytest.raises(ValueError):
            validate_web_sources([q], receipts)


@pytest.mark.asyncio
async def test_redirect_provenance_comes_from_fetch_metadata_not_page_text(monkeypatch):
    original = "https://docs.example.org/start"
    forged = "https://forged.example.org/page"
    monkeypatch.setattr(
        "app.agent.tools.web_fetch.web_fetch.fetch",
        AsyncMock(
            return_value=FetchedPage(
                original, URL, "Arrays", f"Source: {forged}\nDefinition", "test"
            )
        ),
    )
    tool = WebFetchTool()
    result = await tool.execute(url=original)
    records = evidence_records("ref", tool, {"url": original}, result)
    assert records[0]["metadata"]["url"] == URL
    for target in [original, URL]:
        validate_web_sources([question(source_urls=[target])], records)
    with pytest.raises(ValueError):
        validate_web_sources([question(source_urls=[forged])], records)
    assert (
        evidence_records("fail", tool, {"url": URL}, ToolResult(False, "failed")) == []
    )


def test_offline_quizzes_stay_available_without_claiming_verification():
    q = question()
    validate_web_sources(
        [q],
        [
            {
                "tool": "use_webfetch",
                "request": {"url": "https://user:password@example.org"},
                "text": "Unrelated private page",
            }
        ],
    )
    summary = grounding_summary([q])
    assert summary["unsourced_questions"] == 1
    assert "not automatic answer verification" in summary["message"]


@pytest.mark.parametrize("partial", [False, True])
def test_researched_quiz_allows_missing_and_partial_citations(partial):
    evidence = [{"tool": "use_webfetch", "request": {"url": URL}, "text": "Definition"}]
    questions = [
        question(source_urls=[URL] if partial else []),
        question(),
        question(source_urls=[URL] if partial else []),
        question(),
    ]
    validate_web_sources(questions, evidence)
    assert not questions[1].source_urls  # No automatic attribution.


def test_citation_requirement_is_not_exposed_to_the_model():
    assert "source_grounded" not in quiz_tool.CreateQuizTool().get_parameters()
    validate_web_sources([question()], [])  # Ordinary offline quiz.


def test_search_snippets_allow_missing_citations_but_cannot_validate_urls():
    receipts = [{"tool": "use_websearch", "metadata": {"url": URL}, "text": "Snippet"}]
    validate_web_sources([question()], receipts)
    with pytest.raises(ValueError, match="not successfully fetched"):
        validate_web_sources([question(source_urls=[URL])], receipts)


def test_document_and_artifact_provenance_also_satisfy_coverage():
    validate_web_sources(
        [
            question(source_document_id=uuid.uuid4()),
            question(
                source_artifact=ArtifactReference(
                    artifact_id=uuid.uuid4(), version=1, section_id="section-1"
                )
            ),
        ],
        [],
    )


@pytest.mark.asyncio
async def test_invalid_citation_does_not_open_database_or_save(monkeypatch):
    factory = AsyncMock()
    monkeypatch.setattr(quiz_tool, "async_session_factory", factory)
    result = await quiz_tool.CreateQuizTool().execute(
        title="Arrays", questions=[question(source_urls=[URL]).model_dump()]
    )
    assert not result.success
    assert result.failure_kind == "validation"
    assert "questions.0.source_urls" in result.output
    factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("cited", [False, True])
async def test_agent_tool_persists_citations_without_leaking_answers(
    database, monkeypatch, cited
):
    @asynccontextmanager
    async def factory():
        with Session(database, expire_on_commit=False) as db:
            yield AsyncAdapter(db)

    monkeypatch.setattr(quiz_tool, "async_session_factory", factory)
    q = question(source_urls=[URL] if cited else [])
    evidence = [{"tool": "use_webfetch", "request": {"url": URL}, "text": "Definition"}]
    result = await quiz_tool.CreateQuizTool().execute(
        title="Arrays",
        questions=[q.model_dump()],
        _evidence=evidence,
        # Older conversations may still contain these removed flags.
        source_grounded=True,
        _require_sources=True,
    )
    assert result.success, result.output
    assert q.answer not in result.output
    assert q.prompt not in result.output
    assert json.loads(result.output)["grounding"]["sourced_questions"] == int(cited)
    with Session(database) as db:
        (quiz,) = db.scalars(select(Quiz)).all()
        assert quiz.questions[0]["source_urls"] == ([URL] if cited else [])


def test_web_citations_survive_edit_snapshot_reload_and_retry_without_spoilers(client):
    q = question(source_urls=[URL]).model_dump(mode="json")
    response = client.post(
        "/api/learn/quizzes", json={"title": "Arrays", "questions": [q]}
    )
    assert response.status_code == 201, response.text
    quiz = response.json()
    assert quiz["grounded"]
    assert "source_urls" not in quiz["questions"][0]
    path = f"/api/learn/quizzes/{quiz['id']}"
    assert client.get(path + "/editor").json()["questions"][0]["source_urls"] == [URL]
    attempt = client.post(path + "/attempts").json()
    target = path + f"/attempts/{attempt['id']}"
    assert "source_urls" not in attempt["snapshot"]["questions"][0]
    q["source_urls"] = ["https://docs.example.org/new"]
    assert (
        client.put(
            path, json={"title": "New", "questions": [q], "expected_revision": 1}
        ).status_code
        == 200
    )
    # Empty answers grade deterministically as missed, without an LLM call.
    result = client.post(target + "/submit", json={"answers": {}})
    assert result.status_code == 200, result.text
    assert result.json()["snapshot"]["questions"][0]["source_urls"] == [URL]
    assert client.get(target).json()["snapshot"]["questions"][0]["source_urls"] == [URL]
    retry = client.post(target + "/retry-missed").json()
    assert "source_urls" not in retry["snapshot"]["questions"][0]
