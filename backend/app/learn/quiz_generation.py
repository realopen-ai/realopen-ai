"""Focused generation from saved notes, without unrelated chat history."""

import json
import re
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app.learn.quizzes import QuizInput, Question
from app.prompts import format_prompt, get_prompt
from app.services import model_prefs, providers
from app.services.artifact_refs import ArtifactReference


class GenerateQuiz(BaseModel):
    count: int = Field(default=5, ge=1, le=20)
    type: Literal["mixed", "multiple_choice", "short_answer"] = "mixed"
    difficulty: Literal["mixed", "easy", "medium", "hard"] = "mixed"
    language: str = Field(default="", max_length=80)
    selection: str | None = Field(default=None, min_length=1, max_length=12000)


async def from_note(note, body):
    content = body.selection if body.selection is not None else note.content
    if body.selection is not None and body.selection not in note.content:
        raise HTTPException(422, "Selection no longer belongs to the saved note")
    if not content.strip() or len(content) > 12000:
        raise HTTPException(422, "Select a nonempty section of at most 12000 characters")
    messages = [
        {
            "role": "system",
            "content": format_prompt(
                "quiz_generation_system",
                count=body.count,
                question_type=body.type,
                difficulty=body.difficulty,
                language=body.language or "the source language",
                writing_guidance=get_prompt("quiz_writing"),
            ),
        },
        {
            "role": "user",
            "content": json.dumps({"title": note.title, "content": content}, ensure_ascii=False),
        },
    ]
    try:
        model = await model_prefs.resolve_task_model("chat")
        if providers.is_groq_model(model):
            raise HTTPException(503, "Configure a local chat model to generate quizzes")
        for attempt in range(2):
            result = await providers.chat_once(
                model,
                messages,
                format="json",
                think=False,
                timeout=90,
                options={"temperature": 0.2, "num_predict": 6000},
            )
            try:
                raw = result.get("message", {}).get("content", "")
                if not isinstance(raw, str) or len(raw) > 100000:
                    raise ValueError("Invalid response size")
                parsed = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip()))
                questions = [
                    Question.model_validate(
                        {
                            key: q[key]
                            for key in (
                                "type",
                                "prompt",
                                "options",
                                "correct_option",
                                "answer",
                                "explanation",
                            )
                            if key in q
                        }
                    )
                    for q in parsed["questions"]
                ]
                if (
                    len(questions) != body.count
                    or body.type != "mixed"
                    and any(q.type != body.type for q in questions)
                ):
                    raise ValueError("Incorrect question count or type")
                for q in questions:
                    q.source_document_id = note.source_document_id
                    q.source_page = note.source_page
                    q.source_chunk_id = note.source_chunk_id
                    q.source_artifact = (
                        ArtifactReference.model_validate(note.source_artifact)
                        if note.source_artifact
                        else None
                    )
                return QuizInput(
                    title=parsed["title"],
                    description=parsed.get("description", ""),
                    questions=questions,
                    source_conversation_id=note.source_conversation_id,
                    source_note_id=note.id,
                )
            except (ValueError, TypeError, KeyError) as exc:
                if attempt:
                    raise HTTPException(
                        422, "The model returned an invalid quiz. Nothing was saved."
                    )
                messages.append(
                    {
                        "role": "user",
                        "content": format_prompt(
                            "quiz_generation_retry",
                            error=str(exc)[:1000],
                            writing_guidance=get_prompt("quiz_writing"),
                        ),
                    }
                )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, "Local quiz generation unavailable. Nothing was saved.") from exc
