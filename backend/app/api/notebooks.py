import uuid
from datetime import datetime, timezone
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_db
from app.db.models import Notebook, NotebookItem, Conversation
from app.learn import notebooks as service
from app.services import conversations
from app.services.chat_streams import get_stream

router = APIRouter(prefix="/learn/notebooks", tags=["learn"])


def require_idle(notebook):
    stream = get_stream(str(notebook.conversation_id))
    if stream and not stream.done:
        raise HTTPException(
            409, "Wait for the notebook response to finish before changing its sources"
        )


class LinkInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["document", "artifact", "note", "deck"]
    target_id: uuid.UUID


class SelectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected: bool


@router.get("")
async def list_notebooks(db: AsyncSession = Depends(get_db)):
    rows = (
        (await db.execute(select(Notebook).order_by(Notebook.updated_at.desc(), Notebook.id)))
        .scalars()
        .all()
    )
    return [await service.detail(db, row) for row in rows]


@router.post("", status_code=201)
async def create(body: service.NotebookInput, db: AsyncSession = Depends(get_db)):
    conversation = await conversations.create_conversation(db, title=body.title)
    conversation.is_notebook = True
    notebook = Notebook(**body.model_dump(), conversation_id=conversation.id)
    db.add(notebook)
    await db.flush()
    return await service.detail(db, notebook)


@router.get("/{notebook_id}")
async def detail(notebook_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return await service.detail(db, await service.get_notebook(db, notebook_id))


@router.put("/{notebook_id}")
async def update(
    notebook_id: uuid.UUID, body: service.NotebookInput, db: AsyncSession = Depends(get_db)
):
    notebook = await service.get_notebook(db, notebook_id, lock=True)
    notebook.title, notebook.description = body.title, body.description
    notebook.updated_at = datetime.now(timezone.utc)
    conversation = await db.get(Conversation, notebook.conversation_id)
    if conversation:
        conversation.title = body.title
    return await service.detail(db, notebook)


@router.delete("/{notebook_id}", status_code=204)
async def remove(notebook_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    notebook = await service.get_notebook(db, notebook_id, lock=True)
    require_idle(notebook)
    # Keep the conversation as archived provenance: generated files and notes
    # reference it. Do not cascade-delete those assets with the notebook.
    conversation = await db.get(Conversation, notebook.conversation_id)
    if conversation:
        conversation.archived = True
        conversation.is_notebook = False
        conversation.archived_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.execute(delete(NotebookItem).where(NotebookItem.notebook_id == notebook.id))
    await db.delete(notebook)
    return Response(status_code=204)


@router.post("/{notebook_id}/items")
async def attach(notebook_id: uuid.UUID, body: LinkInput, db: AsyncSession = Depends(get_db)):
    notebook = await service.get_notebook(db, notebook_id, lock=True)
    require_idle(notebook)
    await service.attach(db, notebook, body.kind, body.target_id)
    return await service.detail(db, notebook)


async def item(db, notebook_id, item_id):
    require_idle(await service.get_notebook(db, notebook_id, lock=True))
    link = await db.get(NotebookItem, item_id)
    if link is None or link.notebook_id != notebook_id:
        raise HTTPException(404, "Notebook item not found")
    return link


@router.patch("/{notebook_id}/items/{item_id}")
async def select_item(
    notebook_id: uuid.UUID,
    item_id: uuid.UUID,
    body: SelectionInput,
    db: AsyncSession = Depends(get_db),
):
    link = await item(db, notebook_id, item_id)
    link.selected = body.selected
    notebook = await service.get_notebook(db, notebook_id)
    notebook.updated_at = datetime.now(timezone.utc)
    return await service.detail(db, notebook)


@router.delete("/{notebook_id}/items/{item_id}", status_code=204)
async def unlink(notebook_id: uuid.UUID, item_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await db.delete(await item(db, notebook_id, item_id))
    notebook = await service.get_notebook(db, notebook_id)
    notebook.updated_at = datetime.now(timezone.utc)
    return Response(status_code=204)
