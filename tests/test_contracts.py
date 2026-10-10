"""Contract-level guarantees that are about what the MODEL SERVER sees, not the Python config."""

from __future__ import annotations

import inspect
import json

import pytest

from backend import contracts
from backend.contracts import CrucibleModel

CONTRACT_CLASSES = sorted((c for _n, c in inspect.getmembers(contracts, inspect.isclass)
                           if issubclass(c, CrucibleModel) and c is not CrucibleModel), key=lambda c: c.__name__)


def _objects(schema: dict):
    """Every object-typed node in a generated JSON schema, $defs included."""
    stack = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                yield node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def test_every_contract_inherits_the_forbid_base():
    assert len(CONTRACT_CLASSES) >= 40
    for name, cls in inspect.getmembers(contracts, inspect.isclass):
        if cls.__module__ == contracts.__name__ and issubclass(cls, contracts.BaseModel):
            assert issubclass(cls, CrucibleModel), name


@pytest.mark.parametrize("cls", CONTRACT_CLASSES, ids=lambda c: c.__name__)
def test_generated_schema_literally_forbids_extra_keys(cls):
    schema = cls.model_json_schema()
    assert '"additionalProperties": false' in json.dumps(schema)  # the artifact, as the server will receive it
    for node in _objects(schema):  # including every nested model in $defs
        if "properties" in node:
            assert node.get("additionalProperties") is False, (cls.__name__, node.get("title"))


def test_unknown_keys_are_rejected_on_validation():
    with pytest.raises(Exception):
        contracts.ContactBlock.model_validate({"emails": [], "phones": [], "urls": [], "confidence": 0.9})
