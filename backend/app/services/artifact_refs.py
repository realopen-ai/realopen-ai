"""Validated provenance shared by Notes and Flashcards."""

import uuid
from pydantic import BaseModel, Field, ConfigDict
from app.services.artifacts import get_artifact, get_version, read


class ArtifactReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_id: uuid.UUID
    version: int = Field(ge=1)
    section_id: str = Field(min_length=1, max_length=128)
    # UI round-trips derived display metadata; validation always re-derives it
    # from the immutable section instead of trusting caller-supplied values.
    page: int | None = Field(default=None, ge=1)
    slide: int | None = Field(default=None, ge=1)
    sheet: str | None = Field(default=None, max_length=512)


async def validate_reference(db, reference, conversation_id):
    if reference is None:
        return None
    artifact = await get_artifact(db, reference.artifact_id, conversation_id, scoped=True)
    version = await get_version(db, artifact, reference.version)
    section = read(version, reference.section_id, limit=1)
    return {
        "artifact_id": str(artifact.id),
        "version": version.number,
        "section_id": reference.section_id,
        **{key: section[key] for key in ("page", "slide", "sheet") if key in section},
    }


REFERENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "artifact_id": {"type": "string"},
        "version": {"type": "integer", "minimum": 1},
        "section_id": {"type": "string"},
    },
    "required": ["artifact_id", "version", "section_id"],
    "additionalProperties": False,
    "description": "Optional exact provenance from read_artifact; never invent references.",
}
