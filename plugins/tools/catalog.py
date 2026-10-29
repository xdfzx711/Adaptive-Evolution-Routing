"""Validated tool implementations; private connection settings stay outside feedback."""

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from aer_core.config.models import NonBlank, PositiveFloat, Settings
from plugins.tool_selection import ToolDescriptor
from plugins.tools.local import local_tool_registry


def tool_name(value):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", value):
        raise ValueError("Tool identifiers must be 1-64 letters, digits, underscores or hyphens, starting with a letter")
    return value


class CatalogEntry(Settings):
    kind: Literal["local", "mcp"]
    plugin: NonBlank | None = None
    server: NonBlank | None = None
    tool_name: NonBlank | None = None
    description: NonBlank | None = None
    version: NonBlank | None = None
    input_schema: dict | None = None

    @model_validator(mode="after")
    def valid_entry(self):
        if self.kind == "local":
            if self.plugin is None or self.server is not None or self.tool_name is not None:
                raise ValueError("Local tools require plugin and no MCP server/tool_name")
            descriptor = local_tool_registry().get(self.plugin).descriptor
            if self.version is not None and self.version != descriptor.version:
                raise ValueError("Local tool version differs from its registered implementation")
            if self.input_schema is not None and self.input_schema != descriptor.model_dump(mode="json")["input_schema"]:
                raise ValueError("Local tool input_schema differs from its registered implementation")
        elif (self.server is None or self.tool_name is None or self.plugin is not None
              or self.description is None or self.version is None):
            raise ValueError("MCP tools require server, tool_name, description and version, without local plugin")
        return self

    def public_descriptor(self):
        if self.kind == "local":
            registered = local_tool_registry().get(self.plugin).descriptor
            return registered.replace(description=self.description or registered.description)
        return ToolDescriptor(description=self.description, version=self.version, input_schema=self.input_schema)


class MCPServerConfig(Settings):
    transport: Literal["stdio", "streamable_http"]
    version: NonBlank
    command: list[NonBlank] | None = None
    url: NonBlank | None = None
    env_vars: list[NonBlank] = Field(default_factory=list)
    bearer_token_env: NonBlank | None = None
    header_envs: dict[NonBlank, NonBlank] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_connection(self):
        if len(self.env_vars) != len(set(self.env_vars)):
            raise ValueError("env_vars must be unique")
        if self.transport == "stdio":
            if not self.command or self.url is not None or self.bearer_token_env or self.header_envs:
                raise ValueError("STDIO requires command and uses env_vars for credentials")
        else:
            if self.command is not None or self.env_vars or not self.url:
                raise ValueError("Streamable HTTP requires url and no command/env_vars")
            parts = urlsplit(self.url)
            if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
                raise ValueError("MCP URL must be HTTP(S), without credentials, query or fragment")
        return self


class ToolingConfig(Settings):
    catalog: dict[NonBlank, CatalogEntry]
    servers: dict[NonBlank, MCPServerConfig] = Field(default_factory=dict)
    dotenv_path: NonBlank | None = None
    startup_timeout_seconds: PositiveFloat = 30.0
    tool_timeout_seconds: PositiveFloat = 30.0

    @model_validator(mode="after")
    def valid_catalog(self):
        for name, entry in self.catalog.items():
            tool_name(name)
            if entry.kind == "mcp":
                if entry.server not in self.servers:
                    raise ValueError("Tool references an unconfigured MCP server: " + entry.server)
                if entry.version != self.servers[entry.server].version:
                    raise ValueError("MCP tool and server versions must match")
        return self

    def public_catalog(self):
        return {name: entry.public_descriptor().model_dump(mode="json") for name, entry in self.catalog.items()}
