"""Qwen JSON mode for plugin metadata only; shared providers remain unchanged."""

from copy import copy

from plugins.providers.qwen import QwenModel


def metadata_provider(provider, mode):
    # JSON object mode supports arbitrary JSON Schemas carried as values in the
    # metadata. The full metadata schema and Python signature are checked locally.
    # Avoid imposing a provider's restricted strict-schema dialect on tool schemas.
    if not isinstance(provider, QwenModel):
        return provider
    scoped = copy(provider)
    scoped.config = provider.config.model_copy(update={"response_format": "json_object" if mode == "auto" else None})
    return scoped
