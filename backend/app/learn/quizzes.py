"""Quiz validation and answer-safe attempt serialization."""

import re
import uuid
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from sqlalchemy import select

from app.db.models import (
    Quiz,
    QuizAttempt,
    StudyNote,
    DocumentChunk,
    Artifact,
    ArtifactVersion,
)
from app.learn.service import now, utc, check_sources
from app.services.artifact_refs import (
    ArtifactReference,
    REFERENCE_SCHEMA,
    validate_reference,
)
from app.learn.schemas import similar_question
from app.learn.quiz_markdown import mask_code_blocks, validate_code_fences

QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["multiple_choice", "short_answer"]},
        "prompt": {
            "type": "string",
            "maxLength": 2000,
            "description": "Markdown question/context only, no answer choices. Fence code with a language tag and newlines.",
        },
        "options": {
            "type": "array",
            "items": {"type": "string", "maxLength": 500},
            "minItems": 2,
            "maxItems": 6,
            "description": "One answer per array entry, never a newline-joined list of choices. Markdown supported; no A)/B) labels. Keep choices out of prompt.",
        },
        "correct_option": {
            "type": "integer",
            "minimum": 0,
            "maximum": 5,
            "description": "Zero-based index for multiple-choice only.",
        },
        "answer": {
            "type": "string",
            "maxLength": 2000,
            "description": "Expected short answer; omit for multiple choice.",
        },
        "explanation": {"type": "string", "maxLength": 2000},
        "source_document_id": {
            "type": "string",
            "description": "Optional exact retrieved document UUID; never invent.",
        },
        "source_page": {"type": "integer", "minimum": 1},
        "source_chunk_id": {
            "type": "string",
            "description": "Optional exact retrieved chunk UUID.",
        },
        "source_artifact": REFERENCE_SCHEMA,
    },
    "required": ["type", "prompt", "explanation"],
    "allOf": [
        {
            "if": {
                "properties": {"type": {"enum": ["multiple_choice"]}},
                "required": ["type"],
            },
            "then": {"required": ["options", "correct_option"]},
        },
        {
            "if": {
                "properties": {"type": {"enum": ["short_answer"]}},
                "required": ["type"],
            },
            "then": {"required": ["answer"]},
        },
    ],
}


def clean_question_choices(prompt: str, option_count: int) -> str:
    prose = mask_code_blocks(prompt)
    labels = list(re.finditer(r"(?<!\w)([A-Fa-f])([).])(?:\s+|$)", prose))
    if len(labels) < 2:
        return prompt
    boundary = prompt[: labels[0].start()]
    # A standalone list heading belongs to the choices, not the question.
    # Require a question/line/colon boundary before it: prose mentioning
    # "choices" in the middle of a sentence must not be silently removed.
    heading = re.search(
        r"(?i)\b(?:answer[ \t]+)?(?:options|choices)[ \t]*:?[ \t]*$", boundary
    )
    has_list_heading = False
    if heading:
        before_heading = boundary[: heading.start()]
        if before_heading.rstrip().endswith(("?", "？", "؟", ":")) or re.search(
            r"\n[ \t]*$", before_heading
        ):
            boundary = before_heading
            has_list_heading = True
    stem = boundary.rstrip()
    choices = [
        prompt[
            label.end() : (
                labels[index + 1].start() if index + 1 < len(labels) else len(prompt)
            )
        ].strip()
        for index, label in enumerate(labels)
    ]
    expected = [chr(ord("A") + index) for index in range(option_count)]
    # A question-ending '?' or a separate list line establishes a clear boundary.
    clear_boundary = (
        stem.endswith(("?", "？", "؟"))
        or bool(re.search(r"\n[ \t]*$", boundary))
        or has_list_heading
    )
    if (
        stem
        and clear_boundary
        and [label[1].upper() for label in labels] == expected
        and len({label[2] for label in labels}) == 1
        and all(choices)
        and all(not re.search(r"[?？؟]|\n|```|~~~", choice) for choice in choices)
    ):
        return stem
    raise ValueError(
        "Keep answer choices only in options; remove the labeled choice list from prompt"
    )


