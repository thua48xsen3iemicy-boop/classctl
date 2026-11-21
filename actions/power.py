"""Shared identifiers and message shapes for the two system power actions."""

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
PERMISSION = "system.power"
TTL = 10
PARAMETER_SCHEMA = "system.power.empty.v1"
SHUTDOWN = "system.shutdown"
REBOOT = "system.reboot"
CAPABILITIES = [
    dict(
        actionId=action_id,
        label=label,
        permission=PERMISSION,
        context="system",
        maxTtlSeconds=TTL,
        parameterSchema=PARAMETER_SCHEMA,
    )
    for action_id, label in [(SHUTDOWN, "Выключить компьютер"), (REBOOT, "Перезагрузить компьютер")]
]

schema = json.loads((CONTRACTS / "v1.schema.json").read_text())
WIRE = Draft202012Validator(schema, format_checker=FormatChecker())
portal_schema = json.loads((CONTRACTS / "power-v1.schema.json").read_text())
registry = Registry().with_resources([
    ("urn:classctl:control:v1", Resource.from_contents(schema)),
    ("urn:classctl:power:v1", Resource.from_contents(portal_schema)),
])
PORTAL = Draft202012Validator(portal_schema, registry=registry)
PORTAL_GROUP = Draft202012Validator(
    json.loads((CONTRACTS / "power-v2.schema.json").read_text()), registry=registry,
)
PARAMETERS = PORTAL.evolve(schema=PORTAL.schema["$defs"]["parameters"])


def outcome(node_id, state, reason):
    return dict(nodeId=node_id, state=state, reason=reason)


def result(request_id, outcomes):
    return dict(version=1, kind="action.result", requestId=request_id, outcomes=outcomes)
