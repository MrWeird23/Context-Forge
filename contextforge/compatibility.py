"""Stable public compatibility constants for ContextForge 1.x."""

from __future__ import annotations

JSON_OUTPUT_SCHEMA_VERSION = "1.0"
PLUGIN_ENTRY_POINT_GROUP = "contextforge.plugins"
PLUGIN_PROTOCOL_VERSION = "1.0"
MCP_TOOL_PROTOCOL_VERSION = "1.0"
SUPPORTED_PYTHON_VERSIONS = ("3.10", "3.11", "3.12", "3.13")

__all__ = (
    "JSON_OUTPUT_SCHEMA_VERSION",
    "PLUGIN_ENTRY_POINT_GROUP",
    "PLUGIN_PROTOCOL_VERSION",
    "MCP_TOOL_PROTOCOL_VERSION",
    "SUPPORTED_PYTHON_VERSIONS",
)
