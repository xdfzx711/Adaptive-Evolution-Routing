"""Apply native actions to an immutable library without changing unrelated bytes."""

import yaml

from aer_core.schemas.skills import SkillLibrary
from plugins.skills.library import parse_skill


def apply_proposal(library, proposal, *, source_name=None, max_changes=4, changed=()):
    if not isinstance(proposal, dict):
        raise ValueError("SkillClaw did not return a valid proposal")
    action = proposal.get("action")
    if action == "skip":
        return library, None
    if action not in {"improve_skill", "optimize_description", "create_skill"}:
        raise ValueError("Unsupported SkillClaw action: " + str(action))
    data = proposal.get("skill")
    if not isinstance(data, dict):
        raise ValueError("SkillClaw action requires a skill object")
    if any(key in data for key in ("files", "scripts", "resources", "attachments")):
        raise ValueError("Only instruction-only SKILL.md skills are supported")
    current = {skill.name: skill for skill in library.skills}
    if action != "create_skill":
        if source_name not in current or data.get("name", source_name) != source_name:
            raise ValueError("Skill update must preserve the existing name")
        name = source_name
        original = current[name]
        lines = original.content.splitlines(keepends=True)
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
        metadata = yaml.safe_load("".join(lines[1:end]))
        old_body = "".join(lines[end + 1:])
        description = data.get("description", original.description)
        body = old_body if action == "optimize_description" else data.get("content")
        if not isinstance(body, str) or not body.strip():
            raise ValueError("Skill improvement requires a nonempty complete body")
        if action == "optimize_description" and "content" in data and data["content"].strip() != old_body.strip():
            raise ValueError("Description-only action attempted to change the body")
        metadata["description"] = description
        if description == original.description and body.strip() == old_body.strip():
            return library, None
    else:
        name, body = data.get("name"), data.get("content")
        if not isinstance(name, str) or name in current:
            raise ValueError("New skill requires a unique name; conflicts are not overwritten")
        if not isinstance(body, str) or not body.strip():
            raise ValueError("New skill requires a nonempty body")
        metadata = {"name": name, "description": data.get("description")}
    if body.lstrip().startswith("---"):
        raise ValueError("SkillClaw content must be a body, not a nested SKILL.md document")
    if name in changed or len(changed) >= max_changes:
        raise ValueError("Skill change conflict or per-round change limit exceeded")
    document = parse_skill("---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False) + "---\n" + body)
    current[name] = document
    return SkillLibrary(skills=sorted(current.values(), key=lambda skill: skill.name)), {
        "action": action, "name": name, "rationale": str(proposal.get("rationale", ""))}
