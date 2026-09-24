"""Existing Keel state and local JSON integration adapters."""
from keel_trust.common import keys, require
from keel_flow.common import strict_json
from keel_agent.io import MAX_JSON_BYTES
from .api import dispatch, APIError
from .service import Conflict
from .model import adapt_body, utcnow

EOF = object()


def snapshot_from_agent(state, *, labels=None, revision_sources=None, now=None):
    """Read an already-open LocalState through its verified public snapshot API.

    The caller owns state construction and the trusted clock. No databases,
    keys, source observations, approvals or signed envelopes are created here.
    """
    from keel_agent.state import LocalState
    require(isinstance(state, LocalState), "an existing Keel LocalState instance is required")
    envelope = state.latest_snapshot(now=now or utcnow())
    return adapt_body(envelope["body"], state.workspace_id, labels=labels, revision_sources=revision_sources)


def bridge_message(app, value):
    """One JSON-line request. This protocol is Keel-specific, not MCP."""
    try:
        keys(value, {"id", "method", "path", "body"})
        require(type(value["id"]) is str and 0 < len(value["id"]) <= 128, "bounded request id required")
        require(value["method"] in {"GET", "POST"}, "unsupported method")
        require(type(value["path"]) is str and value["path"].startswith("/"), "endpoint path required")
        if value["method"] == "GET": require(value["body"] is None, "GET body must be null")
        return {"id": value["id"], "status": 200, "data": dispatch(app, value["method"], value["path"], value["body"])}
    except APIError as error:
        status, code, message = error.status, error.code, str(error)
    except Conflict as error:
        status, code, message = 409, "CONFLICT", str(error)
    except (ValueError, TypeError, KeyError, RecursionError) as error:
        status, code, message = 400, "INVALID_INPUT", str(error)[:300]
    return {"id": value.get("id") if type(value) is dict and type(value.get("id")) is str else None,
            "status": status, "error": {"code": code, "message": message}}


def read_message(stream):
    raw = stream.readline(MAX_JSON_BYTES+1)
    if not raw: return EOF
    if len(raw) > MAX_JSON_BYTES: raise ValueError("JSON line exceeds 8 MiB; stopping the bridge")
    return strict_json(raw)
