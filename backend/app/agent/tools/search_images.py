"""Search saved images through the same Lens retrieval pipeline as Workspace."""

import asyncio
import base64
import json
import time
from uuid import UUID

from app.agent.base import BaseTool, ToolCall, ToolResult, ToolType, tool_registry
from app.agent.tools.config_base import (
    ConfigField,
    ToolConfigDefinition,
    register_config,
)
from app.db.session import async_session_factory
from app.services.lens.config import get_lens_config
from app.services.lens.search import (
    CHAT_ATTACHMENTS,
    DOCUMENT_IMAGES,
    GENERATED_IMAGES,
    WORKSPACE_FILES,
    LensVisualUnavailable,
    find_images,
)
from app.services.ocr import MAX_IMAGE_BYTES

MAX_MATCHES = 5
EXCERPT_CHARS = 600
SOURCES = {
    CHAT_ATTACHMENTS: ("Chat attachments", "Images uploaded directly to saved chats."),
    WORKSPACE_FILES: (
        "Workspace image files",
        "Standalone image files saved in Workspace.",
    ),
    GENERATED_IMAGES: (
        "Generated images",
        "Images created by the agent in saved chats.",
    ),
    DOCUMENT_IMAGES: (
        "Images from documents",
        "Images extracted from ingested documents.",
    ),
}


def _excerpt(text: str, query: str) -> str:
    text = " ".join(text.split())
    offset = text.lower().find(query.lower()) if query else -1
    start = max(0, offset - 100)
    end = start + EXCERPT_CHARS
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


class SearchImagesTool(BaseTool):
    name = "search_images"
    display_name = "Search Images"
    description = (
        "Find saved images and screenshots by visual description, filename, or visible text. "
        "Default to images saved in this chat. Use library scope only if the user asks to "
        "search their saved image library or past chats. To find similar images from an image "
        "attached to this turn, provide its 1-based reference_image number instead of a query. "
        "When more detail is needed, pass a returned image_id to the Vision tool as lens_image_id."
    )
    tool_type = ToolType.IMAGE_SEARCH

    def get_parameters(self):
        return {
            "query": {
                "type": "string",
                "maxLength": 500,
                "description": "What the image depicts, its filename, or text visible in it.",
            },
            "scope": {
                "type": "string",
                "enum": ["chat", "library"],
                "description": "Search this chat, or the saved library when explicitly requested.",
            },
            "reference_image": {
                "type": "integer",
                "minimum": 1,
                "description": "1-based number of an image attached to the current message.",
            },
        }

    def get_required_params(self):
        return []

    async def execute(
        self,
        *,
        query="",
        scope=None,
        reference_image=None,
        _conversation_id=None,
        _allow_library=False,
        _images=(),
        **kwargs,
    ):
        query = (query or "").strip()
        scope = scope or ("library" if _allow_library else "chat")
        if scope not in {"chat", "library"}:
            return self.invalid_arguments("Use chat or library as the search scope.")
        if not query and reference_image is None:
            return self.invalid_arguments(
                "Provide a query or an attached reference image."
            )
        if query and reference_image is not None:
            return self.invalid_arguments(
                "Provide either a query or a reference image."
            )
        if reference_image is not None and not 1 <= reference_image <= len(_images):
            return self.invalid_arguments("Choose an image attached to this turn.")
        tc = ToolCall(
            id="",
            type=self.tool_type,
            name=self.name,
            status="completed",
            title=self.display_name,
            started_at=time.time(),
            query=query or None,
        )

        def finish(message, *, success=True):
            tc.completed_at = time.time()
            tc.output = message
            return ToolResult(success, message, tc)

        if scope == "library" and not _allow_library:
            return finish(
                "The user must request a search of the saved image library.",
                success=False,
            )
        if scope == "chat" and not _conversation_id:
            return finish(
                "This chat has no saved images. The saved image library is available on request."
            )
        from app.agent.tools.config_store import get_tool_config

        config = get_tool_config(self.name) or {}
        switches = config.get("custom", {}).get("sources", {})
        source_types = tuple(source for source in SOURCES if switches.get(source, True))
        if not source_types:
            return finish(
                "All image sources are turned off in Brain → Tools → Search Images."
            )

        reference = None
        if reference_image is not None:
            encoded = _images[reference_image - 1]
            if encoded.startswith("data:"):
                encoded = encoded.partition(",")[2]
            if len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
                return finish(
                    "The attached reference image exceeds 20 MiB.", success=False
                )
            try:
                reference = await asyncio.to_thread(
                    base64.b64decode, encoded, validate=True
                )
                if not reference or len(reference) > MAX_IMAGE_BYTES:
                    raise ValueError
            except ValueError:
                return finish(
                    "The attached reference image could not be read.", success=False
                )

        try:
            async with async_session_factory() as db:
                matches = await find_images(
                    db,
                    get_lens_config(),
                    query=query,
                    reference=reference,
                    conversation_id=(
                        UUID(str(_conversation_id)) if scope == "chat" else None
                    ),
                    source_types=source_types,
                    limit=MAX_MATCHES,
                    include_ocr=True,
                )
        except LensVisualUnavailable as exc:
            return finish(str(exc), success=False)
        except (ValueError, OSError):
            return finish("The image search input could not be read.", success=False)

        if not matches:
            return finish(
                "No indexed images matched in this chat."
                if scope == "chat"
                else "No indexed images matched in the saved image library."
            )
        tc.image_results = [
            {
                "id": str(item["id"]),
                "filename": item["filename"],
                "excerpt": _excerpt(item["ocr_text"] or "", query),
                **(
                    {"page_number": item["page_number"]}
                    if item.get("page_number")
                    else {}
                ),
            }
            for item in matches
        ]
        tc.completed_at = time.time()
        return ToolResult(
            True,
            json.dumps(
                [
                    {
                        "image_id": row["id"],
                        "filename": row["filename"],
                        "ocr_excerpt": row["excerpt"],
                        **(
                            {"page_number": row["page_number"]}
                            if "page_number" in row
                            else {}
                        ),
                    }
                    for row in tc.image_results
                ],
                ensure_ascii=False,
            ),
            tc,
        )


def _validate_sources(custom):
    sources = custom.get("sources", {})
    if not isinstance(sources, dict) or any(
        key not in SOURCES or not isinstance(value, bool)
        for key, value in sources.items()
    ):
        raise ValueError("Image sources must be recognized boolean switches.")
    return custom


register_config(
    ToolConfigDefinition(
        tool_name=SearchImagesTool.name,
        display_name=SearchImagesTool.display_name,
        description=SearchImagesTool.description,
        custom_defaults={"sources": dict.fromkeys(SOURCES, True)},
        custom_schema=[
            {
                "key": "sources",
                "label": "Image sources",
                "fields": [
                    ConfigField(
                        f"sources.{source}", label, "bool", default=True, help=help_text
                    ).to_dict()
                    for source, (label, help_text) in SOURCES.items()
                ],
            }
        ],
        validate_custom=_validate_sources,
    )
)
tool_registry.register(SearchImagesTool())
