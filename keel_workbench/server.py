"""Loopback-only desktop server, not an Internet-facing production gateway."""
import hmac
import json
import secrets
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore
from urllib.parse import urlsplit

from keel_flow.common import strict_json
from keel_agent.io import MAX_JSON_BYTES
from .api import dispatch, APIError
from .service import Conflict

STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8")}


def check_request(headers, *, authority, token, authenticated=True):
    """Reject alternate hosts, cross-origin requests and ambiguous framing."""
    if headers.get_all("Host") != [authority]:
        raise APIError(403, "HOST_REJECTED", "use the exact local URL printed by the server")
    origin = headers.get_all("Origin")
    if origin is not None and origin != ["http://"+authority]:
        raise APIError(403, "ORIGIN_REJECTED", "cross-origin requests are not supported")
    if headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
        raise APIError(403, "ORIGIN_REJECTED", "same-origin requests required")
    if headers.get_all("Transfer-Encoding") is not None:
        raise APIError(400, "FRAMING_REJECTED", "chunked requests are not supported")
    if authenticated:
        auth = headers.get_all("Authorization")
        if not auth or len(auth) != 1 or not hmac.compare_digest(auth[0].encode(), ("Bearer "+token).encode()):
            raise APIError(401, "TOKEN_REQUIRED", "open the private session link printed by the server")


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, app, port=8765, *, token=None):
        if type(port) is not int or not 0 <= port <= 65535: raise ValueError("invalid local port")
        self.app = app; self.token = token or secrets.token_urlsafe(32)
        if not isinstance(self.token, str) or len(self.token) < 32 or not self.token.isascii():
            raise ValueError("a random ASCII session token of at least 32 characters is required")
        self.slots = BoundedSemaphore(8)
        super().__init__(("127.0.0.1", port), Handler)
        self.authority = "127.0.0.1:"+str(self.server_port)

    def get_request(self):
        request, address = super().get_request(); request.settimeout(5)
        return request, address

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            try: request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError: pass
            self.shutdown_request(request); return
        try: super().process_request(request, address)
        except BaseException:
            self.slots.release(); raise

    def process_request_thread(self, request, address):
        try: super().process_request_thread(request, address)
        finally: self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "KeelWorkbench"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def log_message(self, format, *args):
        pass  # Do not log request headers, imported data or URL fragments.

    def send_payload(self, status, data, content_type="application/json; charset=utf-8"):
        raw = data if isinstance(data, bytes) else json.dumps(data, allow_nan=False, ensure_ascii=True).encode()
        self.send_response(status)
        for name, value in {"Content-Type": content_type, "Content-Length": str(len(raw)),
                            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                            "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                            "Cross-Origin-Resource-Policy": "same-origin", "Connection": "close",
                            "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"}.items():
            self.send_header(name, value)
        self.end_headers(); self.wfile.write(raw); self.close_connection = True

    def handle_api(self):
        try:
            if len(self.path) > 2048: raise APIError(414, "PATH_TOO_LONG", "path too long")
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
                raise APIError(400, "PATH_REJECTED", "use an endpoint without query parameters")
            path = parsed.path
            static = self.command == "GET" and path in STATIC
            check_request(self.headers, authority=self.server.authority, token=self.server.token,
                          authenticated=not (static or self.command == "GET" and path == "/health"))
            body = None
            lengths = self.headers.get_all("Content-Length")
            if self.command == "POST":
                if not lengths or len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                    raise APIError(411, "LENGTH_REQUIRED", "one Content-Length is required")
                size = int(lengths[0])
                if not 0 < size <= MAX_JSON_BYTES: raise APIError(413, "BODY_TOO_LARGE", "JSON body must be 1 byte to 8 MiB")
                if self.headers.get_content_type() != "application/json":
                    raise APIError(415, "JSON_REQUIRED", "Content-Type must be application/json")
                if self.headers.get_content_charset() not in (None, "utf-8") or self.headers.get("Content-Encoding"):
                    raise APIError(415, "ENCODING_REJECTED", "uncompressed UTF-8 JSON required")
                raw = self.rfile.read(size)
                if len(raw) != size: raise APIError(400, "INCOMPLETE_BODY", "incomplete JSON body")
                body = strict_json(raw)
            elif lengths and lengths != ["0"]:
                raise APIError(400, "BODY_REJECTED", "GET does not accept a body")
            if static:
                name, kind = STATIC[path]
                return self.send_payload(200, (Path(__file__).parent/"static"/name).read_bytes(), kind)
            self.send_payload(200, dispatch(self.server.app, self.command, path, body))
        except APIError as error:
            self.send_payload(error.status, {"error": {"code": error.code, "message": str(error)}})
        except Conflict as error:
            self.send_payload(409, {"error": {"code": "CONFLICT", "message": str(error)}})
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
