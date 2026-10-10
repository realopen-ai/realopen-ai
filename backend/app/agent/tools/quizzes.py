"""Standalone quiz creation; application owns identity and grading."""

import json
import logging
import time
import uuid

from fastapi import HTTPException
from pydantic import ValidationError

from app.agent.base import BaseTool, ToolCall, ToolResult, ToolType, tool_registry
from app.db.session import async_session_factory
from app.learn.quizzes import QuizInput, QUESTION_SCHEMA, save_quiz
from app.learn.notebooks import attach_generated

logger = logging.getLogger(__name__)


class CreateQuizTool(BaseTool):
    name = "create_quiz"
    display_name = "Create quiz"
    tool_type = ToolType.QUIZZES
    description = "Save a quiz in Learn, including standalone topic quizzes. Default: 5 questions, mixed difficulty, user's language. Honor requested count/type/difficulty. One concept per question, plausible distinct multiple-choice options, concise expected short answers and explanations. For source-grounded quizzes retrieve relevant sources first; never invent attribution. After saving, do not reveal questions or answer keys in chat."

    def get_parameters(self):
        return {
            "title": {"type": "string", "maxLength": 200},
            "description": {"type": "string", "maxLength": 2000},
            "questions": {"type": "array", "minItems": 1, "maxItems": 20, "items": QUESTION_SCHEMA},
        }

    def get_required_params(self):
        return ["title", "questions"]

    async def execute(self, *, title, questions, description="", conversation_id=None, **kwargs):
        call = ToolCall(
            id=f"tc-quiz-{uuid.uuid4()}",
            type=self.tool_type,
            name=self.name,
            title="Creating quiz",
            started_at=time.time(),
        )
        try:
            body = QuizInput(
                title=title,
                description=description,
                questions=questions,
                source_conversation_id=conversation_id,
            )
            async with async_session_factory() as db:
                quiz = await save_quiz(db, body)
                await attach_generated(db, conversation_id, "quiz", quiz.id)
                await db.commit()
                artifact = {
                    "type": "quiz",
                    "quiz_id": str(quiz.id),
                    "title": quiz.title,
                    "question_count": len(quiz.questions),
                }
            call.gen_results = [artifact]
            call.status = "completed"
            output = json.dumps(
                {**artifact, "message": "Saved in Learn. Do not reveal the answer key."}
            )
        except (ValidationError, ValueError, HTTPException) as exc:
            call.status = "error"
            output = "Invalid quiz: " + (
                "; ".join(
                    f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                    for error in exc.errors(include_input=False, include_url=False)[:3]
                )
                if isinstance(exc, ValidationError)
                else str(exc.detail if isinstance(exc, HTTPException) else exc)
            )
            call.error = output
        except Exception:
            logger.exception("Quiz creation failed")
            call.status = "error"
            output = "Could not save quiz. Try again later."
            call.error = output
        call.output, call.completed_at = output, time.time()
        return ToolResult(call.status == "completed", output, call)


tool_registry.register(CreateQuizTool())
