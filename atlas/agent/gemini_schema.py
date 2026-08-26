"""Gemini Developer API compatible structured-output schemas.

Pydantic JSON Schema emits ``additionalProperties`` for ``dict[str, Any]``
fields. The Gemini Developer API rejects that keyword (Enterprise-only).
ATLAS still parses and validates the model JSON independently.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


def gemini_developer_output_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema for Gemini Developer API structured output.

    Recursively removes ``additionalProperties``. Does not relax ATLAS
    ``parse_model_decision`` / ``validate_decision`` afterward.
    """
    return _strip_additional_properties(model.model_json_schema())


def schema_contains_additional_properties(schema: Any) -> bool:
    """Whether a JSON-schema tree still names additionalProperties."""
    if isinstance(schema, dict):
        if "additionalProperties" in schema:
            return True
        return any(schema_contains_additional_properties(value) for value in schema.values())
    if isinstance(schema, list):
        return any(schema_contains_additional_properties(item) for item in schema)
    return False


def _strip_additional_properties(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            key: _strip_additional_properties(value)
            for key, value in node.items()
            if key != "additionalProperties"
        }
    if isinstance(node, list):
        return [_strip_additional_properties(item) for item in node]
    return node
