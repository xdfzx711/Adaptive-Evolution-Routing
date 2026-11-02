"""Skill access is bound by the host to one execution and Snapshot."""

from typing import Protocol


class SkillAccess(Protocol):
    def list_skills(self) -> list[dict[str, str]]: ...

    def read_skill(self, name: str, *, call_id: str) -> dict: ...
