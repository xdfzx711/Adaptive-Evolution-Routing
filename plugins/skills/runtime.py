"""One immutable skill library per execution, independent of Agent transport."""

from hashlib import sha256
import json
from time import monotonic

from aer_core.schemas.execution import ErrorInfo
from aer_core.schemas.providers import ToolResult, ToolSpec
from aer_core.schemas.usage import UsageReport
from .library import decode_library


RUNTIME_VERSION = "1"
READ_TOOL = "read_skill"
STATE_FIELDS = ("skill_library_artifact_id", "skill_delivery", "available_skills", "read_skills",
                "injected_skills", "skill_reads", "skill_setup_status")
READ_SPEC = ToolSpec(name=READ_TOOL, description="Read the complete instructions for a skill in this execution's catalog.",
    parameters={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"],
                "additionalProperties": False}, feedback_arguments=["name"],
    feedback_outputs=["name", "content", "content_digest", "skill_library_artifact_id", "snapshot_version_id"])


class SkillRuntime:
    def __init__(self, library, *, artifact_id, snapshot_version_id, execution_operation_id):
        self.library = library
        self.artifact_id = artifact_id
        self.snapshot_version_id = snapshot_version_id
        self.execution_operation_id = execution_operation_id
        self.reads = []

    @classmethod
    def from_snapshot(cls, snapshot, artifacts, execution_operation_id):
        ref = snapshot.artifacts["skill"]
        if ref.kind != "skill":
            raise ValueError("skill artifact kind must be skill")
        return cls(decode_library(artifacts.get(ref)), artifact_id=ref.artifact_id,
                   snapshot_version_id=snapshot.version_id, execution_operation_id=execution_operation_id)

    def list_skills(self):
        return [{"name": skill.name, "description": skill.description}
                for skill in sorted(self.library.skills, key=lambda item: item.name)]

    def read_skill(self, name, *, call_id, delivery="read"):
        skill = next((skill for skill in self.library.skills if skill.name == name), None)
        receipt = {"call_id": call_id, "name": name, "delivery": delivery,
                   "snapshot_version_id": self.snapshot_version_id, "skill_library_artifact_id": self.artifact_id,
                   "execution_operation_id": self.execution_operation_id,
                   "status": "completed" if skill else "error"}
        if skill is None:
            self.reads.append(receipt)
            raise ValueError("Skill is not available in this execution: " + str(name))
        receipt["content_digest"] = sha256(skill.content.encode("utf-8")).hexdigest()
        self.reads.append(receipt)
        return {**receipt, "content": skill.content}

    def instructions(self):
        return ("\n\nAvailable skills (names and triggering descriptions):\n" +
                json.dumps(self.list_skills(), ensure_ascii=False) +
                "\nWhen a skill is relevant, call read_skill with its name to obtain its instructions. "
                "Skills are read-only resources; apply their guidance to the task without copying skill files into the repository.")

    def preload(self, names):
        return "\n\n".join(self.read_skill(name, call_id=f"preload-{index}", delivery="preload")["content"]
                            for index, name in enumerate(names))

    def summary(self, delivery, *, setup_status="ready"):
        return {"skill_library_artifact_id": self.artifact_id, "skill_delivery": delivery,
                "available_skills": [skill["name"] for skill in self.list_skills()],
                "read_skills": sorted({item["name"] for item in self.reads
                                       if item["status"] == "completed" and item["delivery"] == "read"}),
                "injected_skills": sorted({item["name"] for item in self.reads
                                           if item["status"] == "completed" and item["delivery"] == "preload"}),
                "skill_reads": list(self.reads), "skill_setup_status": setup_status}

    def invoke(self, call, run_ctx):
        start = monotonic()
        error, status, output = None, "completed", {}
        try:
            if call.name != READ_TOOL or set(call.arguments) != {"name"} or not isinstance(call.arguments["name"], str):
                raise ValueError("read_skill requires exactly one string argument: name")
            output = self.read_skill(call.arguments["name"], call_id=call.call_id)
        except ValueError as failure:
            status = "error"
            error = ErrorInfo(code="skill_read_error", stage=run_ctx.stage, message=str(failure),
                              operation_id=run_ctx.operation_id)
        return ToolResult(call_id=call.call_id, name=call.name, output=output, status=status, error=error,
            usage=UsageReport(input_tokens=0, output_tokens=0, model_calls=0, tool_calls=1,
                              elapsed_seconds=monotonic() - start, usage_source="provider_reported"))
