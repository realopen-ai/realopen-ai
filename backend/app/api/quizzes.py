import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, StrictInt, StrictStr
from sqlalchemy import select

from app.db.models import Quiz, QuizAttempt
from app.db.session import get_db
from app.learn import quizzes
from app.learn.quiz_grading import grade
from app.learn.service import now
from app.learn.notebooks import attach_generated
from app.learn.schemas import CardInput, DeckInput
from app.learn.service import create_deck, deck_detail
from app.learn.quiz_generation import GenerateQuiz, from_note
from app.learn.notes import get_note

router = APIRouter(prefix="/learn/quizzes", tags=["learn"])


class Answers(BaseModel):
    answers: dict[str, StrictInt | StrictStr | None] = Field(default_factory=dict, max_length=20)


class Override(BaseModel):
    correct: bool


class Mistakes(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    question_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)


def mistake_cards(row, selected=None):
    if row.submitted_at is None:
        raise HTTPException(409, "Submit the quiz before creating flashcards")
    questions = [
        q for q in row.snapshot["questions"] if row.results[q["id"]]["correct"] is not True
    ]
    if selected is not None:
        ids = {str(key) for key in selected}
        if not ids.issubset({q["id"] for q in questions}):
            raise HTTPException(422, "Select questions requiring review")
        questions = [q for q in questions if q["id"] in ids]
    return questions


def card_for_question(q):
    answer = q["options"][q["correct_option"]] if q["type"] == "multiple_choice" else q["answer"]
    explanation = "\n\n" + q["explanation"] if q["explanation"] else ""
    # Keep the complete expected answer. Explanations are optional enrichment,
    # not a reason to truncate a correct answer or exceed the card-size limit.
    back = answer + explanation if len(answer + explanation) <= 4000 else answer
    return {
        "front": q["prompt"],
        "back": back,
        "source_artifact": q.get("source_artifact"),
        "source_page": q.get("source_page"),
        "source_chunk_id": q.get("source_chunk_id"),
    }


def validate_answers(attempt, body):
    questions = {q["id"]: q for q in attempt.snapshot["questions"]}
    for key, value in body.answers.items():
        q = questions.get(key)
        if q is None:
            raise HTTPException(422, "Unknown question")
        if value is None:
            continue
        if q["type"] == "multiple_choice":
            if type(value) is not int or not 0 <= value < len(q["options"]):
                raise HTTPException(422, "Invalid option")
        elif not isinstance(value, str) or len(value) > 2000:
            raise HTTPException(422, "Invalid answer")


@router.get("")
async def list_quizzes(q: str = Query(default="", max_length=200), db=Depends(get_db)):
    query = select(Quiz).order_by(Quiz.updated_at.desc()).limit(200)
    if q.strip():
        query = query.where(Quiz.title.icontains(q.strip(), autoescape=True))
    return [quizzes.quiz_dict(row) for row in (await db.execute(query)).scalars()]


@router.post("", status_code=201)
async def create_quiz(body: quizzes.QuizInput, db=Depends(get_db)):
    quiz = await quizzes.save_quiz(db, body)
    await attach_generated(db, quiz.source_conversation_id, "quiz", quiz.id)
    return quizzes.quiz_dict(quiz)


@router.get("/{quiz_id}")
async def detail(quiz_id: uuid.UUID, db=Depends(get_db)):
    return quizzes.quiz_dict(await quizzes.get_quiz(db, quiz_id))


@router.post("/from-note/{note_id}", status_code=201)
async def generate_from_note(note_id: uuid.UUID, body: GenerateQuiz, db=Depends(get_db)):
    quiz = await quizzes.save_quiz(db, await from_note(await get_note(db, note_id), body))
    await attach_generated(db, quiz.source_conversation_id, "quiz", quiz.id)
    return quizzes.quiz_dict(quiz)


@router.get("/{quiz_id}/editor")
async def editor(quiz_id: uuid.UUID, db=Depends(get_db)):
    return quizzes.quiz_dict(await quizzes.get_quiz(db, quiz_id), editor=True)


@router.put("/{quiz_id}")
async def update(quiz_id: uuid.UUID, body: quizzes.QuizInput, db=Depends(get_db)):
    return quizzes.quiz_dict(
        await quizzes.save_quiz(db, body, await quizzes.get_quiz(db, quiz_id, lock=True))
    )


@router.delete("/{quiz_id}", status_code=204)
async def delete(quiz_id: uuid.UUID, db=Depends(get_db)):
    await db.delete(await quizzes.get_quiz(db, quiz_id, lock=True))
    return Response(status_code=204)


