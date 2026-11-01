"""A candidate-local native ToolManager backed by a reproducible description index."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import re
from types import SimpleNamespace


class DescriptionIndex:
    """Dependency-injected lexical index. Native baseline retains upstream Chroma.

    The bundle, not this derived index, is the authoritative repository state.
    """
    def __init__(self):
        self.entries = {}
        self._collection = self

    def count(self):
        return len(self.entries)

    def delete(self, ids):
        for name in ids:
            self.entries.pop(name, None)

    def add_texts(self, texts, ids, metadatas):
        for name, text, metadata in zip(ids, texts, metadatas):
            self.entries[name] = (text, metadata)

    def similarity_search_with_score(self, query, k):
        words = set(re.findall(r"\w+", query.lower()))
        scored = []
        for name, (text, metadata) in self.entries.items():
            tokens = set(re.findall(r"\w+", text.lower()))
            score = 1 - len(words & tokens) / max(1, len(words | tokens))
            scored.append((score, name, metadata))
        return [(SimpleNamespace(metadata=metadata), score) for score, _, metadata in sorted(scored)[:k]]

    def persist(self):
        pass


class ConstructionRepository:
    def __init__(self, upstream, directory, tools):
        root = Path(directory)
        for name in ("tool_code", "tool_description"):
            (root / name).mkdir(parents=True, exist_ok=True)
        self.native = upstream.manager.__new__(upstream.manager)
        self.native.generated_tools = {}
        self.native.generated_tool_repo_dir = str(root)
        self.native.vectordb = DescriptionIndex()
        self.retriever = upstream.retriever.__new__(upstream.retriever)
        self.retriever.tool_manager = self.native
        for name, tool in tools.items():
            self.store(name, tool)
        if not tools:
            (root / "generated_tools.json").write_text("{}", encoding="utf-8")

    def store(self, name, tool):
        self.store_source(name, tool.source, tool.description)

    def store_source(self, name, source, description):
        # add_new_tool deliberately supports replacement, unlike upstream store_tool's existence guard.
        with redirect_stdout(io.StringIO()):
            self.native.add_new_tool({"task_name": name, "code": source, "description": description})

    def relevant(self, description, limit=3):
        with redirect_stdout(io.StringIO()):
            names = self.retriever.retrieve_tool_name(description, limit)
        return self.retriever.retrieve_tool_code_pair(names)

    def descriptions(self, task):
        with redirect_stdout(io.StringIO()):
            names = self.retriever.retrieve_tool_name(task)
        return self.retriever.retrieve_tool_description_pair(names)
