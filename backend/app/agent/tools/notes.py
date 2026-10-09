"""Native study-note creation using the current agent context and retrieved sources."""

import json
import logging
import time
import uuid
from fastapi import HTTPException
from pydantic import ValidationError
from app.agent.base import BaseTool, ToolCall, ToolResult, ToolType, tool_registry
from app.db.session import async_session_factory
from app.learn.notes import NoteInput, create_note
from app.services.artifact_refs import REFERENCE_SCHEMA
from app.learn.notebooks import attach_generated

logger = logging.getLogger(__name__)


class CreateStudyNoteTool(BaseTool):
    name = "create_study_note"
    display_name = "Create study note"
    tool_type = ToolType.NOTES
    description = "Save study notes in Learn. Use clear Markdown headings, concise summaries, definitions, relationships and useful examples/code. Avoid filler, copying long passages or unsupported facts. For documents use rag_search for relevant sections first, not entire documents. Use current conversation context without extra history. Preserve retrieved document/page/chunk IDs. After saving, give a brief summary rather than repeating the note."

    def get_parameters(self):
        return {
            "source_artifact": REFERENCE_SCHEMA,
            "title": {"type": "string", "maxLength": 200},
            "content": {"type": "string", "maxLength": 40000},
            "source_document_id": {
                "type": "string",
                "description": "Optional document UUID from rag_search. Omit for conversation notes; never invent IDs.",
            },
            "source_page": {
                "type": "integer",
                "minimum": 1,
                "description": "Only with a retrieved source document.",
            },
            "source_chunk_id": {
                "type": "string",
                "description": "Only with a source document: exact retrieved chunk UUID, never an invented label.",
            },
        }

    def get_required_params(self):
        return ["title", "content"]

    async def execute(
        self,
        *,
        title,
        content,
        conversation_id=None,
        source_document_id=None,
        source_page=None,
        source_chunk_id=None,
        source_artifact=None,
        **kwargs,
    ):
        call = ToolCall(
            id=f"tc-note-{uuid.uuid4()}",
            type=self.tool_type,
            name=self.name,
            title="Creating study note",
            started_at=time.time(),
        )
        try:
            invalid_source = False

            def optional_source_id(value):
                nonlocal invalid_source
                if value is None:
                    return None
                try:
                    return uuid.UUID(str(value).strip())
                except (ValueError, TypeError, AttributeError):
                    invalid_source = True
                    return None

            source_document_id = optional_source_id(source_document_id)
            source_chunk_id = optional_source_id(source_chunk_id)
            # Page/chunk attribution has no meaning without a document. Small
            # models sometimes invent these optional fields for conversation notes.
            # Keep the trusted conversation origin; never persist fake attribution.
            orphaned_source = not source_document_id and (
                source_page is not None or source_chunk_id is not None
            )
            if not source_document_id:
                source_page = None
                source_chunk_id = None
            body = NoteInput(
                title=title,
                content=content,
                source_conversation_id=conversation_id,
                source_document_id=source_document_id,
                source_page=source_page,
                source_chunk_id=source_chunk_id,
                source_artifact=source_artifact,
            )
            if not body.content:
                raise ValueError("Provide study note content")
            async with async_session_factory() as db:
                note = await create_note(db, body)
                await attach_generated(db, conversation_id, "note", note.id)
                await db.commit()
                artifact = {"type": "study_note", "note_id": str(note.id), "title": note.title}
            call.gen_results = [artifact]
            call.status = "completed"
            output = json.dumps(
                {
                    **artifact,
                    "message": "Saved in Learn. Do not repeat the full note.",
                    **(
                        {"warning": "Ignored invalid or orphaned optional source references."}
                        if orphaned_source or invalid_source
                        else {}
                    ),
                }
            )
        except (ValidationError, ValueError, HTTPException) as exc:
            call.status = "error"
            output = "Invalid study note: " + (
                "; ".join(
                    error["msg"] for error in exc.errors(include_input=False, include_url=False)[:3]
                )
                if isinstance(exc, ValidationError)
                else str(exc.detail if isinstance(exc, HTTPException) else exc)
            )
            call.error = output
        except Exception:
            logger.exception("Study note creation failed")
            call.status = "error"
            output = "Could not save the study note. Try again later."
            call.error = output
        call.output = output
        call.completed_at = time.time()
        return ToolResult(call.status == "completed", output, call)


tool_registry.register(CreateStudyNoteTool())
