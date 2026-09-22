#!/usr/bin/env python3
"""http_policy.py — pre-request SSRF/redirect-destination admission policy.

Ported policy layer from the Keel 0.3.1 review (K41), adapted to the live
transport. Deliberate adaptations vs the candidate's safe_http.py:

  - http:// is ADMITTED (live verifier fetches plain-http posting pages;
    the candidate's https-only rule would break legitimate liveness checks).
    The SSRF control here is scheme-neutral: authority validation + a
    pre-flight DNS check that refuses any non-public destination.
  - The raw-socket TLS-pinned exchange is NOT ported (it bypasses the proxy
    the live sandbox requires via trust_env). Checked-IP *connection* is
    therefore not enforced: urllib/aiohttp re-resolve at connect time, so a
    TOCTOU gap exists between this check and the socket connect. Documented
    and accepted per the K41 risk note — the check is admission, not a
    connection pin.
  - Host 429 admission/record lives in host_cooldowns.py (NOT yet wired —
    2026-09-19 correction: repo-wide grep shows zero importers of that
    module, so the proxy adaptation below is the only live 429 behavior);
    this module only exposes the URL/DNS policy primitives.
  - Stdlib only. No imports from the live engines (no import cycles).

Consumers: http_cache._network_get, verify_retry_async._get, and the sync
urllib fallback sites in verify_retry.check_live / fetch_posting_text.
"""

import ipaddress
import queue
import re
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request

MAX_BODY_BYTES = 4 * 1024 * 1024   # K43: hard cap on any single fetched body
MAX_REDIRECTS = 3                  # K43: redirect hops per fetch
DNS_TIMEOUT_S = 5                  # K41: bounded pre-flight DNS round-trip
MAX_URL_LEN = 8192

# Some public board APIs return the WHOLE board as one JSON document;
# large boards declare Content-Length above the 4 MiB cap, so read_capped's
# pre-check refused them before a byte was read and every lead on those
# boards went ambiguous. Board listings are JSON job records bounded by
# the employer's open-job count — not hostile pages — so they get their
# own still-hard cap. Applies ONLY to board-API fetches; posting-page
# fetches keep the 4 MiB cap.
ASHBY_BOARD_LIMIT = 12 * 1024 * 1024

REDIRECT_STATUSES = (301, 302, 303, 307, 308)

_SECRET_QUERY = re.compile(
    r"(?:password|passwd|secret|token|api.?key|credential|authorization)", re.I)

# Greenhouse's public job-app embed carries its board token as a query
# parameter. It is the only credential-like query the live verifier must
# be able to fetch (ats.parse_greenhouse recognizes the embed form).
_GH_EMBED_HOSTS = {"job-boards.greenhouse.io", "boards.greenhouse.io"}


class NetworkPolicyError(urllib.error.URLError):
    """Raised when a URL or its DNS answer violates the admission policy.

    Subclasses URLError (not HTTPError), so existing HTTP-status handling
    (404/410 -> dead, other -> ambiguous) is unaffected: policy refusals
    surface as generic fetch failures -> ambiguous / stays parked.
    """


def hostname_of(url):
    """Bare lowercase hostname of a URL, or "" when unparseable."""
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def validate_url(url):
    """Admission-check a URL's authority and shape. Returns the normalized
    URL (scheme + host lowercased, fragment stripped).

    Refuses: non-str/overlong/control-char URLs, non-http(s) schemes,
    userinfo, backslashes, non-default ports, localhost / .local /
    .internal hosts, and credential-like query parameters (with the narrow
    Greenhouse embed exception above).

    Adaptation note: unlike the candidate, plain http:// is admitted —
    the live verifier checks plain-http posting pages.
    """
    if (not isinstance(url, str) or len(url) > MAX_URL_LEN
            or any(ord(c) <= 32 or ord(c) == 127 for c in url)):
        raise NetworkPolicyError("invalid URL")
    try:
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").encode("idna").decode("ascii")
        host = host.lower().rstrip(".")
        port = parsed.port
    except (ValueError, UnicodeError) as exc:
        raise NetworkPolicyError("invalid URL authority") from exc
    if scheme not in ("http", "https") or not host:
        raise NetworkPolicyError("only http/https URLs are supported")
    default_port = 443 if scheme == "https" else 80
    if port not in (None, default_port):
        raise NetworkPolicyError("non-default port prohibited")
    if (parsed.username is not None or parsed.password is not None
            or "\\" in url or "%" in host):
        raise NetworkPolicyError(
            "URL credentials and ambiguous authorities are prohibited")
    if host == "localhost" or host.endswith(
            (".localhost", ".local", ".internal")):
        raise NetworkPolicyError("local host prohibited")
    if any(_SECRET_QUERY.search(k) and not (
            k == "token" and host in _GH_EMBED_HOSTS
            and parsed.path == "/embed/job_app"
            and v.isascii() and v.isdigit())
           for k, v in urllib.parse.parse_qsl(parsed.query)):
        raise NetworkPolicyError("credential-like query parameters prohibited")
    authority = "[" + host + "]" if ":" in host else host
    if port is not None:
        authority += ":" + str(port)
    return urllib.parse.urlunsplit(
        (scheme, authority, parsed.path or "/", parsed.query, ""))


