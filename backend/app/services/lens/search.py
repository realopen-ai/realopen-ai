"""Lexical and pgvector candidate retrieval for stored images."""

import logging
import re
from statistics import median

from sqlalchemy import Text, and_, case, func, literal, or_, select
from sqlalchemy.dialects.postgresql import ARRAY

from app.db.models import Document, DocumentChunk, LensImage, LensPassage, Message
from .encoders import image_vectors, model_version, text_vectors

logger = logging.getLogger(__name__)

DEPTH = 50


class LensVisualUnavailable(Exception):
    pass


CHAT_ATTACHMENTS = "chat_attachments"
WORKSPACE_FILES = "workspace_files"
GENERATED_IMAGES = "generated_images"
DOCUMENT_IMAGES = "document_images"


def gallery_item(row):
    data = dict(row)
    return {
        **data,
        "filename": data["filename"] or f"Generated image {data['output_index']}",
    }


def gallery_rows(*, conversation_id=None, source_types=None):
    conversation = func.coalesce(Document.conversation_id, Message.conversation_id)
    source_type = case(
        (LensImage.document_chunk_id.is_not(None), DOCUMENT_IMAGES),
        (LensImage.message_id.is_not(None), GENERATED_IMAGES),
        (Document.message_id.is_not(None), CHAT_ATTACHMENTS),
        else_=WORKSPACE_FILES,
    )
    gallery = (
        select(
            LensImage.id,
            Document.original_filename.label("filename"),
            conversation.label("conversation_id"),
            func.coalesce(Document.created_at, Message.created_at).label("created_at"),
            Document.id.label("document_id"),
            LensImage.document_chunk_id,
            case(
                (
                    Document.mime_type.in_(
                        (
                            "application/pdf",
                            "application/vnd.ms-powerpoint",
                            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                        )
                    ),
                    DocumentChunk.page_number,
                ),
            ).label("page_number"),
            LensImage.output_index,
        )
        .select_from(LensImage)
        .outerjoin(DocumentChunk, DocumentChunk.id == LensImage.document_chunk_id)
        .outerjoin(
            Document,
            Document.id
            == func.coalesce(LensImage.document_id, DocumentChunk.document_id),
        )
        .outerjoin(Message, Message.id == LensImage.message_id)
        .where(
            LensImage.thumbnail.is_not(None),
            LensImage.error.is_(None),
            or_(
                Document.mime_type.startswith("image/"),
                Message.id.is_not(None),
                and_(
                    DocumentChunk.chunk_type == "image_description",
                    DocumentChunk.image_path.is_not(None),
                    Document.id.is_not(None),
                ),
            ),
        )
    )
    if conversation_id is not None:
        gallery = gallery.where(conversation == conversation_id)
    if source_types is not None:
        gallery = gallery.where(source_type.in_(source_types))
    return gallery


async def exact_candidates(db, query: str, conversation_id=None, source_types=None):
    needle = query.lower()
    ocr = func.lower(LensImage.ocr_text)
    matches = [func.strpos(ocr, needle) > 0]
    if any(separator in needle for separator in "_:/.-"):
        # Tesseract can insert spaces into identifiers, e.g. ERR_CONN_ 1042.
        compact = func.regexp_replace(ocr, r"\s*([_:/.-])\s*", r"\1", "g")
        matches.append(func.strpos(compact, needle) > 0)
    matches.append(func.strpos(func.lower(Document.original_filename), needle) > 0)
    phrase_match = or_(*matches)
    terms = list(dict.fromkeys(re.findall(r"[^\W_]{3,}", needle)))
    if len(terms) > 1:
        ocr_words = func.regexp_split_to_array(ocr, "[^[:alnum:]]+", type_=ARRAY(Text))
        matches.append(ocr_words.contains(terms))
    return list(
        await db.scalars(
            gallery_rows(conversation_id=conversation_id, source_types=source_types)
            .with_only_columns(LensImage.id)
            .where(or_(*matches))
            .order_by(phrase_match.desc().nulls_last(), LensImage.id)
            .limit(DEPTH)
        )
    )


async def visual_scores(db, config, vector, conversation_id=None, source_types=None):
    if vector is None:
        return {}
    distance = LensImage.visual_embedding.cosine_distance(vector)
    return dict(
        (
            await db.execute(
                gallery_rows(conversation_id=conversation_id, source_types=source_types)
                .with_only_columns(LensImage.id, literal(1.0) - distance)
                .where(
                    LensImage.visual_version
                    == model_version("siglip2", config.siglip2),
                    LensImage.visual_embedding.is_not(None),
                )
                .order_by(distance, LensImage.id)
                .limit(DEPTH)
            )
        ).all()
    )


async def semantic_scores(db, config, vector, conversation_id=None, source_types=None):
    if vector is None:
        return {}, 0
    similarity = (1.0 - func.min(LensPassage.embedding.cosine_distance(vector))).label(
        "similarity"
    )
    ranked = (
        gallery_rows(conversation_id=conversation_id, source_types=source_types)
        .join_from(LensImage, LensPassage, LensPassage.image_id == LensImage.id)
        .with_only_columns(LensPassage.image_id, similarity)
        .where(LensImage.semantic_version == model_version("e5", config.e5))
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


async def _query_vector(config, ident, query):
    try:
        result = await text_vectors(config, ident, [query])
        return result[0][0] if result else None
    except Exception as exc:
        logger.warning(
            "Lens %s query encoder unavailable (%s)", ident, type(exc).__name__
        )
        return None


async def find_images(
    db,
    config,
    *,
    query="",
    reference=None,
    conversation_id=None,
    source_types=None,
    limit=DEPTH,
    include_ocr=False,
):
    """Search one conversation or the saved library; scope every retrieval channel."""
    if source_types is not None and not source_types:
        return []
    if reference is not None:
        vectors = await image_vectors(config, [reference])
        if vectors is None:
            raise LensVisualUnavailable(
                "Install the profile's SigLIP2 model in Brain → Models."
            )
        ids = list(
            await visual_scores(db, config, vectors[0], conversation_id, source_types)
        )
    else:
        visual_vector = await _query_vector(config, "siglip2", query)
        semantic_vector = await _query_vector(config, "e5", query)
        exact = await exact_candidates(db, query, conversation_id, source_types)
        visual = await visual_scores(
            db, config, visual_vector, conversation_id, source_types
        )
        semantic, background = await semantic_scores(
            db, config, semantic_vector, conversation_id, source_types
        )
        ids = rank(exact, visual, semantic, background)
    ids = ids[:limit]
    if not ids:
        return []
    gallery = gallery_rows(
        conversation_id=conversation_id, source_types=source_types
    ).where(LensImage.id.in_(ids))
    if include_ocr:
        gallery = gallery.add_columns(LensImage.ocr_text)
    rows = (await db.execute(gallery)).mappings()
    items = {row["id"]: gallery_item(row) for row in rows}
    return [items[ident] for ident in ids if ident in items]
