"""Request and response validation against the published OpenAPI contract."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker


CONTRACT_PATH = Path(__file__).resolve().parents[3] / "docs" / "openapi.yaml"
FORMAT_CHECKER = FormatChecker()


@lru_cache(maxsize=1)
def contract() -> dict[str, Any]:
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


def validate(schema: dict[str, Any], value: Any) -> list[dict[str, str]]:
    """Return public field errors using the contract's ErrorDetail shape."""
    document = contract()
    validator = Draft202012Validator(
        {**document, "allOf": [schema]}, format_checker=FORMAT_CHECKER
    )
    details = []
    for error in sorted(validator.iter_errors(value), key=lambda e: list(map(str, e.path))):
        field = "".join(
            "/" + str(part).replace("~", "~0").replace("/", "~1")
            for part in error.absolute_path
        )
        # ErrorDetail.field is optional; an error at the document root has no field.
        detail = {"message": error.message}
        if field:
            detail["field"] = field
        details.append(detail)
    return details


def dereference(value: dict[str, Any]) -> dict[str, Any]:
    document = contract()
    while "$ref" in value:
        node: Any = document
        for part in value["$ref"][2:].split("/"):
            node = node[part.replace("~1", "/").replace("~0", "~")]
        value = node
    return value


def operation_parameters(operation: dict[str, Any], raw: dict[str, str]) -> tuple[dict, list]:
    """Parse documented path/query parameters and apply schema defaults."""
    parsed: dict[str, Any] = {}
    details: list[dict[str, str]] = []
    for reference in operation.get("parameters", []):
        parameter = dereference(reference)
        name = parameter["name"]
        schema = dereference(parameter["schema"])
        if name not in raw:
            if parameter.get("required"):
                details.append({"field": f"/{name}", "message": "Обязательный параметр отсутствует."})
            elif "default" in schema:
                parsed[name] = schema["default"]
            continue
        value: Any = raw[name]
        if schema.get("type") == "integer":
            try:
                if not value.isdecimal():
                    raise ValueError
                value = int(value)
            except ValueError:
                details.append({"field": f"/{name}", "message": "Ожидается целое число."})
                continue
        elif schema.get("type") == "boolean":
            if value not in {"true", "false"}:
                details.append({"field": f"/{name}", "message": "Ожидается true или false."})
                continue
            value = value == "true"
        issues = validate(parameter["schema"], value)
        if issues:
            details.extend({"field": f"/{name}", "message": issue["message"]} for issue in issues)
        else:
            parsed[name] = value
    return parsed, details
