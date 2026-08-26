from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from contextforge.compatibility import (
    JSON_OUTPUT_SCHEMA_VERSION,
    PLUGIN_ENTRY_POINT_GROUP,
    PLUGIN_PROTOCOL_VERSION,
    SUPPORTED_PYTHON_VERSIONS,
)
from contextforge.plugins import AnalyzerCapability, FrameworkCapability, Plugin


ROOT = Path(__file__).parents[1]
COMPATIBILITY_MANIFEST = ROOT / "docs" / "compatibility-1.0.json"
JSON_SCHEMA = ROOT / "docs" / "json-schema-1.0.json"


def test_stable_compatibility_manifest_matches_runtime_contracts():
    manifest = json.loads(COMPATIBILITY_MANIFEST.read_text())

    assert manifest == {
        "compatibility_version": "1.0",
        "json_output": {
            "schema_version": JSON_OUTPUT_SCHEMA_VERSION,
            "schema": "json-schema-1.0.json",
        },
        "plugins": {
            "entry_point_group": PLUGIN_ENTRY_POINT_GROUP,
            "protocol_version": PLUGIN_PROTOCOL_VERSION,
        },
        "python": {
            "supported_versions": list(SUPPORTED_PYTHON_VERSIONS),
        },
    }


def test_json_schema_preserves_the_stable_envelope_and_forward_compatibility():
    schema = json.loads(JSON_SCHEMA.read_text())
    validator = Draft202012Validator(schema)
    payload = {
        "schema_version": JSON_OUTPUT_SCHEMA_VERSION,
        "command": "future-command",
        "repository": "/repository",
        "future_additive_field": {"accepted": True},
    }

    validator.validate(payload)
    for required in ("schema_version", "command", "repository"):
        malformed = dict(payload)
        malformed.pop(required)
        assert list(validator.iter_errors(malformed))


def test_plugin_protocol_dataclasses_remain_frozen_and_default_compatible():
    plugin = Plugin(name="example", version="1.2.3", protocol_version=PLUGIN_PROTOCOL_VERSION)

    assert plugin.priority == 100
    assert plugin.analyzers == ()
    assert plugin.framework_analyzers == ()
    with pytest.raises(FrozenInstanceError):
        setattr(plugin, "priority", 1)


def test_public_plugin_capabilities_have_stable_field_order():
    assert tuple(AnalyzerCapability.__dataclass_fields__) == ("analyzer", "extensions")
    assert tuple(FrameworkCapability.__dataclass_fields__) == ("analyzer",)
    assert tuple(Plugin.__dataclass_fields__) == (
        "name",
        "version",
        "protocol_version",
        "analyzers",
        "framework_analyzers",
        "priority",
    )