def validate_option_choices(options: list[str]) -> None:
    """Catch overlapping choice lists without splitting or reindexing answers.

    Newlines alone are valid Markdown. A stronger signal is an unfenced
    multiline entry containing another complete option as a standalone line.
    Code/output blocks and ordinary multiline prose remain allowed.
    """
    normalized = [" ".join(option.casefold().split()) for option in options]
    for index, option in enumerate(options):
        # Fenced programs/output can legitimately include the entire text of
        # another option, e.g. printing one line versus printing several lines.
        if mask_code_blocks(option) != option:
            continue
        lines = [
            " ".join(line.casefold().split())
            for line in option.splitlines()
            if line.strip()
        ]
        if len(lines) < 2:
            continue
        for other_index, other in enumerate(normalized):
            if index != other_index and other in lines:
                raise ValueError(
                    f"options.{index} contains options.{other_index} as a separate line, "
                    "suggesting bundled answer choices. Put one independent answer per "
                    "array entry, remove duplicates, and set correct_option to its "
                    "zero-based index in the repaired array. Do not join choices with "
                    "newlines; use a fenced block for a single multiline code/output answer."
                )


class Question(BaseModel):
    type: Literal["multiple_choice", "short_answer"]
    prompt: str = Field(min_length=1, max_length=2000)
    options: list[str] = Field(default_factory=list, max_length=6)
    correct_option: StrictInt | None = None
    answer: str = Field(default="", max_length=2000)
    explanation: str = Field(default="", max_length=2000)
    source_document_id: uuid.UUID | None = None
    source_page: int | None = Field(default=None, ge=1)
    source_chunk_id: uuid.UUID | None = None
    source_artifact: ArtifactReference | None = None

    @field_validator("prompt", "answer", "explanation", "options")
    @classmethod
    def validate_markdown(cls, value):
        if isinstance(value, list):
            for index, option in enumerate(value):
                try:
                    validate_code_fences(option)
                except ValueError as exc:
                    raise ValueError(f"Option {index}: {exc}") from exc
        else:
            validate_code_fences(value)
        return value

    @model_validator(mode="after")
    def validate_question(self):
        self.prompt = self.prompt.strip()
        self.answer = self.answer.strip()
        self.options = [value.strip() for value in self.options]
        if not self.prompt:
            raise ValueError("Question cannot be empty")
        if self.type == "multiple_choice":
            self.prompt = clean_question_choices(self.prompt, len(self.options))
            if len(self.options) < 2 or any(
                not value or len(value) > 500 for value in self.options
            ):
                raise ValueError("Provide two to six nonempty options")
            if len({value.casefold() for value in self.options}) != len(self.options):
                raise ValueError("Options must be distinct")
            validate_option_choices(self.options)
            if self.correct_option is None or not 0 <= self.correct_option < len(
                self.options
            ):
                raise ValueError("Invalid correct option")
            self.answer = ""
        elif not self.answer:
            raise ValueError("Short-answer questions require an expected answer")
        else:
            if " ".join(self.answer.casefold().split()) == " ".join(
                self.prompt.casefold().split()
            ):
                raise ValueError("Question and expected answer must differ")
            self.options = []
            self.correct_option = None
        return self


