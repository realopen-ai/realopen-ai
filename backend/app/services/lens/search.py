"""Lexical and pgvector candidate retrieval for stored images."""

from statistics import median

from sqlalchemy import func, literal, or_, select

from app.db.models import Document, LensImage, LensPassage, Message
from .encoders import model_version

DEPTH = 50


def gallery_rows():
    return (
        select(
            LensImage.id,
            Document.original_filename.label("filename"),
            func.coalesce(Document.conversation_id, Message.conversation_id).label(
                "conversation_id"
            ),
            func.coalesce(Document.created_at, Message.created_at).label("created_at"),
            LensImage.document_id,
            LensImage.output_index,
        )
        .outerjoin(Document, Document.id == LensImage.document_id)
        .outerjoin(Message, Message.id == LensImage.message_id)
        .where(
            LensImage.thumbnail.is_not(None),
            LensImage.error.is_(None),
            or_(Document.mime_type.startswith("image/"), Message.id.is_not(None)),
        )
    )


async def exact_candidates(db, query: str):
    needle = query.lower()
    ocr = func.lower(LensImage.ocr_text)
    matches = [func.strpos(ocr, needle) > 0]
    if any(separator in needle for separator in "_:/.-"):
        # Tesseract can insert spaces into identifiers, e.g. ERR_CONN_ 1042.
        compact = func.regexp_replace(ocr, r"\s*([_:/.-])\s*", r"\1", "g")
        matches.append(func.strpos(compact, needle) > 0)
    return list(
        await db.scalars(
            select(LensImage.id)
            .outerjoin(Document, Document.id == LensImage.document_id)
            .where(
                LensImage.thumbnail.is_not(None),
                LensImage.error.is_(None),
                or_(
                    *matches,
                    func.strpos(func.lower(Document.original_filename), needle) > 0,
                ),
            )
            .order_by(LensImage.id)
            .limit(DEPTH)
        )
    )


async def visual_scores(db, config, vector):
    if vector is None:
        return {}
    distance = LensImage.visual_embedding.cosine_distance(vector)
    return dict(
        (
            await db.execute(
                select(LensImage.id, (literal(1.0) - distance))
                .where(
                    LensImage.visual_version
                    == model_version("siglip2", config.siglip2),
                    LensImage.visual_embedding.is_not(None),
                    LensImage.error.is_(None),
                )
                .order_by(distance, LensImage.id)
                .limit(DEPTH)
            )
        ).all()
    )


async def semantic_scores(db, config, vector):
    if vector is None:
        return {}, 0
    similarity = (1.0 - func.min(LensPassage.embedding.cosine_distance(vector))).label(
        "similarity"
    )
    ranked = (
        select(LensPassage.image_id, similarity)
        .join(LensImage, LensImage.id == LensPassage.image_id)
        .where(
            LensImage.semantic_version == model_version("e5", config.e5),
            LensImage.error.is_(None),
        )
        .group_by(LensPassage.image_id)
        .cte()
    )
    top = await db.execute(
        select(ranked.c.image_id, ranked.c.similarity)
        .order_by(ranked.c.similarity.desc(), ranked.c.image_id)
        .limit(DEPTH)
    )
    scores = dict(top.all())
    if not scores:
        return {}, 0
    background = await db.scalar(
        select(func.percentile_cont(0.5).within_group(ranked.c.similarity))
    )
    return scores, background


def rank(exact, visual, semantic, background=None):
    """Preserve visual scores and give OCR one bounded contribution, as in Lens.

    Exact phrases have priority. Permit E5-only results while SigLIP2 is absent
    or its embeddings are being rebuilt.
    """
    exact_ids = set(exact)
    baseline = (
        background
        if background is not None
        else (median(semantic.values()) if semantic else 0)
    )
    scores = dict(visual)
    for ident, similarity in semantic.items():
        strength = max(
            0, min(1, (similarity - 0.83) / 0.06, (similarity - baseline - 0.04) / 0.06)
        )
        if ident in scores:
            scores[ident] += 0.16 * strength
        elif similarity >= 0.83:
            scores[ident] = -2.0 + similarity
    return [
        *exact,
        *sorted(scores.keys() - exact_ids, key=lambda ident: (-scores[ident], ident)),
    ][:DEPTH]
