"""Shared helpers for backend and contract tests (plain functions, no pytest magic)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "contracts"
FIXTURES = ROOT / "fixtures"
BASE_URI = "https://trademind.example/contracts/"
SCHEMA_FILES = ("scenario.schema.json", "result.schema.json", "api.schema.json")


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def fixture(name: str):
    return load_json(FIXTURES / name)


def make_registry() -> Registry:
    registry = Registry()
    for name in SCHEMA_FILES:
        doc = load_json(CONTRACTS / name)
        registry = registry.with_resource(doc["$id"], Resource.from_contents(doc, default_specification=DRAFT202012))
    return registry


def validator(file_name: str, definition: str | None = None) -> Draft202012Validator:
    """Validator for a whole schema file, or for one of its #/$defs entries."""
    ref = BASE_URI + file_name + (f"#/$defs/{definition}" if definition else "")
    return Draft202012Validator({"$ref": ref}, registry=make_registry())


def apply_ops(document, ops):
    """Apply the set/delete/append ops used by fixtures/contract-cases.json."""
    doc = copy.deepcopy(document)
    for op in ops:
        *parents, last = op["path"]
        node = doc
        for key in parents:
            node = node[key]
        if op["op"] == "set":
            node[last] = op["value"]
        elif op["op"] == "delete":
            del node[last]
        elif op["op"] == "append":
            node[last].append(op["value"])
        else:  # guards against a typo in the case file
            raise ValueError(f"unknown op {op['op']!r}")
    return doc


def replace_canonical(node, canonical):
    """Replace the string '$canonical' anywhere in node with a copy of the canonical scenario."""
    if node == "$canonical":
        return copy.deepcopy(canonical)
    if isinstance(node, dict):
        return {k: replace_canonical(v, canonical) for k, v in node.items()}
    if isinstance(node, list):
        return [replace_canonical(v, canonical) for v in node]
    return node
