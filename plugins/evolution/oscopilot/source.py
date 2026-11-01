"""Load pinned upstream modules with explicit dependency substitutions.

The complete upstream checkout is unmodified and available to the native baseline.
Only import lines are substituted here; upstream planning, retrieval, generation,
judgment, repair and ToolManager methods execute directly from their source files. Constructors with
global configuration are replaced by dependency injection in engine/repository.
"""

from functools import lru_cache
import builtins
from pathlib import Path
import platform
import subprocess
from types import SimpleNamespace


UPSTREAM_REVISION = "f720af8807e49a92dda64572d2c6bc6c0ac7ee7e"
DEFAULT_SOURCE = Path(__file__).resolve().parents[3] / "third_party" / "OS-Copilot"


def no_dotenv(*args, **kwargs):
    return False


def utf8_open(file, mode="r", *args, **kwargs):
    if "b" not in mode:
        kwargs.setdefault("encoding", "utf-8")
    return builtins.open(file, mode, *args, **kwargs)


def no_automatic_retry(**kwargs):
    # Retry decisions belong to the bounded construction loop, with usage receipts.
    return lambda function: function


def send_chat_prompts(system, user, llm, prefix=""):
    return llm.chat([{"role": "system", "content": system}, {"role": "user", "content": user}], prefix=prefix)


def load_module(root, relative, substitutions, injected):
    path = root / relative
    source = path.read_text(encoding="utf-8")
    for line, replacement in substitutions.items():
        if line not in source:
            raise ValueError("Unsupported OS-Copilot source import: " + relative + ": " + line)
        source = source.replace(line, replacement)
    namespace = {"__name__": "aer_oscopilot_upstream", "__file__": str(path), **injected}
    exec(compile(source, str(path), "exec"), namespace)
    return namespace


@lru_cache(maxsize=4)
def load_upstream(source_path=None):
    root = Path(source_path or DEFAULT_SOURCE).resolve()
    if not (root / "oscopilot").is_dir():
        raise ValueError("OS-Copilot checkout is missing; run git submodule update --init third_party/OS-Copilot")
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                            text=True, timeout=10, check=True)
    if result.stdout.strip() != UPSTREAM_REVISION:
        raise ValueError("OS-Copilot must be checked out at " + UPSTREAM_REVISION)
    dirty = subprocess.run(["git", "-C", str(root), "diff", "--quiet", "HEAD", "--", "oscopilot"],
                           capture_output=True, timeout=10)
    if dirty.returncode != 0:
        raise ValueError("OS-Copilot source has local modifications; keep AER adaptations outside the submodule")
    base = load_module(root, "oscopilot/modules/base_module.py", {
        "from oscopilot.utils.llms import OpenAI, OLLAMA": "",
        "from oscopilot.environments import Env": "",
        "from oscopilot.utils import get_os_version": "",
        "from dotenv import load_dotenv": "",
    }, {"load_dotenv": no_dotenv, "get_os_version": platform.platform})
    executor = load_module(root, "oscopilot/modules/executor/friday_executor.py", {
        "from oscopilot.modules.base_module import BaseModule": "",
        "from oscopilot.tool_repository.manager.tool_manager import get_open_api_doc_path": "",
        "from oscopilot.utils.utils import send_chat_prompts, api_exception_mechanism": "",
    }, {"BaseModule": base["BaseModule"], "send_chat_prompts": send_chat_prompts,
        "api_exception_mechanism": no_automatic_retry})
    action = load_module(root, "oscopilot/tool_repository/manager/action_node.py", {}, {})
    planner = load_module(root, "oscopilot/modules/planner/friday_planner.py", {
        "from oscopilot.tool_repository.manager.action_node import ActionNode": "",
        "from oscopilot.modules.base_module import BaseModule": "",
        "from oscopilot.tool_repository.manager.tool_manager import get_open_api_description_pair": "",
        "from oscopilot.utils.utils import send_chat_prompts, api_exception_mechanism": "",
    }, {"ActionNode": action["ActionNode"], "BaseModule": base["BaseModule"],
        "send_chat_prompts": send_chat_prompts, "api_exception_mechanism": no_automatic_retry,
        # Bundled upstream API servers are not running in AER's construction workspace.
        "get_open_api_description_pair": lambda: {}})
    retriever = load_module(root, "oscopilot/modules/retriever/vector_retriever.py", {
        "from oscopilot.modules.base_module import BaseModule": "",
        "from oscopilot.utils.utils import send_chat_prompts": "",
    }, {"BaseModule": base["BaseModule"], "send_chat_prompts": send_chat_prompts})
    manager = load_module(root, "oscopilot/tool_repository/manager/tool_manager.py", {
        "from langchain.vectorstores import Chroma": "",
        "from langchain.embeddings.openai import OpenAIEmbeddings": "",
        "from langchain_community.embeddings import OllamaEmbeddings": "",
        "from dotenv import load_dotenv": "",
    }, {"load_dotenv": no_dotenv, "open": utf8_open})
    prompts = load_module(root, "oscopilot/prompts/friday_pt.py", {}, {})["prompt"]
    return SimpleNamespace(executor=executor["FridayExecutor"], planner=planner["FridayPlanner"],
                           retriever=retriever["FridayRetriever"], manager=manager["ToolManager"],
                           prompts=prompts, revision=UPSTREAM_REVISION)
