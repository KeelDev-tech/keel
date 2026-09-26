"""Bounded public HTTPS reads, without proxies, cookies, credentials or POST.

Each redirect is independently resolved and checked. The transport connects
to the checked IP with TLS SNI/certificate checks against the original host.
This confines bundled readers, not arbitrary plugins or local Python code.
"""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import http.client
import io
import ipaddress
import math
import hashlib
import os
from pathlib import Path
import re
import queue
import threading
import socket
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl
from urllib.request import Request
from safe_io import atomic_json, contained_path, file_lock, read_json

MAX_BYTES = 4 * 1024 * 1024
MAX_REDIRECTS = 3
_backoff = {}
_backoff_lock = threading.Lock()
ALLOWED_HEADERS = {"user-agent", "accept", "accept-language", "if-none-match", "if-modified-since"}
SECRET_QUERY = re.compile(r"(?:password|passwd|secret|token|api.?key|credential|authorization)", re.I)


class NetworkPolicyError(URLError):
    pass


def validate_url(url):
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise NetworkPolicyError("invalid URL")
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port
    except (ValueError, UnicodeError) as exc:
        raise NetworkPolicyError("invalid URL authority") from exc
    if parsed.scheme != "https" or not host or port not in (None, 443):
        raise NetworkPolicyError("only public HTTPS on port 443 is supported")
    if parsed.username is not None or parsed.password is not None or "\\" in url or "%" in host:
        raise NetworkPolicyError("URL credentials and ambiguous authorities are prohibited")
    if any(SECRET_QUERY.search(k) and not
           (k == "token" and host in {"job-boards.greenhouse.io", "boards.greenhouse.io"}
            and parsed.path == "/embed/job_app" and v.isascii() and v.isdigit())
           for k, v in parse_qsl(parsed.query)):
        raise NetworkPolicyError("credential-like query parameters are prohibited")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise NetworkPolicyError("local host prohibited")
    authority = "[" + host + "]" if ":" in host else host
    return urlunsplit(("https", authority, parsed.path or "/", parsed.query, ""))


def _public(address):
    ip = ipaddress.ip_address(address)
    # Mapped, NAT64, 6to4 and Teredo addresses can hide an IPv4 destination.
    translated = ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32")
    return (ip.is_global and not ip.is_multicast and not ip.is_unspecified
            and not getattr(ip, "ipv4_mapped", None)
            and not (ip.version == 6 and any(ip in ipaddress.ip_network(n) for n in translated)))


_dns_slots = threading.BoundedSemaphore(8)


