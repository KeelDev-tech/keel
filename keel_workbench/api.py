"""Transport-independent, versioned API. All changes are session-local."""
from keel_trust.common import keys
from . import __version__


class APIError(ValueError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status; self.code = code


def dispatch(app, method, path, body=None):
    if method == "GET":
        if path == "/health":
            return {"status": "ok", "service": "keel-workbench", "version": __version__}
        if path == "/api/v1/overview": return app.overview()
        if path == "/api/v1/snapshot": return app.snapshot()
        if path == "/api/v1/workflows": return app.catalog()
        if path == "/api/v1/history": return app.history()
        if path == "/openapi.json":
            from .openapi import specification
            return specification()
    elif method == "POST":
        if path == "/api/v1/run": return app.run(body)
        if path == "/api/v1/snapshot":
            keys(body, {"previous_sha256", "snapshot"})
            return app.replace(body["snapshot"], previous_sha256=body["previous_sha256"])
    else:
        raise APIError(405, "METHOD_NOT_ALLOWED", "GET and POST are supported")
    raise APIError(404, "NOT_FOUND", "unknown endpoint")
