"""Search and preview the local Lens image library."""

import asyncio
import logging
from pathlib import Path
from urllib.parse import quote
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
    image_rgb,
    image_mime,
    LensEncoderUnavailable,
    require_local_models,
    model_version,
)
from app.services.lens.index import source_bytes
from app.services import ocr
from app.services.model_manager.lens_models import model_paths
from app.services.ocr import MAX_IMAGE_BYTES

router = APIRouter(prefix="/lens", tags=["lens"])
logger = logging.getLogger(__name__)


@router.post("/ocr")
async def extract_image_text(file: UploadFile = File(...)):
    data = await file.read(MAX_IMAGE_BYTES + 1)
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(422, "Upload a supported image under 20 MiB.")
    if await asyncio.to_thread(image_mime, data) is None:
        raise HTTPException(422, "Upload a supported image under 20 megapixels.")
    try:
        language = await asyncio.to_thread(ocr.default_language)
        result = await ocr.recognize(data, language)
    except (ValueError, OSError) as exc:
        raise HTTPException(
            422, "The image could not be read for text extraction."
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            503,
            "Text extraction is unavailable. Check Tesseract in Settings → Dependencies.",
        ) from exc
    return {"text": result.text}


async def _state(db, config):
    visual_ready, semantic_ready = await asyncio.gather(
        asyncio.to_thread(model_paths, "siglip2", config.siglip2),
        asyncio.to_thread(model_paths, "e5", config.e5),
    )
    indexing_error = None
    try:
        await asyncio.to_thread(require_local_models, visual_ready)
        await asyncio.to_thread(require_local_models, semantic_ready)
    except LensEncoderUnavailable as exc:
        indexing_error = str(exc)
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
        "indexing_error": indexing_error,
    }


@router.get("/images")
async def list_images(
    q: str = Query(default="", max_length=500),
    offset: int = Query(default=0, ge=0, le=100_000),
    db: AsyncSession = Depends(get_db),
):
    config = get_lens_config()
    if q.strip():
        try:
            items = await search.find_images(db, config, query=q.strip())
        except LensEncoderUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        return {
            "items": items,
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
        "items": [search.gallery_item(row) for row in rows[:48]],
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
        items = await search.find_images(db, config, reference=data)
    except (search.LensVisualUnavailable, LensEncoderUnavailable) as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(
            422, "Upload a supported image under 20 MiB and 20 megapixels."
        ) from exc
    except Exception as exc:
        logger.warning("Lens image query encoder unavailable (%s)", type(exc).__name__)
        raise HTTPException(
            503, "SigLIP2 could not read the image. Check the model installation."
        ) from exc
    return {"items": items, "state": await _state(db, config)}


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
    return search.gallery_item(row)


@router.get("/images/{ident}/download")
async def image_download(ident: UUID, db: AsyncSession = Depends(get_db)):
    image = await image_detail(ident, db)
    row = await db.get(LensImage, ident)
    if row is None:
        raise HTTPException(404, "Image not found.")
    try:
        data = await source_bytes(db, row)
        mime = await asyncio.to_thread(image_mime, data)
    except (ValueError, OSError) as exc:
        raise HTTPException(404, "Image not found.") from exc
    if mime is None:
        raise HTTPException(404, "Image not found.")
    extension = {
        "image/jpeg": "jpg",
        "image/mpo": "mpo",
        "image/png": "png",
        "image/webp": "webp",
        "image/tiff": "tiff",
        "image/bmp": "bmp",
        "image/gif": "gif",
    }[mime]
    filename = Path(image["filename"]).stem + "." + extension
    return Response(
        data,
        media_type=mime,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}",
        },
    )


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
