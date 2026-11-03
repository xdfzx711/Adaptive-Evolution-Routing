"""Load the pinned upstream prompt constants without importing its runners or SDKs."""

import ast
from pathlib import Path
import subprocess


UPSTREAM_REVISION = "ed80611788292ea739f1effd31f16c53823b8a0d"
DEFAULT_SOURCE = Path(__file__).resolve().parents[3] / "third_party/reasoning-bank"
PROMPTS = "third_party/src/minisweagent/memory/instruction.py"


def load_prompts(source=None):
    root = Path(source or DEFAULT_SOURCE).resolve()
    if not (root / PROMPTS).is_file():
        raise ValueError("ReasoningBank source missing; run git submodule update --init third_party/reasoning-bank")
    revision = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True,
        capture_output=True, text=True, timeout=15).stdout.strip()
    if revision != UPSTREAM_REVISION:
        raise ValueError("ReasoningBank must be pinned to " + UPSTREAM_REVISION)
    original = subprocess.run(["git", "-C", str(root), "show", f"{UPSTREAM_REVISION}:{PROMPTS}"],
        check=True, capture_output=True, text=True, encoding="utf-8", timeout=15).stdout
    current = (root / PROMPTS).read_text(encoding="utf-8")
    if current != original:
        raise ValueError("ReasoningBank prompt source differs from the pinned version")
    constants = {}
    for node in ast.parse(current).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in {"SUCCESSFUL_SI", "FAILED_SI"}:
                constants[node.targets[0].id] = ast.literal_eval(node.value)
    return constants["SUCCESSFUL_SI"], constants["FAILED_SI"]
