"""Portable skill content and execution-bound delivery capabilities."""

from typing import Literal

from pydantic import Field, model_validator

from .base import DomainModel, Identifier, Items


class SkillDocument(DomainModel):
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    description: Identifier = Field(max_length=1024)
    content: Identifier = Field(max_length=65536)


class SkillLibrary(DomainModel):
    schema_version: Literal["aer.skill_library.v1"] = "aer.skill_library.v1"
    skills: Items[SkillDocument] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def unique_names(self):
        if len({skill.name for skill in self.skills}) != len(self.skills):
            raise ValueError("skill names must be unique")
        return self


class SkillDeliveryCapabilities(DomainModel):
    transport: Literal["tool", "mcp", "preload"]
    discovery: Literal["catalog", "explicit"]
    observes_reads: bool
