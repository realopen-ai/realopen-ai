"""Artifact browser/API. Agent callers additionally enforce conversation scope."""

import uuid
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select
from app.db.session import async_session_factory
from app.db.models import ArtifactVersion
from app.services import artifacts as service

router = APIRouter(prefix="/artifacts", tags=["artifacts"])


class SectionEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_id: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=service.MAX_SOURCE)


class UpdateArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    changes: list[SectionEdit] = Field(min_length=1, max_length=20)


class RestoreArtifact(BaseModel):
    expected_version: int = Field(ge=1)
    version: int = Field(ge=1)


@router.get("")
async def list_artifacts(
    conversation_id: uuid.UUID | None = None,
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    async with async_session_factory() as db:
        result = await service.catalog(
            db, conversation_id, scoped=conversation_id is not None, limit=limit, offset=offset
        )
        await db.commit()
        return result


@router.get("/{artifact_id}")
async def detail(artifact_id: uuid.UUID, version: int | None = Query(None, ge=1)):
    async with async_session_factory() as db:
        artifact = await service.get_artifact(db, artifact_id)
        current = await service.get_version(db, artifact, version)
        versions = (
            (
                await db.execute(
                    select(ArtifactVersion)
                    .where(ArtifactVersion.artifact_id == artifact.id)
                    .order_by(ArtifactVersion.number.desc())
                )
            )
            .scalars()
            .all()
        )
        await db.commit()
        return {
            **service.metadata(artifact),
            "selected_version": current.number,
            "outline": service.read(current),
            "outputs": list(current.outputs),
            "previews": {
                fmt: service.file_path(path).stem
                for fmt, path in current.outputs.items()
                if fmt != "original"
            },
            "versions": [
                {"number": v.number, "created_at": v.created_at, "restored_from": v.restored_from}
                for v in versions
            ],
        }


@router.get("/{artifact_id}/read")
async def read_artifact(
    artifact_id: uuid.UUID,
    version: int | None = Query(None, ge=1),
    section_id: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(service.MAX_READ, ge=1, le=service.MAX_READ),
):
    async with async_session_factory() as db:
        artifact = await service.get_artifact(db, artifact_id)
        current = await service.get_version(db, artifact, version)
        result = service.read(current, section_id, offset, limit)
        await db.commit()
        return result


@router.put("/{artifact_id}")
async def update_artifact(artifact_id: uuid.UUID, body: UpdateArtifact):
    async with async_session_factory() as db:
        artifact = await service.get_artifact(db, artifact_id)
        current = await service.get_version(db, artifact)
        try:
            source = service.edited_source(
                current, artifact.kind, [change.model_dump() for change in body.changes]
            )
            version = await service.publish(db, artifact, body.expected_version, source)
            await db.commit()
        except (ValueError, KeyError) as exc:
            raise HTTPException(422, "Invalid artifact source") from exc
        return {**service.metadata(artifact), "selected_version": version.number}


@router.post("/{artifact_id}/restore")
async def restore(artifact_id: uuid.UUID, body: RestoreArtifact):
    async with async_session_factory() as db:
        artifact = await service.get_artifact(db, artifact_id)
        old = await service.get_version(db, artifact, body.version)
        await service.publish(
            db, artifact, body.expected_version, old.source, restored_from=old.number
        )
        await db.commit()
        return service.metadata(artifact)


@router.post("/{artifact_id}/export/{format}")
async def export(artifact_id: uuid.UUID, format: str, version: int | None = Query(None, ge=1)):
    async with async_session_factory() as db:
        artifact = await service.get_artifact(db, artifact_id)
        current = await service.get_version(db, artifact, version)
        await service.export(db, artifact, current, format)
        await db.commit()
        return {
            "download_url": f"/api/artifacts/{artifact.id}/download/{format}?version={current.number}"
        }


@router.get("/{artifact_id}/download/{format}")
async def download(artifact_id: uuid.UUID, format: str, version: int | None = Query(None, ge=1)):
    async with async_session_factory() as db:
        artifact = await service.get_artifact(db, artifact_id)
        current = await service.get_version(db, artifact, version)
        relative = current.outputs.get(format)
        if not relative:
            raise HTTPException(404, "Export is not available")
        path = service.file_path(relative)
        if not path.is_file():
            raise HTTPException(404, "Artifact output not found")
        await db.commit()
        filename = artifact.title.rsplit(".", 1)[0] + path.suffix
        return FileResponse(path, filename=filename)
