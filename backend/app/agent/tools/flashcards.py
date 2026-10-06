"""Native, opt-in learning artifact creation. No additional inference call."""

import json
import logging
import time

from fastapi import HTTPException
from pydantic import ValidationError

from app.agent.base import BaseTool, ToolCall, ToolResult, ToolType, tool_registry
from app.db.session import async_session_factory
from app.learn.schemas import DeckInput
from app.learn.service import create_deck

logger = logging.getLogger(__name__)


class CreateFlashcardDeckTool(BaseTool):
    name = "create_flashcard_deck"
    display_name = "Create flashcards"
    tool_type = ToolType.FLASHCARDS
    description = (
        "Save a flashcard deck in Learn. One concept per card; concise active-recall "
        "questions and answers. Do not give away answers or repeat questions. "
        "For documents, use rag_search first for relevant sections; never load an entire "
        "large document. Return a brief summary, not the cards, after saving."
        " Honor requested count, difficulty, language and answer detail; default to "
        "10 accessible cards in the user's language with concise answers. Preserve "
        "document/page/chunk IDs from retrieval when available."
    )

    def get_parameters(self):
        return {
            "title": {"type": "string", "maxLength": 200},
            "description": {"type": "string", "maxLength": 2000},
            "cards": {
                "type": "array",
                "minItems": 1,
                "maxItems": 100,
                "items": {
                    "type": "object",
                    "properties": {
                        "front": {"type": "string", "maxLength": 2000},
                        "back": {"type": "string", "maxLength": 4000},
                        "source_reference": {
                            "type": "string",
                            "description": "Optional page/chapter reference",
                            "maxLength": 500,
                        },
                        "source_page": {"type": "integer", "minimum": 1},
                        "source_chunk_id": {
                            "type": "string",
                            "description": "Optional chunk UUID from retrieval",
                        },
                    },
                    "required": ["front", "back"],
                    "additionalProperties": False,
                },
            },
            "source_document_id": {
                "type": "string",
                "description": "Optional document UUID returned by rag_search",
            },
        }

    def get_required_params(self):
        return ["title", "cards"]

    async def execute(
        self,
        *,
        title,
        cards,
        description=None,
        source_document_id=None,
        conversation_id=None,
        **kwargs,
    ):
        started = time.time()
        call = ToolCall(
            id=f"tc-flashcards-{int(started * 1000)}",
            type=self.tool_type,
            name=self.name,
            title="Creating flashcards",
            started_at=started,
        )
        try:
            body = DeckInput(
                title=title,
                cards=cards,
                description=description,
                source_document_id=source_document_id,
                source_conversation_id=conversation_id,
            )
            if not body.cards:
                raise ValueError("Provide at least one card")
            async with async_session_factory() as db:
                deck = await create_deck(db, body)
                await db.commit()
                artifact = {
                    "type": "flashcard_deck",
                    "deck_id": str(deck.id),
                    "title": deck.title,
                    "card_count": len(body.cards),
                }
            call.gen_results = [artifact]
            call.status = "completed"
            output = json.dumps(
                {
                    **artifact,
                    "message": "Saved in Learn. The user can study via the deck card. Do not repeat the cards.",
                }
            )
        except (ValidationError, ValueError, HTTPException) as exc:
            call.status = "error"
            if isinstance(exc, ValidationError):
                detail = "; ".join(
                    f"{'.'.join(map(str, error['loc']))}: {error['msg']}"
                    for error in exc.errors(include_input=False, include_url=False)[:5]
                )
            else:
                detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            output = f"Invalid flashcards: {detail}"
            call.error = output
        except Exception:
            logger.exception("Flashcard creation failed")
            call.status = "error"
            output = "Could not save flashcards. Try again later."
            call.error = output
        call.completed_at = time.time()
        call.output = output
        return ToolResult(call.status == "completed", output, call)


tool_registry.register(CreateFlashcardDeckTool())
