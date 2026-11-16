"""Search and preview the local Lens image library."""

import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import LensImage
from app.db.session import get_db
from app.services.lens import search
from app.services.lens.config import get_lens_config
from app.services.lens.encoders import (
    image_vectors,
    image_rgb,
    model_version,
    text_vectors,
)
from app.services.lens.index import source_bytes
from app.services.model_manager.lens_models import model_paths
from app.services.ocr import MAX_IMAGE_BYTES

router = APIRouter(prefix="/lens", tags=["lens"])
logger = logging.getLogger(__name__)


def _item(row):
    data = dict(row)
    return {
        **data,
        "filename": data["filename"] or f"Generated image {data['output_index']}",
    }


async def _state(db, config):
    visual_ready, semantic_ready = await asyncio.gather(
        asyncio.to_thread(model_paths, "siglip2", config.siglip2),
        asyncio.to_thread(model_paths, "e5", config.e5),
    )
    row = (
        (
            await db.execute(
                select(
                    func.count().filter(LensImage.error.is_(None)).label("total"),
                    func.count().filter(LensImage.error.is_not(None)).label("failed"),
                    func.count()
                    .filter(LensImage.ocr_text.is_not(None), LensImage.error.is_(None))
                    .label("ocr"),
                    func.count()
                    .filter(
                        LensImage.visual_version
                        == model_version("siglip2", config.siglip2),
                        LensImage.error.is_(None),
                    )
                    .label("visual"),
                    func.count()
                    .filter(LensImage.ocr_text != "", LensImage.error.is_(None))
                    .label("with_text"),
                    func.count()
                    .filter(
                        LensImage.semantic_version == model_version("e5", config.e5),
                        LensImage.error.is_(None),
                    )
                    .label("semantic"),
                ).select_from(LensImage)
            )
        )
        .mappings()
        .one()
    )
    return {
        **row,
        "siglip2_installed": bool(visual_ready),
        "e5_installed": bool(semantic_ready),
    }


async def _query_vector(config, ident, query):
    try:
        result = await text_vectors(config, ident, [query])
        return result[0][0] if result else None
    except Exception as exc:
        logger.warning(
            "Lens %s query encoder unavailable (%s)", ident, type(exc).__name__
        )
        return None


@router.get("/images")
async def list_images(
    q: str = Query(default="", max_length=500),
    offset: int = Query(default=0, ge=0, le=100_000),
    db: AsyncSession = Depends(get_db),
):
    config = get_lens_config()
    if q.strip():
        query = q.strip()
        visual_vector = await _query_vector(config, "siglip2", query)
        semantic_vector = await _query_vector(config, "e5", query)
        exact = await search.exact_candidates(db, query)
        visual = await search.visual_scores(db, config, visual_vector)
        semantic, background = await search.semantic_scores(db, config, semantic_vector)
        ids = search.rank(exact, visual, semantic, background)
        rows = (
            await db.execute(search.gallery_rows().where(LensImage.id.in_(ids)))
        ).mappings()
        items = {row["id"]: _item(row) for row in rows}
        return {
            "items": [items[ident] for ident in ids if ident in items],
            "state": await _state(db, config),
        }
    gallery = search.gallery_rows()
    rows = (
        (
            await db.execute(
                gallery.order_by(
                    gallery.selected_columns.created_at.desc(), LensImage.id
                )
                .limit(49)
                .offset(offset)
            )
        )
        .mappings()
        .all()
    )
    return {
        "items": [_item(row) for row in rows[:48]],
        "more": len(rows) > 48,
        "state": await _state(db, config),
    }


@router.post("/similar")
async def similar_images(
    file: UploadFile = File(...), db: AsyncSession = Depends(get_db)
):
    data = await file.read(MAX_IMAGE_BYTES + 1)
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(422, "Upload a supported image under 20 MiB.")
    try:
        config = get_lens_config()
        vectors = await image_vectors(config, [data])
    except (ValueError, OSError) as exc:
        raise HTTPException(
            422, "Upload a supported image under 20 MiB and 20 megapixels."
        ) from exc
    except Exception as exc:
        logger.warning("Lens image query encoder unavailable (%s)", type(exc).__name__)
        raise HTTPException(
            503, "SigLIP2 could not read the image. Check the model installation."
        ) from exc
    if vectors is None:
        raise HTTPException(
            409, "Install the profile's SigLIP2 model in Brain → Models."
        )
    scores = await search.visual_scores(db, config, vectors[0])
    rows = (
        await db.execute(search.gallery_rows().where(LensImage.id.in_(scores)))
    ).mappings()
    items = {row["id"]: _item(row) for row in rows}
    return {
        "items": [items[ident] for ident in scores if ident in items],
        "state": await _state(db, config),
    }


@router.get("/images/{ident}")
async def image_detail(ident: UUID, db: AsyncSession = Depends(get_db)):
    row = (
        (
            await db.execute(
                search.gallery_rows()
                .add_columns(LensImage.ocr_text)
                .where(LensImage.id == ident)
            )
        )
        .mappings()
        .first()
    )
    if not row:
        raise HTTPException(404, "Image not found.")
    return _item(row)


@router.get("/images/{ident}/thumbnail")
async def image_thumbnail(ident: UUID, db: AsyncSession = Depends(get_db)):
    data = await db.scalar(
        select(LensImage.thumbnail).where(
            LensImage.id == ident, LensImage.error.is_(None)
        )
    )
    if data is None:
        raise HTTPException(404, "Image not found.")
    return Response(
        data, media_type="image/webp", headers={"Cache-Control": "no-store"}
    )


@router.get("/images/{ident}/preview")
async def image_preview(ident: UUID, db: AsyncSession = Depends(get_db)):
    row = await db.get(LensImage, ident)
    if row is None or row.thumbnail is None or row.error:
        raise HTTPException(404, "Image not found.")
    try:
        data = await source_bytes(db, row)

        def render():
            from io import BytesIO

            with image_rgb(data) as picture, BytesIO() as output:
                picture.thumbnail((2048, 2048))
                picture.save(output, format="WEBP", quality=90)
                return output.getvalue()

        preview = await asyncio.to_thread(render)
    except (ValueError, OSError) as exc:
        raise HTTPException(404, "Image not found.") from exc
    return Response(
        preview, media_type="image/webp", headers={"Cache-Control": "no-store"}
    )
