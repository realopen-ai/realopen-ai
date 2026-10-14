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
from app.learn.service import check_sources
from app.db.models import DocumentChunk
from app.services.artifact_refs import (
    ArtifactReference,
    REFERENCE_SCHEMA,
    validate_reference,
)
from app.learn.notebooks import attach_generated

logger = logging.getLogger(__name__)


class CreateStudyNoteTool(BaseTool):
    guidance_prompt = "agent_study_notes"
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

    def normalize_arguments(self, kwargs):
        values = dict(kwargs)
        for key in ("source_document_id", "source_chunk_id"):
            if values.get(key) is not None:
                try:
                    values[key] = str(uuid.UUID(str(values[key]).strip()))
                except (ValueError, TypeError, AttributeError):
                    values[key] = None
        if not values.get("source_document_id"):
            values["source_page"] = None
            values["source_chunk_id"] = None
        if values.get("source_artifact") is not None:
            try:
                values["source_artifact"] = ArtifactReference.model_validate(
                    values["source_artifact"]
                ).model_dump(mode="json", exclude_none=True)
            except (ValidationError, ValueError, TypeError):
                values["source_artifact"] = None
        return values

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
        failure_kind = None
        try:
            sources = {
                "source_document_id": source_document_id,
                "source_page": source_page,
                "source_chunk_id": source_chunk_id,
                "source_artifact": source_artifact,
            }
            normalized_sources = self.normalize_arguments(sources)
            invalid_source = any(
                value is not None and normalized_sources.get(key) is None
                for key, value in sources.items()
            )
            body = NoteInput(
                title=title,
                content=content,
                source_conversation_id=conversation_id,
                **normalized_sources,
            )
            if not body.content:
                raise ValueError("Provide study note content")
            async with async_session_factory() as db:
                # Optional model-supplied attribution is best-effort, unlike
                # the strict manual API. Never drop the trusted conversation,
                # title/content, or swallow infrastructure/transaction failures.
                if body.source_document_id:
                    try:
                        await check_sources(db, body)
                    except HTTPException as exc:
                        if exc.status_code not in {403, 404, 422}:
                            raise
                        body.source_document_id = None
                        body.source_chunk_id = None
                        body.source_page = None
                        invalid_source = True
                if body.source_chunk_id:
                    chunk = await db.get(DocumentChunk, body.source_chunk_id)
                    if chunk is None or chunk.document_id != body.source_document_id:
                        body.source_chunk_id = None
                        body.source_page = None
                        invalid_source = True
                    else:
                        body.source_page = chunk.page_number
                if body.source_artifact:
                    try:
                        await validate_reference(
                            db, body.source_artifact, conversation_id
                        )
                    except HTTPException as exc:
                        if exc.status_code not in {403, 404, 422}:
                            raise
                        body.source_artifact = None
                        invalid_source = True
                note = await create_note(db, body)
                await attach_generated(db, conversation_id, "note", note.id)
                await db.commit()
                artifact = {
                    "type": "study_note",
                    "note_id": str(note.id),
                    "title": note.title,
                }
            call.gen_results = [artifact]
            call.status = "completed"
            output = json.dumps(
                {
                    **artifact,
                    "message": "Saved in Learn. Do not repeat the full note.",
                    **(
                        {
                            "warning": "Ignored invalid or orphaned optional source references."
                        }
                        if invalid_source
                        else {}
                    ),
                }
            )
        except (ValidationError, ValueError, HTTPException) as exc:
            failure_kind = (
                "execution"
                if isinstance(exc, HTTPException) and exc.status_code not in {400, 422}
                else "validation"
            )
            call.status = "error"
            prefix = (
                "Invalid study note"
                if failure_kind == "validation"
                else "Could not save study note"
            )
            output = (
                prefix
                + ": "
                + (
                    "; ".join(
                        error["msg"]
                        for error in exc.errors(include_input=False, include_url=False)[
                            :3
                        ]
                    )
                    if isinstance(exc, ValidationError)
                    else str(exc.detail if isinstance(exc, HTTPException) else exc)
                )
            )
            call.error = output
        except Exception:
            logger.exception("Study note creation failed")
            call.status = "error"
            output = "Could not save the study note. Try again later."
            call.error = output
        call.output = output
        call.completed_at = time.time()
        return ToolResult(call.status == "completed", output, call, failure_kind)


tool_registry.register(CreateStudyNoteTool())
