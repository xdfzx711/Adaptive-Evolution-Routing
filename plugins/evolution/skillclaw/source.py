"""Import unmodified upstream pipeline modules under a private namespace."""

from hashlib import sha256
import importlib
from pathlib import Path
import subprocess
import sys
from threading import RLock
from types import ModuleType, SimpleNamespace


UPSTREAM_REVISION = "334ddf82856826b7a259470c6670fb0ef5ce2dc4"
DEFAULT_SOURCE = Path(__file__).resolve().parents[3] / "third_party/SkillClaw"
_LOCK = RLock()


def verify_source(source=None):
    root = Path(source or DEFAULT_SOURCE).resolve()
    if not (root / "evolve_server/pipeline/execution.py").is_file():
        raise ValueError("SkillClaw source missing; run git submodule update --init third_party/SkillClaw")
    revision = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True, timeout=15).stdout.strip()
    if revision != UPSTREAM_REVISION:
        raise ValueError("SkillClaw must be pinned to " + UPSTREAM_REVISION)
    clean = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all", "--",
                            "evolve_server", "skillclaw"], capture_output=True, text=True, check=True, timeout=15)
    if clean.stdout.strip():
        raise ValueError("SkillClaw upstream source must remain unmodified")
    return root


def load_upstream(source=None):
    root = verify_source(source)
    namespace = "_aer_skillclaw_" + sha256(str(root).encode()).hexdigest()[:16]
    with _LOCK:
        # Upstream's top-level __init__ eagerly imports unrelated Agent/server
        # engines. Namespace packages load the original workflow modules without
        # starting those integrations or adding global sys.path aliases.
        for suffix, directory in (("", root), (".evolve_server", root / "evolve_server")):
            name = namespace + suffix
            if name not in sys.modules:
                package = ModuleType(name)
                package.__path__ = [str(directory)]
                package.__package__ = name
                sys.modules[name] = package
        def module(path):
            return importlib.import_module(namespace + ".evolve_server." + path)
        return SimpleNamespace(summarizer=module("pipeline.summarizer"),
            aggregation=module("pipeline.aggregation"), execution=module("pipeline.execution"),
            verifier=module("pipeline.skill_verifier"), utils=module("core.utils"))