def resolve_public(host, *, timeout=5):
    # A stuck system resolver must not hang the caller or spawn unbounded threads.
    if not _dns_slots.acquire(blocking=False):
        raise NetworkPolicyError("DNS resolver capacity exhausted")
    result = queue.Queue(maxsize=1)
    def lookup():
        try:
            result.put((True, socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
        except Exception as exc:
            result.put((False, exc))
        finally:
            _dns_slots.release()
    threading.Thread(target=lookup, daemon=True, name="keel-dns").start()
    try:
        ok, records = result.get(timeout=min(5, timeout))
    except queue.Empty as exc:
        raise TimeoutError("DNS deadline exceeded") from exc
    if not ok:
        raise URLError(type(records).__name__) from records
    if not records or len(records) > 64:
        raise NetworkPolicyError("empty or excessive DNS answer")
    for family, kind, proto, canon, address in records:
        if family not in (socket.AF_INET, socket.AF_INET6) or not _public(address[0]):
            raise NetworkPolicyError("DNS answer includes a non-public destination")
    return records


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("HTTPS deadline exceeded")
    return remaining


def _cooldown_path(host):
    root = Path(os.environ.get("KEEL_HOME", str(Path.home() / "keel"))).absolute()
    name = hashlib.sha256(host.encode("ascii")).hexdigest()
    return contained_path(root, Path("data/http-cooldowns") / (name + ".json"), must_exist=False)


class HostRateLimited(NetworkPolicyError):
    """A host is cooling down after HTTP 429; cached reads are denied."""


def _check_host_cooldown_locked(host, path):
    """Check both hold stores while the caller owns the host's file lock."""
    state = read_json(path, missing=None, limit=4096)
    if state is not None:
        if (not isinstance(state, dict) or type(state.get("schema_version")) is not int
                or state["schema_version"] != 1 or state.get("host") != host or "until" not in state):
            raise NetworkPolicyError("invalid persistent host cooldown")
        until = state["until"]
        if until is not None and (type(until) not in (int, float) or not math.isfinite(until) or until < 0):
            raise NetworkPolicyError("invalid persistent cooldown expiry")
        if until is None or until > time.time():
            raise HostRateLimited("host is cooling down after HTTP 429")
    with _backoff_lock:
        if _backoff.get(host, 0) > time.monotonic():
            raise HostRateLimited("host is cooling down after HTTP 429")
        _backoff.pop(host, None)


def check_host_cooldown(url, *, timeout=20):
    """Deny cached reads during a shared host hold, without DNS or transport."""
    host = urlsplit(validate_url(url)).hostname
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise NetworkPolicyError("timeout must be between 0 and 60 seconds")
    path = _cooldown_path(host)
    deadline = time.monotonic() + timeout
    with file_lock(str(path) + ".lock", timeout=_remaining(deadline)):
        _remaining(deadline)
        _check_host_cooldown_locked(host, path)


def _retry_delay(raw):
    # RFC delta-seconds is an integer, not a float/exponent or signed number.
    raw = str(raw).strip()
    if re.fullmatch(r"[0-9]+", raw):
        value = float(raw)
        return max(60, value) if math.isfinite(value) else None
    try:
        leap_second = bool(re.search(r"\d{2}:\d{2}:60(?: |$)", raw))
        date_text = re.sub(r"(\d{2}:\d{2}):60(?= |$)", r"\1:59", raw) if leap_second else raw
        date = parsedate_to_datetime(date_text)
        if date.tzinfo is None:
            # The obsolete asctime HTTP-date form omits GMT but still means UTC.
            date = date.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        short_year = re.fullmatch(r"[A-Za-z]+, \d{2}-[A-Za-z]{3}-(\d{2}) \d{2}:\d{2}:\d{2} GMT", raw)
        if short_year:
            year = now.year // 100 * 100 + int(short_year.group(1))
            date = date.replace(year=year)
            future_limit = (now.year + 50, now.month, now.day, now.hour, now.minute, now.second)
            if (date.year, date.month, date.day, date.hour, date.minute, date.second) > future_limit:
                date = date.replace(year=year - 100)
        if leap_second:
            date += timedelta(seconds=1)
        return max(60, (date - now).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return 60


def _read_host(url, method, headers, deadline, max_bytes):
    """Serialize cooperating readers of one host, including admission and 429 writes.

    Workspace-local POSIX locks are not a distributed fleet rate limiter.
    Persistent expiry assumes a reasonably synchronized wall clock; None means
    an unrepresentably long provider hold, requiring operator reconciliation.
    State read/lock errors deny admission. A failed 429 write raises and retains
    an in-process hold; other processes cannot inherit a hold that storage could
    not persist. Storage failure remains an operational recovery boundary.
    """
    host = urlsplit(url).hostname
    path = _cooldown_path(host)
    with file_lock(str(path) + ".lock", timeout=_remaining(deadline)):
        _remaining(deadline)
        state = read_json(path, missing=None, limit=4096)
        if state is not None:
            if (not isinstance(state, dict) or type(state.get("schema_version")) is not int
                    or state["schema_version"] != 1 or state.get("host") != host or "until" not in state):
                raise NetworkPolicyError("invalid persistent host cooldown")
            until = state["until"]
            if until is not None and (type(until) not in (int, float) or not math.isfinite(until) or until < 0):
                raise NetworkPolicyError("invalid persistent cooldown expiry")
            if until is None or until > time.time():
                raise NetworkPolicyError("host is cooling down after HTTP 429")
        with _backoff_lock:
            if _backoff.get(host, 0) > time.monotonic():
                raise NetworkPolicyError("host is cooling down after HTTP 429")
            _backoff.pop(host, None)
        records = resolve_public(host, timeout=_remaining(deadline))
        result = _exchange(url, method, headers, records, _remaining(deadline), max_bytes)
        if result.status == 429:
            delay = _retry_delay(result.headers.get("Retry-After", "60"))
            until = time.time() + delay if delay is not None else None
            if until is not None and not math.isfinite(until):
                until = None
            with _backoff_lock:
                _backoff[host] = time.monotonic() + delay if delay is not None else math.inf
            try:
                atomic_json(path, {"schema_version": 1, "host": host, "until": until})
            except Exception:
                result.close()
                raise
        return result


class Response(io.BytesIO):
    def __init__(self, body, status, headers, url):
        super().__init__(body)
        self.status, self.code, self.headers, self.url = status, status, headers, url

    def getcode(self):
        return self.status

    def geturl(self):
        return self.url

    def info(self):
        return self.headers


def _exchange(url, method, headers, records, timeout, max_bytes):
    parsed = urlsplit(url)
    deadline = time.monotonic() + timeout
    context = ssl.create_default_context()
    context.set_alpn_protocols(["http/1.1"])
    last_error = None
    # DNS is not consulted again by the socket connection.
    for family, kind, proto, canon, address in records:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("HTTPS deadline exceeded")
        sock = socket.socket(family, kind, proto)
        conn = http.client.HTTPSConnection(parsed.hostname, timeout=remaining, context=context)
        active = {"socket": sock}
        response = None
        def expire():
            try:
                active["socket"].shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        timer = threading.Timer(remaining, expire)
        timer.daemon = True
        timer.start()
        try:
            sock.settimeout(remaining)
            sock.connect(address)
            conn.sock = context.wrap_socket(sock, server_hostname=parsed.hostname)
            active["socket"] = conn.sock
            conn.request(method, (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""),
                         headers={**headers, "Accept-Encoding": "identity"})
            response = conn.getresponse()
            length = response.getheader("Content-Length")
            if length and (int(length) < 0 or int(length) > max_bytes):
                raise NetworkPolicyError("response exceeds size limit")
            if response.getheader("Content-Encoding", "identity").lower() not in ("identity", ""):
                raise NetworkPolicyError("compressed responses are not supported")
            chunks, size = [], 0
            while method != "HEAD":
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("HTTPS deadline exceeded")
                if conn.sock:
                    conn.sock.settimeout(remaining)
                chunk = response.read1(min(65536, max_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > max_bytes:
                    raise NetworkPolicyError("response exceeds size limit")
            return Response(b"".join(chunks), response.status, response.headers, url)
        except (OSError, http.client.HTTPException) as exc:
            last_error = exc
            if isinstance(exc, NetworkPolicyError):
                raise
        finally:
            timer.cancel()
            if response is not None:
                response.close()
            conn.close()
            sock.close()
    raise URLError(type(last_error).__name__ if last_error else "connection failed")


def urlopen(url, data=None, timeout=20, *, max_bytes=MAX_BYTES):
    if data is not None:
        raise NetworkPolicyError("request bodies prohibited")
    method, headers = "GET", {}
    if isinstance(url, Request):
        method = url.get_method()
        if url.data is not None:
            raise NetworkPolicyError("request bodies prohibited")
        headers = dict(url.header_items())
        url = url.full_url
    if method not in ("GET", "HEAD"):
        raise NetworkPolicyError("only GET and HEAD are supported")
    if any(k.lower() not in ALLOWED_HEADERS for k in headers):
        raise NetworkPolicyError("request header outside the read-only allowlist")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise NetworkPolicyError("timeout must be between 0 and 60 seconds")
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_BYTES:
        raise NetworkPolicyError("invalid response size limit")
    deadline = time.monotonic() + timeout
    seen = set()
    for hop in range(MAX_REDIRECTS + 1):
        url = validate_url(url)
        if url in seen:
            raise NetworkPolicyError("redirect loop")
        seen.add(url)
        result = _read_host(url, method, headers, deadline, max_bytes)
        if result.status in (301, 302, 303, 307, 308):
            target = result.headers.get("Location")
            result.close()
            if not target or hop == MAX_REDIRECTS:
                raise NetworkPolicyError("redirect limit or missing destination")
            url = urljoin(url, target)
            headers = {k: v for k, v in headers.items() if k.lower() in {"user-agent", "accept"}}
            continue
        if result.status >= 400:
            raise HTTPError(url, result.status, "HTTPS response error", result.headers, result)
        return result
    raise NetworkPolicyError("redirect limit")
