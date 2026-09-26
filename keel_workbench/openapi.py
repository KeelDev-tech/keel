"""OpenAPI 3.1.1 description of the small Workbench integration surface."""
from . import __version__
from .model import SCHEMA, LANES
from .workflows import CATALOG


def obj(properties, required=None):
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required,
            "additionalProperties": False}


def specification():
    string = {"type": "string", "minLength": 1}; digest = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
    maybe_object = {"type": ["object", "null"]}
    label = obj({"role_id": string, "company": {**string, "maxLength": 200},
                 "title": {**string, "maxLength": 200}, "lane": {"type": "string", "enum": list(LANES)}})
    snapshot = obj({"schema_version": {"type": "integer", "const": 1}, "schema": {"const": SCHEMA},
                    "workspace_id": string, "synthetic": {"type": "boolean"},
                    "flow": {"type": "object", "description": "Keel 0.7 canonical flow export; validated by keel_flow.board.build."},
                    "assurance": {**maybe_object, "description": "Exact flow-bound Keel assurance export or null."},
                    "trust": {**maybe_object, "description": "Exact flow-bound Keel trust export or null."},
                    "revision_sources": {**maybe_object, "description": "keel.revision_sources.v1 export, same workspace and role identities; checked by keel_agent.revisions."},
                    "labels": {"type": "array", "maxItems": 2000, "items": label}})
    properties = {"request_id": {**string, "maxLength": 128}, "workflow_id": {"enum": [r["id"] for r in CATALOG]},
                  "snapshot_sha256": digest, "role_ids": {"type": "array", "uniqueItems": True, "maxItems": 2000, "items": string},
                  "options": {"type": "object"}}
    variants = []
    for identifier in [r["id"] for r in CATALOG]:
        options = obj({})
        if identifier == "daily-brief": options = obj({"budget_minutes": {"type": "integer", "minimum": 0, "maximum": 240}})
        if identifier == "twin-scenario":
            options = obj({"delay_minutes": {"type": "integer", "minimum": 0, "maximum": 1440},
                           "capacity_multiplier": {"type": "number", "minimum": .25, "maximum": 4},
                           "rate_hold_source": {"type": ["string", "null"]}, "invalidate_role": {"type": ["string", "null"]}})
        variant = {**properties, "workflow_id": {"const": identifier}, "options": options}
        if identifier in {"daily-brief", "incident-replay", "twin-scenario"}:
            variant["role_ids"] = {"type": "array", "maxItems": 0}
        variants.append(obj(variant))
    error = obj({"error": obj({"code": string, "message": {"type": "string"}})})
    schemas = {"Snapshot": snapshot, "RunRequest": {"oneOf": variants}, "Error": error,
               "ImportRequest": obj({"previous_sha256": digest, "snapshot": {"$ref": "#/components/schemas/Snapshot"}})}
    paths = {}
    for path, method, operation, request_schema, response_schema in [
        ("/health", "get", "health", None, None),
        ("/api/v1/overview", "get", "overview", None, None),
        ("/api/v1/snapshot", "get", "snapshot", None, "Snapshot"),
        ("/api/v1/workflows", "get", "workflows", None, None),
        ("/api/v1/history", "get", "sessionHistory", None, None),
        ("/api/v1/run", "post", "runWorkflow", "RunRequest", None),
        ("/api/v1/snapshot", "post", "importSnapshot", "ImportRequest", None),
        ("/openapi.json", "get", "openapi", None, None),
    ]:
        operation_data = {"operationId": operation, "responses": {"200": {"description": "Local check result; never execution authority",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/"+response_schema} if response_schema else {"type": "object"}}}}}}
        for code in (400, 401, 403, 404, 405, 409, 411, 413, 414, 415, 500):
            operation_data["responses"][str(code)] = {"description": "Request rejected",
                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}
        operation_data["responses"]["503"] = {"description": "Local concurrency limit reached; empty response"}
        if request_schema: operation_data["requestBody"] = {"required": True, "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/"+request_schema}}}}
        if path == "/health": operation_data["security"] = []
        paths.setdefault(path, {})[method] = operation_data
    return {"openapi": "3.1.1", "info": {"title": "Keel Workbench API", "version": __version__,
             "description": "Loopback-only review API. POST imports replace a session copy. Run results never authorize execution. Nested Keel contracts are validated by the bundled reducers; see WORKBENCH.md."},
            "servers": [{"url": "/"}], "security": [{"localSession": []}], "paths": paths,
            "components": {"securitySchemes": {"localSession": {"type": "http", "scheme": "bearer"}}, "schemas": schemas}}
