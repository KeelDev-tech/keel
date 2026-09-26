"""Small Python SDK for the local Workbench API; no third-party dependencies."""
import json
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
from keel_flow.common import strict_json
from keel_agent.io import MAX_JSON_BYTES


class ClientError(RuntimeError):
    def __init__(self, status, detail):
        self.status = status; self.detail = detail
        super().__init__(str(detail))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl): return None


class Client:
    def __init__(self, endpoint, token, *, timeout=10):
        parsed = urlsplit(endpoint)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None or
                parsed.path not in ("", "/") or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("use the loopback http://127.0.0.1:PORT endpoint")
        if type(token) is not str or len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError("valid local session token required")
        if type(timeout) not in (int, float) or not 0 < timeout <= 60: raise ValueError("timeout must be 0–60 seconds")
        self.endpoint = endpoint.rstrip("/"); self.token = token; self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def call(self, method, path, body=None):
        if method not in {"GET", "POST"} or path not in {"/api/v1/overview", "/api/v1/snapshot", "/api/v1/workflows", "/api/v1/run", "/api/v1/history", "/openapi.json", "/health"}:
            raise ValueError("unsupported method or API path")
        data = None if body is None else json.dumps(body, allow_nan=False).encode()
        if data is not None and len(data) > MAX_JSON_BYTES: raise ValueError("request exceeds 8 MiB")
        request = Request(self.endpoint+path, data=data, method=method,
                          headers={"Authorization": "Bearer "+self.token, "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_JSON_BYTES+1)
            if len(raw) > MAX_JSON_BYTES: raise ValueError("response exceeds 8 MiB")
            return strict_json(raw)
        except HTTPError as error:
            raw = error.read(MAX_JSON_BYTES+1); error.close()
            detail = strict_json(raw) if len(raw) <= MAX_JSON_BYTES and raw else {"error": "HTTP request rejected"}
            raise ClientError(error.code, detail) from None

    def overview(self): return self.call("GET", "/api/v1/overview")
    def snapshot(self): return self.call("GET", "/api/v1/snapshot")
    def workflows(self): return self.call("GET", "/api/v1/workflows")
    def history(self): return self.call("GET", "/api/v1/history")
    def run(self, command): return self.call("POST", "/api/v1/run", command)
    def import_snapshot(self, snapshot, *, previous_sha256):
        return self.call("POST", "/api/v1/snapshot", {"previous_sha256": previous_sha256, "snapshot": snapshot})
