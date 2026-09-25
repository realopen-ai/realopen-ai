"""Brain Skills management API."""

import zipfile

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.services import skills

router = APIRouter()


class SkillWrite(BaseModel):
    name: str
    description: str
    roles: list[str] = Field(default_factory=list)
    enabled: bool = True
    content: str


@router.get("/skills")
async def list_all_skills():
    return skills.list_skills()


@router.get("/skills/{skill_id}")
async def read_skill(skill_id: str):
    try:
        return skills.get_skill(skill_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get(
    "/skills/{skill_id}/resources/{resource_path:path}",
    response_class=PlainTextResponse,
)
async def read_skill_resource(skill_id: str, resource_path: str):
    try:
        return skills.read_resource(skill_id, resource_path)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Skill resource not found") from exc
    except ValueError as exc:
        status = 415 if "not a text file" in str(exc) else 400
        raise HTTPException(status, str(exc)) from exc


@router.post("/skills")
async def create_skill(body: SkillWrite):
    try:
        skill_id = skills.slugify(body.name)
        try:
            skills.get_skill(skill_id)
        except FileNotFoundError:
            pass
        else:
            raise HTTPException(409, f"Skill '{skill_id}' already exists")
        return skills.save_skill(**body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.put("/skills/{skill_id}")
async def update_skill(skill_id: str, body: SkillWrite):
    try:
        skills.get_skill(skill_id)
        return skills.save_skill(skill_id=skill_id, **body.model_dump())
    except FileNotFoundError as exc:
        raise HTTPException(404, "Skill not found") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/skills/{skill_id}", status_code=204)
async def remove_skill(skill_id: str):
    try:
        skills.delete_skill(skill_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Skill not found") from exc


@router.post("/skills/import")
async def import_skill(
    archive: UploadFile | None = File(None),
    files: list[UploadFile] | None = File(None),
):
    try:
        if archive:
            return skills.import_zip(await archive.read(skills.MAX_IMPORT_BYTES + 1))
        if files:
            entries = [
                (item.filename or "", await item.read(skills.MAX_IMPORT_BYTES + 1))
                for item in files
            ]
            return skills.import_files(entries)
        raise ValueError("Choose a skill ZIP or folder")
    except (ValueError, zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise HTTPException(400, str(exc)) from exc
