"""Reuse the existing bounded contracts; no new persistence layer."""
from keel_flow.common import (ContractError, canonical, digest, strict_json, keys, version, text,
                              integer, require, hexdigest, number, boolean, records, clock,
                              fresh, unique, timestamp)


def strings(value, name="strings", *, maximum=1000, nonempty=False):
    require(type(value) is list and len(value) <= maximum and (value or not nonempty), name + ": bounded array required")
    for item in value: text(item, name)
    require(len(set(value)) == len(value), name + ": duplicates forbidden")
    return value


def choice(value, choices, name):
    require(type(value) is str and value in choices, name + ": unsupported value")
    return value


def clone(value):
    return strict_json(canonical(value))


def envelope(document, fields, *, now):
    clock(now); keys(document, fields | {"schema_version", "workspace_id", "source_revision", "observed_at", "complete"})
    version(document); text(document["workspace_id"]); text(document["source_revision"]); boolean(document["complete"])
    return document["complete"] and fresh(document["observed_at"], now)
