"""Small model-facing app specification; never accepts source code or file paths."""

import keyword
import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.agent.coder.starters import BACKENDS, FRONTENDS
from app.agent.coder.styling import DEFAULT_STYLING, validate_styling

IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,47}\Z")
RESERVED = {"id", "health", "docs", "openapi", "sqlite_master", "sqlite_sequence"}


def identifier(value):
    if (
        not IDENTIFIER.fullmatch(value)
        or keyword.iskeyword(value)
        or value in RESERVED
        or value.startswith(("sqlite_", "model_"))
    ):
        raise ValueError(
            f"Use a non-reserved lowercase snake_case identifier: {value!r}"
        )
    return value


class EntityField(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str
    type: Literal["string", "integer", "number", "boolean"] = "string"
    label: str | None = Field(default=None, min_length=1, max_length=100)
    required: bool = True
    default: str | int | float | bool | None = None

    @model_validator(mode="after")
    def validate_field(self):
        identifier(self.name)
        if self.default is not None:
            allowed = {
                "string": (str,),
                "integer": (int,),
                "number": (int, float),
                "boolean": (bool,),
            }[self.type]
            if type(self.default) not in allowed:
                raise ValueError(f"Default for {self.name} must match {self.type}")
            if isinstance(self.default, float) and not math.isfinite(self.default):
                raise ValueError("Defaults must be finite")
            if self.type == "integer" and abs(self.default) > 9007199254740991:
                raise ValueError(
                    "Integer defaults must fit JavaScript safe integer precision"
                )
            if isinstance(self.default, str) and (
                len(self.default) > 1000 or self.required and not self.default.strip()
            ):
                raise ValueError(
                    "String defaults must fit the field and not be blank when required"
                )
            if isinstance(self.default, str):
                self.default = self.default.strip()
        return self


class Entity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str
    label: str | None = Field(default=None, min_length=1, max_length=100)
    fields: list[EntityField] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def validate_entity(self):
        identifier(self.name)
        if len({f.name for f in self.fields}) != len(self.fields):
            raise ValueError("Field names must be unique within an entity")
        return self


class AppSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=100)
    backend: str = "fastapi"
    frontend: str = "react-vite"
    styling: str = DEFAULT_STYLING
    features: list[str] = Field(
        default_factory=lambda: ["crud"], min_length=1, max_length=8
    )
    entities: list[Entity] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_app(self):
        validate_styling(self.styling)
        if not self.name.strip():
            raise ValueError("App name cannot be blank")
        if self.backend not in {"none", *BACKENDS} or self.frontend not in {
            "none",
            *FRONTENDS,
        }:
            raise ValueError("Choose a registered backend/frontend stack")
        if self.backend == self.frontend == "none":
            raise ValueError("Select a backend, frontend, or both")
        if len({e.name for e in self.entities}) != len(self.entities):
            raise ValueError("Entity names must be unique")
        if "crud" not in self.features or len(set(self.features)) != len(self.features):
            raise ValueError("Select CRUD and do not repeat features")
        return self
