"""Tiny JSON-schema subset validator; keeps the demo dependency-free."""

from __future__ import annotations

from typing import Any


def validate(value: Any, schema: dict[str, Any], path: str = "$", *, strict: bool = False) -> list[str]:
    errors: list[str] = []
    if "anyOf" in schema and not any(
        not validate(value, choice, path, strict=strict) for choice in schema["anyOf"]
    ):
        errors.append(f"{path}: no anyOf schema matched")
    expected = schema.get("type")
    type_ok = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
    }
    if expected in type_ok and not type_ok[expected]:
        return [f"{path}: expected {expected}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value is not in enum")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{path}.{name}: required")
        for name, child in value.items():
            if name in properties:
                errors.extend(validate(child, properties[name], f"{path}.{name}", strict=strict))
            elif strict and schema.get("additionalProperties") is False:
                errors.append(f"{path}.{name}: unexpected")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            errors.extend(validate(item, schema["items"], f"{path}[{index}]", strict=strict))
    return errors

