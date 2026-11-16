"""Maintain the derived image index from durable chat and document sources."""

import asyncio
import base64
from datetime import UTC, datetime, timedelta
import logging

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert

from app.config import settings
from app.db.models import Document, LensImage, LensPassage
from app.db.session import async_session_factory
from app.services import ocr
from app.services.safe_paths import confined_path
from app.services.model_manager.lens_models import model_paths
from .config import get_lens_config
from .encoders import image_vectors, model_version, text_vectors, thumbnail

logger = logging.getLogger(__name__)
BATCH_SIZE = 4
OUTPUTS_PATH = '$[*].tool_call.genResults[*] ? (@.type == "image" && @.data.type() == "string").data'
ENCODED_LIMIT = (ocr.MAX_IMAGE_BYTES + 2) // 3 * 4


async def discover(db):
    documents = (
        select(Document.id)
        .where(
            Document.mime_type.startswith("image/", autoescape=True),
            ~select(LensImage.id).where(LensImage.document_id == Document.id).exists(),
        )
        .order_by(Document.created_at, Document.id)
        .limit(32)
    )
    await db.execute(
        insert(LensImage)
        .from_select(["document_id"], documents, include_defaults=False)
        .on_conflict_do_nothing()
    )
    # Derive image identities from the position in a terminal message's immutable
    # result list. Old image-generation outputs use the same JSON structure.
    await db.execute(
        text(
            """
            INSERT INTO lens_images (message_id, output_index)
            SELECT m.id, image.position
            FROM messages m
            CROSS JOIN LATERAL jsonb_path_query(m.blocks, CAST(:path AS jsonpath))
                WITH ORDINALITY AS image(data, position)
            WHERE m.role = 'assistant' AND m.completion_status <> 'streaming'
                AND m.blocks @? '$[*].tool_call.genResults[*] ? (@.type == "image")'
                AND NOT EXISTS (
                    SELECT 1 FROM lens_images i
                    WHERE i.message_id = m.id AND i.output_index = image.position
                )
            ORDER BY m.id, image.position
            LIMIT 32
            ON CONFLICT DO NOTHING
            """
        ),
        {"path": OUTPUTS_PATH},
    )
    await db.commit()


async def source_bytes(db, image: LensImage) -> bytes:
    if image.document_id is not None:
        document = await db.get(Document, image.document_id)
        if not document or not document.mime_type.startswith("image/"):
            raise FileNotFoundError("Saved image was removed.")

        def read():
            path = confined_path(settings.get_data_dir(), document.file_path)
            with path.open("rb") as file:
                data = file.read(ocr.MAX_IMAGE_BYTES + 1)
            if len(data) > ocr.MAX_IMAGE_BYTES:
                raise ValueError("Image exceeds 20 MiB.")
            return data

        return await asyncio.to_thread(read)

    encoded = await db.scalar(
        text(
            """
            SELECT value #>> '{}'
            FROM messages m CROSS JOIN LATERAL
                jsonb_path_query(m.blocks, CAST(:path AS jsonpath))
                WITH ORDINALITY AS image(value, position)
            WHERE m.id = :message AND image.position = :position
                AND length(value #>> '{}') <= :limit
            """
        ),
        {
            "path": OUTPUTS_PATH,
            "message": image.message_id,
            "position": image.output_index,
            "limit": ENCODED_LIMIT,
        },
    )
    if encoded is None:
        raise FileNotFoundError("Generated image is unavailable or too large.")
    return await asyncio.to_thread(base64.b64decode, encoded, validate=True)


async def _recoverable_job(db, condition):
    return list(
        await db.scalars(
            select(LensImage)
            .where(
                condition,
                LensImage.error.is_(None),
                or_(LensImage.retry_at.is_(None), LensImage.retry_at <= func.now()),
            )
            .order_by(LensImage.id)
            .limit(BATCH_SIZE)
        )
    )


async def _sources(db, rows):
    data = []
    for row in rows:
        try:
            data.append((row, await source_bytes(db, row)))
        except (ValueError, OSError):
            row.error = "Image file is unavailable or invalid."
    return data


async def index_batch() -> bool:
    config = get_lens_config()
    async with async_session_factory() as db:
        await discover(db)

        visual_ready, semantic_ready = await asyncio.gather(
            asyncio.to_thread(model_paths, "siglip2", config.siglip2),
            asyncio.to_thread(model_paths, "e5", config.e5),
        )
        previews = await _recoverable_job(db, LensImage.thumbnail.is_(None))
        for row, data in await _sources(db, previews):
            try:
                row.thumbnail = await asyncio.to_thread(thumbnail, data)
            except (ValueError, OSError):
                row.error = "Image file is unavailable or invalid."
        await db.commit()

        ocr_rows = await _recoverable_job(
            db, LensImage.thumbnail.is_not(None) & LensImage.ocr_text.is_(None)
        )
        language = await asyncio.to_thread(ocr.default_language) if ocr_rows else "eng"
        for row, data in await _sources(db, ocr_rows):
            try:
                result = await ocr.recognize(data, language)
                row.ocr_text = result.text.replace("\0", "")[:32_000]
                row.retry_at = None
            except Exception as exc:
                logger.debug("Lens OCR failed (%s)", type(exc).__name__)
                row.retry_at = datetime.now(UTC) + timedelta(minutes=5)
        await db.commit()

        visual = []
        if visual_ready:
            version = model_version("siglip2", config.siglip2)
            visual = await _recoverable_job(
                db,
                LensImage.thumbnail.is_not(None)
                & LensImage.visual_version.is_distinct_from(version),
            )
            sources = await _sources(db, visual)
            if sources:
                embeddings = await image_vectors(config, [data for _, data in sources])
                if embeddings is not None:
                    for (row, _), vector in zip(sources, embeddings, strict=True):
                        row.visual_embedding = vector
                        row.visual_version = version
            await db.commit()

        semantic = []
        if semantic_ready:
            version = model_version("e5", config.e5)
            semantic = await _recoverable_job(
                db,
                (LensImage.ocr_text != "")
                & LensImage.semantic_version.is_distinct_from(version),
            )
            if semantic:
                embeddings = await text_vectors(
                    config, "e5", [row.ocr_text for row in semantic], passages=True
                )
                if embeddings is not None:
                    for row, vectors in zip(semantic, embeddings, strict=True):
                        await db.execute(
                            delete(LensPassage).where(LensPassage.image_id == row.id)
                        )
                        if vectors:
                            await db.execute(
                                insert(LensPassage)
                                .values(
                                    [
                                        {
                                            "image_id": row.id,
                                            "position": i,
                                            "embedding": vector,
                                        }
                                        for i, vector in enumerate(vectors)
                                    ]
                                )
                                .on_conflict_do_nothing()
                            )
                        row.semantic_version = version
            await db.commit()
        return bool(previews or ocr_rows or visual or semantic)


async def monitor():
    while True:
        try:
            more = await index_batch()
        except Exception:
            logger.exception("Lens indexing paused until the next scan")
            more = False
        await asyncio.sleep(1 if more else 20)
