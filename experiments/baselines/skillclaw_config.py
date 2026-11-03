"""Explicit experiment controls for the independent native SkillClaw baseline."""

from pathlib import Path
import sys

from pydantic import BaseModel, ConfigDict, Field
import yaml


class Endpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key_env: str
    base_url_env: str
    model_env: str | None = None


class BaselineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    aer_config: str
    initial_library: str
    source: str = "../../third_party/SkillClaw"
    native_python: str = sys.executable
    dotenv_path: str | None = "../../.env"
    mode: str = Field(default="learn", pattern="^(learn|frozen)$")
    evolve_every_tasks: int = Field(default=2, ge=1)
    cycle_timeout_seconds: float = Field(default=900, gt=0)
    execution_api: Endpoint = Field(default_factory=lambda: Endpoint(
        api_key_env="CODEX_API_KEY", base_url_env="CODEX_BASE_URL"))
    evolution_api: Endpoint = Field(default_factory=lambda: Endpoint(
        api_key_env="QWEN_API_KEY", base_url_env="QWEN_BASE_URL", model_env="QWEN_MODEL"))
    llm_max_tokens: int = Field(default=8192, ge=1)
    llm_temperature: float = Field(default=0.4, ge=0)
    use_session_judge: bool = True
    use_skill_verifier: bool = False


def load_config(path):
    path = Path(path).resolve()
    config = BaselineConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    values = config.model_dump()
    for name in ("aer_config", "initial_library", "source", "native_python", "dotenv_path"):
        if values[name] is not None:
            values[name] = str((path.parent / values[name]).resolve())
    return BaselineConfig.model_validate(values)