def _public(address):
    """True when the address is a routable public destination."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    # Mapped, NAT64, 6to4 and Teredo addresses can hide an IPv4 destination.
    translated = ("64:ff9b::/96", "64:ff9b:1::/48",
                  "2002::/16", "2001::/32")
    return (ip.is_global and not ip.is_multicast and not ip.is_unspecified
            and not getattr(ip, "ipv4_mapped", None)
            and not (ip.version == 6
                     and any(ip in ipaddress.ip_network(n)
                             for n in translated)))


_dns_slots = threading.BoundedSemaphore(8)


def resolve_public(host, port=443, timeout=DNS_TIMEOUT_S):
    """Resolve `host` and refuse any non-public destination.

    The system resolver runs in a daemon thread with a bounded wait, so a
    stuck resolver can neither hang the caller nor spawn unbounded threads.
    Raises NetworkPolicyError when any returned address is non-public
    (mixed public/private answers are refused wholesale), on empty or
    excessive answers, or when the deadline is exceeded.
    """
    result = queue.Queue(maxsize=1)

    def lookup():
        try:
            result.put((True, socket.getaddrinfo(
                host, port, type=socket.SOCK_STREAM)))
        except Exception as exc:  # noqa: BLE001 - DNS failure is a refusal
            result.put((False, exc))
        finally:
            _dns_slots.release()

    if not _dns_slots.acquire(blocking=False):
        raise NetworkPolicyError("DNS resolver capacity exhausted")
    threading.Thread(target=lookup, daemon=True, name="keel-dns").start()
    try:
        ok, records = result.get(timeout=min(DNS_TIMEOUT_S, timeout))
    except queue.Empty as exc:
        raise NetworkPolicyError("DNS deadline exceeded") from exc
    if not ok:
        raise urllib.error.URLError(type(records).__name__) from records
    if not records or len(records) > 64:
        raise NetworkPolicyError("empty or excessive DNS answer")
    for family, _kind, _proto, _canon, address in records:
        if (family not in (socket.AF_INET, socket.AF_INET6)
                or not _public(address[0])):
            raise NetworkPolicyError(
                "DNS answer includes a non-public destination")
    return records


def _proxied(url):
    """True when a proxy applies to this URL's scheme.

    The sandbox's DNS shim answers every external hostname with a
    non-public address (198.18.33.x) while real egress goes through an
    HTTP(S) proxy (urllib/aiohttp honor proxy env vars). In that case the
    local DNS answer is meaningless as a destination classifier — the
    proxy is the actual egress — so the DNS pre-flight must be skipped.
    Direct-connect environments keep the full DNS admission.

    NO_PROXY/no_proxy exemptions are honored: a URL whose host is on the
    no-proxy list is treated as unproxied and still gets the DNS check.
    """
    try:
        proxies = urllib.request.getproxies()
    except Exception:  # noqa: BLE001 - safest fallback is no proxy
        return False
    if not proxies:
        return False
    scheme = (urllib.parse.urlsplit(url).scheme or "").lower()
    proxy = proxies.get(scheme) or proxies.get("all")
    if not proxy:
        return False
    # Honor NO_PROXY exemptions for this host.
    try:
        from urllib.request import proxy_bypass
        if proxy_bypass(hostname_of(url)):
            return False
    except Exception:  # noqa: BLE001 - fail closed: keep the DNS admission
        return False
    return True


def admit(url, dns_timeout=DNS_TIMEOUT_S):
    """Full pre-request admission: validate_url + (usually) resolve_public.

    validate_url ALWAYS runs: it still refuses localhost / .local /
    .internal hosts, userinfo, non-default ports, and credential-like
    query params. resolve_public is SKIPPED when a proxy applies to the
    URL's scheme (see _proxied): local DNS answers do not describe the
    proxy's egress, and refusing them would break every proxied fetch.

    Returns the normalized URL to request. Raises NetworkPolicyError on
    any policy violation (callers treat as a fetch failure -> ambiguous).
    """
    clean = validate_url(url)
    if _proxied(clean):
        return clean
    parsed = urllib.parse.urlsplit(clean)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    resolve_public(parsed.hostname, port, timeout=dns_timeout)
    return clean


def read_capped(resp, limit=MAX_BODY_BYTES):
    """Read a urllib-style response body, hard-capped at `limit` bytes.

    Refuses a lying/oversize Content-Length before reading a byte, then
    streams in chunks so an unbounded body can never exhaust memory.
    Raises NetworkPolicyError when the body exceeds the cap.

    Note: urllib never sends Accept-Encoding, so responses arrive as
    identity here and the Content-Length pre-check is valid. (The aiohttp
    path negotiates compression, so it caps decompressed bytes only.)
    """
    length = resp.getheader("Content-Length") if hasattr(
        resp, "getheader") else None
    if length is not None:
        try:
            declared = int(str(length).strip())
        except (TypeError, ValueError):
            raise NetworkPolicyError("malformed Content-Length")
        if declared < 0 or declared > limit:
            raise NetworkPolicyError("response exceeds size limit")
    chunks, size = [], 0
    while True:
        chunk = resp.read(min(65536, limit + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > limit:
            raise NetworkPolicyError("response exceeds size limit")
    return b"".join(chunks)
