"""Bounded, loopback-only review server reusing Workbench transport controls."""
import socket
from pathlib import Path
from urllib.parse import urlsplit

from keel_agent.io import MAX_JSON_BYTES
from keel_flow.common import strict_json
from keel_workbench.api import APIError
from keel_workbench.service import Conflict
from keel_workbench.server import LocalServer, Handler, check_request, STATIC as WORKBENCH_STATIC
from .review import ReviewError

HERE = Path(__file__).parent
WORKBENCH = HERE.parent / "keel_workbench" / "static"
STATIC = {path: (WORKBENCH / name, kind) for path, (name, kind) in WORKBENCH_STATIC.items()}
STATIC.update({"/review": (HERE / "static" / "review.html", "text/html; charset=utf-8"),
               "/review.js": (HERE / "static" / "review.js", "text/javascript; charset=utf-8"),
               "/review.css": (HERE / "static" / "review.css", "text/css; charset=utf-8"),
               "/live-nav.js": (HERE / "static" / "live-nav.js", "text/javascript; charset=utf-8")})


class LiveHandler(Handler):
    server_version = "KeelLive"

    def handle_api(self):
        try:
            if len(self.path) > 2048:
                raise APIError(414, "PATH_TOO_LONG", "path too long")
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
                raise APIError(400, "PATH_REJECTED", "use an endpoint without query parameters")
            path = parsed.path
            static = self.command == "GET" and path in STATIC
            check_request(self.headers, authority=self.server.authority, token=self.server.token,
                          authenticated=not (static or self.command == "GET" and path == "/health"))
            if self.command not in ("GET", "POST"):
                raise APIError(405, "METHOD_NOT_ALLOWED", "GET and POST are supported")
            body = None
            lengths = self.headers.get_all("Content-Length")
            if self.command == "POST":
                if not lengths or len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                    raise APIError(411, "LENGTH_REQUIRED", "one Content-Length is required")
                size = int(lengths[0])
                if not 0 < size <= MAX_JSON_BYTES:
                    raise APIError(413, "BODY_TOO_LARGE", "JSON body must be 1 byte to 8 MiB")
                if self.headers.get_all("Content-Type") is None or len(self.headers.get_all("Content-Type")) != 1:
                    raise APIError(415, "JSON_REQUIRED", "one JSON Content-Type is required")
                if self.headers.get_content_type() != "application/json":
                    raise APIError(415, "JSON_REQUIRED", "Content-Type must be application/json")
                if self.headers.get_content_charset() not in (None, "utf-8") or self.headers.get("Content-Encoding"):
                    raise APIError(415, "ENCODING_REJECTED", "uncompressed UTF-8 JSON required")
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise APIError(400, "INCOMPLETE_BODY", "incomplete JSON body")
                body = strict_json(raw)
            elif lengths and lengths != ["0"]:
                raise APIError(400, "BODY_REJECTED", "GET does not accept a body")
            if static:
                file, kind = STATIC[path]
                data = file.read_bytes()
                if path == "/":
                    data = data.replace(b'</head>', b'<script src="/live-nav.js" defer></script></head>')
                return self.send_payload(200, data, kind)
            self.send_payload(200, self.server.app.dispatch(self.command, path, body))
        except APIError as error:
            self.send_payload(error.status, {"error": {"code": error.code, "message": str(error)}})
        except Conflict as error:
            self.send_payload(409, {"error": {"code": "CONFLICT", "message": str(error)}})
        except ReviewError as error:
            status = 403 if str(error) in ("review_access_denied", "review_principal_required") else 400
            self.send_payload(status, {"error": {"code": "REVIEW_REJECTED", "message": str(error)}})
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
            self.send_payload(400, {"error": {"code": "INVALID_INPUT", "message": str(error)[:300]}})
        except (socket.timeout, OSError):
            self.close_connection = True
        except Exception:
            self.send_payload(500, {"error": {"code": "INTERNAL_ERROR", "message": "local check failed; inspect the host"}})

    do_GET = handle_api
    do_POST = handle_api
    do_PUT = handle_api
    do_PATCH = handle_api
    do_DELETE = handle_api
    do_OPTIONS = handle_api


class LiveServer(LocalServer):
    """Same 127.0.0.1 binding, eight-client cap, timeout and token as Workbench."""
    def __init__(self, app, port=8765, *, token=None):
        super().__init__(app, port, token=token)
        self.RequestHandlerClass = LiveHandler


def serve(app, port=8765, *, token=None):
    """Return a bound server; host prints its private link and owns its lifetime."""
    return LiveServer(app, port, token=token)
