"""Import instruction-only SKILL.md folders and freeze their original text."""

import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, model_validator

from aer_core.config.models import NonBlank, Settings
from aer_core.schemas.skills import SkillDocument, SkillLibrary


class SkillsConfig(Settings):
    initial_library: NonBlank | None = None
    mode: Literal["on_demand", "preload"] = "on_demand"
    preload: list[NonBlank] = Field(default_factory=list)

    @model_validator(mode="after")
    def selection(self):
        if len(set(self.preload)) != len(self.preload):
            raise ValueError("preload skill names must be unique")
        if self.mode == "on_demand" and self.preload:
            raise ValueError("preload names require mode=preload")
        return self

    def controlled(self):
        return self.model_dump(mode="json", exclude={"initial_library"})


class _FrontmatterLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise ValueError("skill frontmatter requires unique string keys")
        result[key] = loader.construct_object(value_node, deep=True)
    return result


_FrontmatterLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def parse_skill(content):
    lines = content.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("SKILL.md must start with YAML frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise ValueError("SKILL.md frontmatter must have a closing delimiter") from error
    metadata = yaml.load("\n".join(lines[1:end]), Loader=_FrontmatterLoader)
    if not isinstance(metadata, dict) or not "\n".join(lines[end + 1:]).strip():
        raise ValueError("SKILL.md requires metadata and a nonempty instruction body")
    return SkillDocument(name=metadata.get("name"), description=metadata.get("description"), content=content)


def encode_library(library):
    data = library.model_dump(mode="json")
    data["skills"].sort(key=lambda skill: skill["name"])
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def decode_library(content):
    library = SkillLibrary.model_validate_json(content)
    for skill in library.skills:
        if parse_skill(skill.content) != skill:
            raise ValueError("skill metadata differs from its frozen SKILL.md")
    return library


def import_library(path):
    if path is None:
        return SkillLibrary()
    root = Path(path)
    if not root.is_absolute():
        raise ValueError("initial_library must be resolved relative to the experiment YAML before import")
    if not root.is_dir():
        raise ValueError("initial_library must be a directory")
    skills = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        if directory.is_symlink():
            raise ValueError("skill folders must not be symbolic links")
        entry = directory / "SKILL.md"
        if not entry.is_file() or entry.is_symlink():
            raise ValueError(f"skill folder {directory.name} requires a regular SKILL.md")
        if any(item != entry for item in directory.iterdir()):
            raise ValueError(f"skill {directory.name}: v1 supports SKILL.md only; attachments are not supported")
        if entry.stat().st_size > 262144:
            raise ValueError("SKILL.md exceeds the import size limit")
        skills.append(parse_skill(entry.read_bytes().decode("utf-8-sig")))
    return SkillLibrary(skills=skills)


def initial_skill_artifacts(settings):
    if settings is None:
        return {}
    library = import_library(settings.initial_library)
    if not set(settings.preload) <= {skill.name for skill in library.skills}:
        raise ValueError("preload selection contains an unknown skill")
    return {"skill": encode_library(library)}
