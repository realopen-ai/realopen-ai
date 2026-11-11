"""In-memory workspace for a temporary chat's attachments and staged outputs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import time
import uuid
from itertools import groupby
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from tempfile import TemporaryDirectory
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import HTTPException

from app.db.models import Artifact, ArtifactVersion, Document, DocumentChunk, Memory
from app.services.artifact_sources import sections_for
from app.services.memory import MemoryManager
from app.services import rag

MAX_ATTACHMENTS_BYTES = 64 * 1024 * 1024
MAX_ATTACHMENTS = 24
MAX_SESSIONS = 32
SESSION_IDLE_SECONDS = 12 * 60 * 60


def attachment_filename(filename: str | None) -> str:
    name = Path((filename or "").replace(chr(92), "/")).name[:255]
    return "attachment" if name in {"", ".", ".."} or chr(0) in name else name


# These tools have no durable effects for an unlinked conversation. Mutating
# tools must get an explicit staging adapter before being enabled here.
READ_TOOLS = {
    "use_websearch",
    "use_webfetch",
    "use_vision",
    "rag_search",
    "search_past_conversations",
    "load_skill",
    "discover_tools",
    "read_tool_result",
    "use_code_exec",
    "use_image_gen",
}
STAGED_TOOLS = {
    "create_study_note",
    "create_flashcard_deck",
    "create_quiz",
    "manage_memory",
    "use_report_gen",
    "use_pptx_gen",
    "use_excel_gen",
    "list_artifacts",
    "read_artifact",
    "summarize_artifact",
    "update_artifact",
    "export_artifact",
}
report_output_directory: ContextVar[Path | None] = ContextVar(
    "temporary_report_output", default=None
)


@dataclass
class StagedOutput:
    persist: Callable[..., Awaitable[dict]]
    title: str


@dataclass
class MemoryDraft:
    id: uuid.UUID
    text: str
    category: str
    pinned: bool
    original: tuple[str, str, bool] | None = None
    deleted: bool = False


@dataclass
class Attachment:
    id: uuid.UUID
    filename: str
    data: bytes
    image: bool
    message_index: int
    chunks: list[tuple[uuid.UUID, rag.Chunk]]
    pages: int


@dataclass
class TemporaryChat:
    id: uuid.UUID
    touched: float = field(default_factory=time.monotonic)
    discarded: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    attachments: list[Attachment] = field(default_factory=list)
    outputs: dict[str, StagedOutput] = field(default_factory=dict)
    reports: dict[str, dict] = field(default_factory=dict)
    versions: dict[uuid.UUID, list[ArtifactVersion]] = field(default_factory=dict)
    directory: TemporaryDirectory | None = None
    memory_changes: dict[uuid.UUID, MemoryDraft] = field(default_factory=dict)

    def merge_memories(self, memories):
        return [
            *(memory for memory in memories if memory.id not in self.memory_changes),
            *(draft for draft in self.memory_changes.values() if not draft.deleted),
        ]

    async def save_memories(self, db):
        manager = MemoryManager()
        for draft in self.memory_changes.values():
            if draft.original is None:
                if not draft.deleted:
                    await manager.add_memory(
                        db,
                        draft.text,
                        category=draft.category,
                        pinned=draft.pinned,
                        source="ai_agent",
                        conversation_id=str(self.id),
                    )
                continue
            current = await db.get(Memory, draft.id, with_for_update=True)
            if (
                not current
                or (current.text, current.category, current.pinned) != draft.original
            ):
                raise HTTPException(409, "A staged memory changed outside this chat")
            if draft.deleted:
                await manager.delete_memory(db, str(draft.id))
            else:
                await manager.update_memory(
                    db, str(draft.id), text=draft.text, category=draft.category
                )

    def reports_dir(self) -> Path:
        if self.directory is None:
            self.directory = TemporaryDirectory(prefix="realopen-temporary-")
        return Path(self.directory.name)

    def stage_output(self, title, persist):
        if len(self.outputs) >= 100:
            raise HTTPException(413, "Too many temporary outputs")
        ident = uuid.uuid4().hex
        self.outputs[ident] = StagedOutput(persist, title)
        return ident

    async def save_outputs(self, db):
        return {
            ident: await item.persist(db, self.id)
            for ident, item in self.outputs.items()
        }

    async def attach(self, files, message_index):
        count = len(self.attachments)
        size = sum(len(item.data) for item in self.attachments)
        if count + len(files) > MAX_ATTACHMENTS:
            raise HTTPException(413, "Too many temporary chat attachments")
        additions = []
        for upload, image in files:
            data = await upload.read(MAX_ATTACHMENTS_BYTES - size + 1)
            size += len(data)
            if size > MAX_ATTACHMENTS_BYTES:
                raise HTTPException(413, "Temporary chat attachments exceed 64 MiB")
            filename = attachment_filename(upload.filename)
            pages = (
                [] if image else (await rag.extract_content_async(data, filename)).pages
            )
            chunks = await asyncio.to_thread(rag.chunk_pages, pages) if pages else []
            additions.append(
                Attachment(
                    uuid.uuid4(),
                    filename,
                    data,
                    image,
                    message_index,
                    [(uuid.uuid4(), c) for c in chunks],
                    len(pages),
                )
            )
        self.attachments.extend(additions)
        return additions

    def search(self, query, limit, *, document_ids, exact_text=None):
        terms = set(query.casefold().split())
        matches = []
        for item in self.attachments:
            if item.id not in document_ids:
                continue
            for chunk_id, chunk in item.chunks:
                if exact_text and exact_text.casefold() not in chunk.text.casefold():
                    continue
                score = len(terms.intersection(chunk.text.casefold().split()))
                matches.append(
                    (
                        score,
                        rag.RetrievedSource(
                            document_id=str(item.id),
                            document_filename=item.filename,
                            chunk_id=str(chunk_id),
                            text=chunk.text,
                            page_number=chunk.page_number,
                            line_start=chunk.line_start,
                            line_end=chunk.line_end,
                            chunk_type=chunk.chunk_type,
                            score=float(score),
                            vector_sim=0,
                            bm25_score=float(score),
                        ),
                    )
                )
        matches.sort(key=lambda match: match[0], reverse=True)
        return [source for _, source in matches[:limit]]

    async def save_attachments(self, db, message_ids, written):
        for item in self.attachments:
            written.append(item.id)
            path = await rag.save_uploaded_file(item.data, item.filename, item.id)
            db.add(
                Document(
                    id=item.id,
                    filename=item.filename,
                    original_filename=item.filename,
                    mime_type=rag.guess_mime_type(item.filename),
                    file_path=path,
                    file_size_bytes=len(item.data),
                    content_hash=hashlib.sha256(item.data).hexdigest(),
                    scope="private",
                    conversation_id=self.id,
                    message_id=message_ids.get(item.message_index),
                    total_pages=item.pages or None,
                    total_chunks=len(item.chunks),
                    digestion_status="ready",
                )
            )
            await db.flush()
            db.add_all(
                [
                    DocumentChunk(
                        id=chunk_id,
                        document_id=item.id,
                        chunk_index=chunk.chunk_index,
                        text=rag._sanitize_text_for_pg(chunk.text),
                        page_number=chunk.page_number,
                        line_start=chunk.line_start,
                        line_end=chunk.line_end,
                        chunk_type=chunk.chunk_type,
                    )
                    for chunk_id, chunk in item.chunks
                ]
            )

    def artifact_catalog(self, offset):
        items = [
            {"id": str(item.id), "title": item.filename, "kind": "upload", "version": 1}
            for item in self.attachments
            if not item.image
        ] + [
            {
                "id": str(ident),
                "title": self.reports[str(ident)]["filename"],
                "kind": self.reports[str(ident)]["type"],
                "version": len(versions),
            }
            for ident, versions in self.versions.items()
        ]
        end = offset + 20
        return {
            "artifacts": items[offset:end],
            "next_offset": end if end < len(items) else None,
        }

    async def register_report(self, result):
        ident = uuid.UUID(result["report_id"])
        sidecar = self.reports_dir() / f"{ident}.source.json"
        payload = json.loads(
            await asyncio.to_thread(sidecar.read_text, encoding="utf-8")
        )
        source = payload["source"]
        self.versions[ident] = [
            ArtifactVersion(
                artifact_id=ident,
                number=1,
                source=source,
                settings=payload["settings"],
                sections=sections_for(source, payload["kind"]),
                outputs={result["format"]: result["file_path"]},
            )
        ]
        self.reports[str(ident)] = dict(result)
        result["artifact_id"] = str(ident)
        result["version"] = 1

    def artifact_version(self, ident, number):
        if versions := self.versions.get(ident):
            number = number or len(versions)
            if number > len(versions):
                raise HTTPException(404, "Artifact version not found")
            report = self.reports[str(ident)]
            return report["type"], report["filename"], versions[number - 1]
        item = next(
            (item for item in self.attachments if item.id == ident and not item.image),
            None,
        )
        if item is None or number not in (None, 1):
            raise HTTPException(404, "Artifact version not found")
        sections = [
            {
                "id": f"page-{page or 1}",
                "title": f"Page {page or 1}",
                "page": page,
                "content": "\n\n".join(chunk.text for _, chunk in chunks),
            }
            for page, chunks in groupby(item.chunks, lambda pair: pair[1].page_number)
        ]
        return (
            "upload",
            item.filename,
            ArtifactVersion(
                artifact_id=ident,
                number=1,
                source="\n\n".join(s["content"] for s in sections),
                sections=sections,
                settings={},
                outputs={},
            ),
        )

    async def save_reports(self, db, written_files):
        from app.services.artifacts import file_path

        saved = {}
        for ident, versions in self.versions.items():
            result = self.reports[str(ident)]
            db.add(
                Artifact(
                    id=ident,
                    title=result["filename"],
                    kind=result["type"],
                    conversation_id=self.id,
                    current_version=len(versions),
                )
            )
            await db.flush()
            for version in versions:
                # Keep staged ORM objects independent of the database transaction
                # so a failed save can be retried.
                db.add(
                    ArtifactVersion(
                        artifact_id=ident,
                        number=version.number,
                        source=version.source,
                        settings=version.settings,
                        sections=version.sections,
                        outputs=version.outputs,
                    )
                )
                for fmt, path in version.outputs.items():
                    target = file_path(path)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    written_files.append(target)
                    await asyncio.to_thread(
                        shutil.copy2, self.reports_dir() / target.name, target
                    )
                    report_id = target.stem
                    saved[report_id] = {
                        **self.reports[report_id],
                        "artifact_id": str(ident),
                        "version": version.number,
                        "download_url": f"/api/artifacts/{ident}/download/{fmt}?version={version.number}",
                    }
        await db.flush()
        return saved


def stage_learning_result(chat, call, body, save, kind, id_key, **counts):
    from app.agent.base import ToolResult

    async def persist(db, conversation_id):
        item = await save(
            db, body.model_copy(update={"source_conversation_id": conversation_id})
        )
        return {"type": kind, id_key: str(item.id), "title": body.title, **counts}

    ident = chat.stage_output(body.title, persist)
    call.status = "completed"
    call.completed_at = time.time()
    call.gen_results = [
        {"type": "temporary_output", "temporary_output_id": ident, "title": body.title}
    ]
    call.output = json.dumps(
        {
            "status": "staged",
            "title": body.title,
            "message": "Created for this temporary chat. It will appear in Learn when the user saves the chat.",
            **counts,
        },
        ensure_ascii=False,
    )
    return ToolResult(True, call.output, call)


_sessions: dict[uuid.UUID, TemporaryChat] = {}


def get_session(ident: uuid.UUID, *, create: bool = True):
    now = time.monotonic()
    for key, chat in list(_sessions.items()):
        if not chat.lock.locked() and now - chat.touched > SESSION_IDLE_SECONDS:
            discard_session(key)
    chat = _sessions.get(ident)
    if chat is None and create:
        if len(_sessions) >= MAX_SESSIONS:
            raise HTTPException(429, "Too many temporary chats")
        chat = _sessions[ident] = TemporaryChat(ident)
    if chat:
        chat.touched = now
    return chat


def discard_session(ident: uuid.UUID):
    chat = _sessions.pop(ident, None)
    if chat and chat.directory:
        chat.directory.cleanup()
