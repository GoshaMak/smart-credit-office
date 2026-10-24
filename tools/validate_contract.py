"""Strict YAML, OpenAPI 3.1, examples and auth cache-header validation.

Run from the repository root with:
    uv run --project backend --locked python tools/validate_contract.py
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import OpenAPIV31SpecValidator
from yaml.constructor import ConstructorError


DEFAULT_CONTRACT = Path(__file__).resolve().parents[1] / "docs" / "openapi.yaml"
HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


class ContractError(ValueError):
    """The contract is invalid or violates project-wide HTTP rules."""


class StrictLoader(yaml.SafeLoader):
    """Preserve date strings and reject mapping keys overwritten by YAML."""

    yaml_implicit_resolvers = {
        key: [(tag, regex) for tag, regex in entries if tag != "tag:yaml.org,2002:timestamp"]
        for key, entries in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict:
        # Check before flattening: merge keys could otherwise hide an overwrite.
        result = {}
        for key_node, value_node in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                raise ConstructorError(None, None, "YAML merge keys are not allowed", key_node.start_mark)
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in result
            except TypeError as exc:
                raise ConstructorError(None, None, "Unhashable mapping key", key_node.start_mark) from exc
            if duplicate:
                raise ConstructorError(None, None, f"Duplicate YAML key: {key!r}", key_node.start_mark)
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def load_contract(path: Path = DEFAULT_CONTRACT) -> dict[str, Any]:
    try:
        document = yaml.load(path.read_text(encoding="utf-8"), Loader=StrictLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise ContractError(str(exc)) from exc
    if not isinstance(document, dict):
        raise ContractError("Contract root must be an object")
    return document


def pointer(parts: tuple[str, ...]) -> str:
    return "#/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in parts)


def resolve_local(document: dict, value: dict) -> dict:
    seen = set()
    while "$ref" in value:
        reference = value["$ref"]
        if not reference.startswith("#/") or reference in seen:
            raise ContractError(f"Expected an acyclic local reference: {reference}")
        seen.add(reference)
        value = document
        for part in reference[2:].split("/"):
            value = value[part.replace("~1", "/").replace("~0", "~")]
    return value


def schema_validator(document: dict, schema_pointer: str) -> Draft202012Validator:
    # Keep references anchored to the complete document, including components.
    return Draft202012Validator(
        {**document, "$ref": schema_pointer}, format_checker=FormatChecker()
    )


def check_examples(document: dict) -> int:
    checked = 0

    def check(value: Any, schema_path: tuple[str, ...], label: str) -> None:
        nonlocal checked
        errors = list(schema_validator(document, pointer(schema_path)).iter_errors(value))
        if errors:
            raise ContractError(f"Invalid example at {label}: {errors[0].message}")
        checked += 1

    def walk(node: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(node, dict):
            # Media Type and Parameter examples use an adjacent schema.
            if "schema" in node:
                if "example" in node:
                    check(node["example"], path + ("schema",), pointer(path) + "/example")
                if isinstance(node.get("examples"), dict):
                    for name, example in node["examples"].items():
                        example = resolve_local(document, example)
                        if "value" in example:
                            check(example["value"], path + ("schema",), pointer(path) + f"/examples/{name}")
            # JSON Schema examples are arrays; OpenAPI's legacy example is singular.
            if len(path) >= 3 and path[:2] == ("components", "schemas"):
                if isinstance(node.get("examples"), list):
                    for index, value in enumerate(node["examples"]):
                        check(value, path, pointer(path) + f"/examples/{index}")
                if "example" in node:
                    check(node["example"], path, pointer(path) + "/example")
            for key, value in node.items():
                if key not in {"example", "examples"}:
                    walk(value, path + (str(key),))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, path + (str(index),))

    walk(document)
    return checked


def check_auth_headers(document: dict) -> None:
    for path, item in document["paths"].items():
        if not path.startswith("/auth/"):
            continue
        for method, operation in item.items():
            if method not in HTTP_METHODS:
                continue
            for status, response in operation["responses"].items():
                response = resolve_local(document, response)
                header = response.get("headers", {}).get("Cache-Control", {})
                header = resolve_local(document, header)
                if header.get("schema", {}).get("const") != "no-store":
                    raise ContractError(f"{method.upper()} {path} {status}: Cache-Control must be no-store")


def validate_contract(path: Path = DEFAULT_CONTRACT) -> tuple[dict, int]:
    document = load_contract(path)
    if document.get("openapi") != "3.1.0":
        raise ContractError("This project requires OpenAPI 3.1.0")
    errors = list(OpenAPIV31SpecValidator(document, base_uri=path.resolve().as_uri()).iter_errors())
    if errors:
        raise ContractError(str(errors[0]))
    check_auth_headers(document)
    return document, check_examples(document)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_CONTRACT)
    args = parser.parse_args()
    try:
        document, examples = validate_contract(args.path)
    except ContractError as exc:
        parser.exit(1, f"Contract validation failed: {exc}\n")
    operations = sum(method in HTTP_METHODS for item in document["paths"].values() for method in item)
    print(f"OpenAPI {document['info']['version']}: {operations} operations, {examples} valid examples; strict YAML and auth headers OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