class QuizInput(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    questions: list[Question] = Field(min_length=1, max_length=20)
    source_conversation_id: uuid.UUID | None = None
    source_document_id: uuid.UUID | None = None
    source_note_id: uuid.UUID | None = None
    expected_revision: int | None = None

    @model_validator(mode="after")
    def validate_quiz(self):
        self.title = self.title.strip()
        if not self.title:
            raise ValueError("Title cannot be empty")
        prompts = [" ".join(q.prompt.casefold().split()) for q in self.questions]
        if len(set(prompts)) != len(prompts) or any(
            similar_question(q.prompt, previous.prompt)
            for index, q in enumerate(self.questions)
            for previous in self.questions[:index]
        ):
            raise ValueError("Duplicate question")
        return self


async def get_quiz(db, quiz_id, lock=False):
    query = select(Quiz).where(Quiz.id == quiz_id)
    if lock:
        query = query.with_for_update()
    quiz = (await db.execute(query)).scalar_one_or_none()
    if quiz is None:
        raise HTTPException(404, "Quiz not found")
    return quiz


def public_questions(questions):
    return [
        {
            key: value
            for key, value in q.items()
            if key not in {"correct_option", "answer", "explanation"}
        }
        for q in questions
    ]


def quiz_dict(quiz, editor=False):
    return {
        "id": str(quiz.id),
        "title": quiz.title,
        "description": quiz.description,
        "revision": quiz.revision,
        "question_count": len(quiz.questions),
        "grounded": bool(
            quiz.source_note_id
            or any(
                q.get("source_document_id") or q.get("source_artifact")
                for q in quiz.questions
            )
        ),
        "questions": quiz.questions if editor else public_questions(quiz.questions),
        "source_conversation_id": (
            str(quiz.source_conversation_id) if quiz.source_conversation_id else None
        ),
        "source_note_id": str(quiz.source_note_id) if quiz.source_note_id else None,
        "created_at": utc(quiz.created_at) if quiz.created_at else None,
        "updated_at": utc(quiz.updated_at) if quiz.updated_at else None,
    }


async def save_quiz(db, body, quiz=None):
    await check_sources(db, body)
    if body.source_note_id and await db.get(StudyNote, body.source_note_id) is None:
        raise HTTPException(422, "Source note not found")
    if quiz is not None and body.expected_revision != quiz.revision:
        raise HTTPException(409, "Quiz changed; reload before editing")
    if quiz is None:
        quiz = Quiz()
        db.add(quiz)
        quiz.revision = 1
    else:
        quiz.revision += 1
    quiz.title, quiz.description = body.title, body.description
    quiz.source_conversation_id = body.source_conversation_id
    quiz.source_note_id = body.source_note_id
    validated = []
    for question in body.questions:
        if body.source_document_id and not question.source_document_id:
            question.source_document_id = body.source_document_id
        await check_sources(
            db,
            body.model_copy(update={"source_document_id": question.source_document_id}),
        )
        if question.source_chunk_id:
            chunk = await db.get(DocumentChunk, question.source_chunk_id)
            if chunk is None or chunk.document_id != question.source_document_id:
                raise HTTPException(422, "Chunk does not belong to source document")
            if question.source_page and chunk.page_number != question.source_page:
                raise HTTPException(422, "Page does not match source chunk")
            if question.source_page is None:
                question.source_page = chunk.page_number
        if (
            question.source_page or question.source_chunk_id
        ) and not question.source_document_id:
            raise HTTPException(422, "Page and chunk require a source document")
        reference = await validate_reference(
            db, question.source_artifact, body.source_conversation_id
        )
        if reference and question.source_document_id:
            artifact = await db.get(Artifact, uuid.UUID(reference["artifact_id"]))
            if artifact.document_id != question.source_document_id:
                raise HTTPException(422, "Artifact and document sources disagree")
        if reference is None and question.source_document_id and question.source_page:
            # Bridge existing document/page provenance into immutable artifact
            # citations where an exact extracted section already exists. This
            # lets mixed-document mistake decks reuse per-card artifact links.
            version = (
                await db.execute(
                    select(ArtifactVersion)
                    .join(Artifact, Artifact.id == ArtifactVersion.artifact_id)
                    .where(
                        Artifact.document_id == question.source_document_id,
                        ArtifactVersion.number == Artifact.current_version,
                    )
                )
            ).scalar_one_or_none()
            if version:
                matches = [
                    section
                    for section in version.sections
                    if section.get("page") == question.source_page
                ]
                if len(matches) == 1:
                    reference = await validate_reference(
                        db,
                        ArtifactReference(
                            artifact_id=version.artifact_id,
                            version=version.number,
                            section_id=matches[0]["id"],
                        ),
                        body.source_conversation_id,
                    )
        data = question.model_dump(mode="json")
        data["source_artifact"] = reference
        validated.append(dict(data, id=str(uuid.uuid4())))
    quiz.questions = validated
    quiz.updated_at = now()
    await db.flush()
    return quiz


async def get_attempt(db, quiz_id, attempt_id, lock=False):
    query = select(QuizAttempt).where(
        QuizAttempt.id == attempt_id, QuizAttempt.quiz_id == quiz_id
    )
    if lock:
        query = query.with_for_update()
    attempt = (await db.execute(query)).scalar_one_or_none()
    if attempt is None:
        raise HTTPException(404, "Attempt not found")
    return attempt


def attempt_dict(attempt):
    snapshot = dict(attempt.snapshot)
    if attempt.submitted_at is None:
        snapshot["questions"] = public_questions(snapshot["questions"])
    results = attempt.results
    return {
        "id": str(attempt.id),
        "quiz_id": str(attempt.quiz_id),
        "snapshot": snapshot,
        "answers": attempt.answers,
        "results": results,
        "score": (
            sum(r["correct"] is True for r in results.values()) if results else None
        ),
        "needs_review": (
            sum(r["correct"] is None for r in results.values()) if results else 0
        ),
        "created_at": utc(attempt.created_at) if attempt.created_at else None,
        "submitted_at": utc(attempt.submitted_at) if attempt.submitted_at else None,
    }