@router.post("/{quiz_id}/attempts", status_code=201)
async def start(quiz_id: uuid.UUID, db=Depends(get_db)):
    quiz = await quizzes.get_quiz(db, quiz_id, lock=True)
    existing = (
        await db.execute(
            select(QuizAttempt)
            .where(QuizAttempt.quiz_id == quiz.id, QuizAttempt.submitted_at.is_(None))
            .order_by(QuizAttempt.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return quizzes.attempt_dict(existing)
    attempt = QuizAttempt(
        quiz_id=quiz.id,
        snapshot={
            "title": quiz.title,
            "revision": quiz.revision,
            "questions": quiz.questions,
            "source_conversation_id": str(quiz.source_conversation_id)
            if quiz.source_conversation_id
            else None,
            "source_note_id": str(quiz.source_note_id) if quiz.source_note_id else None,
        },
        answers={},
    )
    db.add(attempt)
    await db.flush()
    return quizzes.attempt_dict(attempt)


@router.get("/{quiz_id}/attempts")
async def history(quiz_id: uuid.UUID, db=Depends(get_db)):
    await quizzes.get_quiz(db, quiz_id)
    rows = (
        await db.execute(
            select(QuizAttempt)
            .where(QuizAttempt.quiz_id == quiz_id)
            .order_by(QuizAttempt.created_at.desc())
            .limit(100)
        )
    ).scalars()
    return [quizzes.attempt_dict(row) for row in rows]


@router.get("/{quiz_id}/attempts/{attempt_id}")
async def attempt(quiz_id: uuid.UUID, attempt_id: uuid.UUID, db=Depends(get_db)):
    return quizzes.attempt_dict(await quizzes.get_attempt(db, quiz_id, attempt_id))


@router.put("/{quiz_id}/attempts/{attempt_id}/answers")
async def draft(quiz_id: uuid.UUID, attempt_id: uuid.UUID, body: Answers, db=Depends(get_db)):
    row = await quizzes.get_attempt(db, quiz_id, attempt_id, lock=True)
    if row.submitted_at:
        raise HTTPException(409, "Attempt already submitted")
    validate_answers(row, body)
    row.answers = body.answers
    await db.flush()
    return quizzes.attempt_dict(row)


@router.get("/{quiz_id}/attempts/{attempt_id}/flashcards")
async def preview_mistakes(quiz_id: uuid.UUID, attempt_id: uuid.UUID, db=Depends(get_db)):
    row = await quizzes.get_attempt(db, quiz_id, attempt_id)
    return [{"question_id": q["id"], **card_for_question(q)} for q in mistake_cards(row)]


@router.post("/{quiz_id}/attempts/{attempt_id}/flashcards", status_code=201)
async def save_mistakes(
    quiz_id: uuid.UUID, attempt_id: uuid.UUID, body: Mistakes, db=Depends(get_db)
):
    row = await quizzes.get_attempt(db, quiz_id, attempt_id, lock=True)
    if row.flashcard_deck_id:
        return await deck_detail(db, row.flashcard_deck_id)
    questions = mistake_cards(row, body.question_ids)
    documents = {q.get("source_document_id") for q in questions}
    document_id = next(iter(documents)) if len(documents) == 1 else None
    cards = []
    for q in questions:
        data = card_for_question(q)
        if not document_id:
            # Mixed-document decks retain references without claiming all cards
            # came from one document. Artifact references remain per-card.
            if q.get("source_document_id"):
                data["source_reference"] = (
                    f"Document {q['source_document_id']}; page {q.get('source_page')}; chunk {q.get('source_chunk_id')}"
                )
            data["source_page"] = data["source_chunk_id"] = None
        cards.append(CardInput(**data))
    deck = await create_deck(
        db,
        DeckInput(
            title=body.title,
            cards=cards,
            source_conversation_id=row.snapshot.get("source_conversation_id"),
            source_document_id=document_id,
        ),
    )
    deck.source_note_id = (
        uuid.UUID(row.snapshot["source_note_id"]) if row.snapshot.get("source_note_id") else None
    )
    row.flashcard_deck_id = deck.id
    await attach_generated(db, deck.source_conversation_id, "deck", deck.id)
    await db.flush()
    return await deck_detail(db, deck.id)


@router.post("/{quiz_id}/attempts/{attempt_id}/submit")
async def submit(quiz_id: uuid.UUID, attempt_id: uuid.UUID, body: Answers, db=Depends(get_db)):
    row = await quizzes.get_attempt(db, quiz_id, attempt_id, lock=True)
    if row.submitted_at is None:
        validate_answers(row, body)
        row.answers = body.answers
        row.results = await grade(row.snapshot["questions"], row.answers)
        row.submitted_at = now()
        await db.flush()
    return quizzes.attempt_dict(row)


@router.put("/{quiz_id}/attempts/{attempt_id}/override/{question_id}")
async def override(
    quiz_id: uuid.UUID,
    attempt_id: uuid.UUID,
    question_id: uuid.UUID,
    body: Override,
    db=Depends(get_db),
):
    row = await quizzes.get_attempt(db, quiz_id, attempt_id, lock=True)
    key = str(question_id)
    question = next((q for q in row.snapshot["questions"] if q["id"] == key), None)
    if row.submitted_at is None or question is None or question["type"] != "short_answer":
        raise HTTPException(409, "Only submitted short answers can be overridden")
    results = dict(row.results)
    original = results[key].get("original", results[key])
    results[key] = {
        "correct": body.correct,
        "method": "override",
        "feedback": "",
        "original": original,
    }
    row.results = results
    await db.flush()
    return quizzes.attempt_dict(row)
