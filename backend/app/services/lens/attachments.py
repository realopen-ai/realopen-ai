"""Persist saved chat image attachments in the existing document store."""

import asyncio
import hashlib
import logging
import shutil
from uuid import uuid4

from app.db.models import Document
from app.db.session import async_session_factory
from app.services import rag
from app.services.temporary_chats import attachment_filename
from .encoders import image_mime

logger = logging.getLogger(__name__)


async def save_images(conversation_id, message_id, attachments):
    written = []
    try:
        async with async_session_factory() as db:
            for filename, content in attachments:
                mime = await asyncio.to_thread(image_mime, content)
                if mime is None:
                    continue
                ident = uuid4()
                written.append(ident)
                filename = attachment_filename(filename)
                path = await rag.save_uploaded_file(content, filename, ident)
                digest = await asyncio.to_thread(
                    lambda: hashlib.sha256(content).hexdigest()
                )
                db.add(
                    Document(
                        id=ident,
                        filename=filename,
                        original_filename=filename,
                        mime_type=mime,
                        file_path=path,
                        file_size_bytes=len(content),
                        content_hash=digest,
                        scope="private",
                        conversation_id=conversation_id,
                        message_id=message_id,
                        digestion_status="ready",
                    )
                )
            await db.commit()
    except BaseException as exc:
        for ident in written:
            await asyncio.to_thread(shutil.rmtree, rag.get_document_dir(ident), True)
        if not isinstance(exc, Exception):
            raise
        logger.exception("Could not save chat image attachments")
