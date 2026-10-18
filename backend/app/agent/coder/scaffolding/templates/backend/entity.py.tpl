"""Editable schemas and endpoints for this resource."""

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from backend.repository import Repository

NAME = ENTITY_NAME
FIELDS = ENTITY_FIELDS


class Create(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    # CREATE_FIELDS


class Patch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    # PATCH_FIELDS


class Record(Create):
    id: int


class Page(BaseModel):
    items: list[Record]
    total: int
    limit: int
    offset: int


def build_router(services):
    repository = Repository(NAME, FIELDS)
    services.initializers.append(repository.initialize)
    router = APIRouter(
        prefix="/api/" + NAME, tags=[NAME], dependencies=services.route_dependencies
    )

    @router.get("", response_model=Page)
    async def list_records(
        search: str = Query("", max_length=200),
        sort: str = "id",
        descending: bool = False,
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        if sort not in repository.columns:
            raise HTTPException(422, "Unknown sort field")
        return await run_in_threadpool(
            repository.list, search, sort, descending, limit, offset
        )

    @router.get("/{record_id}", response_model=Record)
    async def get_record(record_id: int):
        record = await run_in_threadpool(repository.get, record_id)
        if record is None:
            raise HTTPException(404, "Record not found")
        return record

    @router.post("", response_model=Record, status_code=201)
    async def create_record(data: Create):
        record = await run_in_threadpool(repository.create, data.model_dump())
        await services.changed(NAME, "created", record["id"])
        return record

    @router.patch("/{record_id}", response_model=Record)
    async def update_record(record_id: int, data: Patch):
        changes = data.model_dump(exclude_unset=True)
        record = await run_in_threadpool(repository.update, record_id, changes)
        if record is None:
            raise HTTPException(404, "Record not found")
        if changes:
            await services.changed(NAME, "updated", record_id)
        return record

    @router.delete("/{record_id}", status_code=204)
    async def delete_record(record_id: int):
        if not await run_in_threadpool(repository.delete, record_id):
            raise HTTPException(404, "Record not found")
        await services.changed(NAME, "deleted", record_id)
        return Response(status_code=204)

    return router
