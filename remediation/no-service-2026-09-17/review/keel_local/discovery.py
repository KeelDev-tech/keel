"""Read-only Greenhouse public discovery. No employer API keys, no submissions.

All discoveries are STAGED. Posting text is untrusted input and never changes
permissions, personal facts, or consent. Browser/form verification still follows.
"""
from __future__ import annotations
from html.parser import HTMLParser
import http.client
import ipaddress
import re
import socket
import ssl
import time
from urllib.parse import urlsplit
from .contracts import ContractError, strict_json, text, public_url_shape, number, integer

HOST = "boards-api.greenhouse.io"


class FetchRefused(RuntimeError):
    pass


def public_addresses(host, resolver=socket.getaddrinfo):
    addresses = resolver(host, 443, type=socket.SOCK_STREAM)
    if not addresses:
        raise FetchRefused("DNS_EMPTY")
    result = []
    for family, kind, protocol, _, sockaddr in addresses:
        ip = ipaddress.ip_address(sockaddr[0])
        if not ip.is_global or ip.is_multicast:
            raise FetchRefused("DNS_NON_PUBLIC")
        result.append((family, kind, protocol, sockaddr))
    return result


def allowed_discovery_url(url):
    if not public_url_shape(url):
        raise FetchRefused("URL_INVALID")
    p = urlsplit(url)
    if (p.hostname != HOST or p.fragment or p.query != "content=true" or
            not re.fullmatch(r"/v1/boards/[a-zA-Z0-9_-]+/jobs", p.path)):
        raise FetchRefused("URL_NOT_ALLOWLISTED")
    return p


def fetch_public_board(url, *, timeout=10, maximum_bytes=4 * 1024 * 1024):
    """Pins validated DNS result to the TLS socket; no proxy, cookie or redirect.

    Socket timeouts bound stalled I/O, not OS DNS resolution wall time. Run this
    in the existing supervised worker for a hard process-level deadline.
    """
    parsed = allowed_discovery_url(url)
    number(timeout, minimum=0.1, maximum=30); integer(maximum_bytes, minimum=1)
    addresses = public_addresses(parsed.hostname)
    family, kind, protocol, sockaddr = addresses[0]
    raw = socket.socket(family, kind, protocol)
    raw.settimeout(timeout)
    connection = http.client.HTTPSConnection(parsed.hostname, timeout=timeout)
    try:
        raw.connect(sockaddr)
        _ctx = ssl.create_default_context(); _ctx.minimum_version = ssl.TLSVersion.TLSv1_2; connection.sock = _ctx.wrap_socket(raw, server_hostname=parsed.hostname)
        connection.request("GET", parsed.path + "?" + parsed.query,
                           headers={"Accept": "application/json", "Accept-Encoding": "identity",
                                    "User-Agent": "KeelLocalDiscovery/0.1"})
        response = connection.getresponse()
        if response.status == 429:
            raise FetchRefused("RATE_LIMITED")
        if response.status != 200:
            raise FetchRefused("HTTP_STATUS_" + str(response.status))
        if response.getheader("Content-Encoding", "identity").lower() != "identity":
            raise FetchRefused("UNSUPPORTED_ENCODING")
        if response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise FetchRefused("NOT_JSON")
        body = response.read(maximum_bytes + 1)
        if len(body) > maximum_bytes:
            raise FetchRefused("BODY_TOO_LARGE")
        return strict_json(body)
    finally:
        connection.close()
        raw.close()


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0
    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "br", "li", "div", "h1", "h2", "h3"}:
            self.parts.append("\n")
    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "li", "div"}:
            self.parts.append("\n")
    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def posting_text(html):
    parser = _Text(); parser.feed(html)
    return "\n".join(" ".join(line.split()) for line in "".join(parser.parts).splitlines() if line.strip())


def parse_greenhouse(board, payload, *, observed_at, known_posting_ids=()):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", text(board)):
        raise ContractError("invalid board token")
    if type(payload) is not dict or type(payload.get("jobs")) is not list:
        raise ContractError("invalid Greenhouse jobs response")
    known = {str(x) for x in known_posting_ids}
    staged, excluded = [], []
    for row in payload["jobs"]:
        if type(row) is not dict or type(row.get("id")) is not int or row["id"] <= 0:
            excluded.append("invalid_job_identity"); continue
        jid = str(row["id"])
        if jid in known:
            excluded.append("duplicate"); continue
        if not public_url_shape(row.get("absolute_url")) or type(row.get("content")) is not str:
            excluded.append("posting_url_or_text_missing"); continue
        if not isinstance(row.get("title"), str) or not row["title"].strip():
            excluded.append("title_missing"); continue
        known.add(jid)
        staged.append({"schema_version": 1, "provider": "greenhouse", "employer_id": board,
                       "posting_id": jid, "posting_url": row["absolute_url"], "title": row["title"],
                       "posting_text": posting_text(row["content"]), "source_observed_at": observed_at,
                       "status": "STAGED", "trusted_instructions": False})
    return {"staged": staged, "excluded": excluded, "fetched_count": len(payload["jobs"])}


def posting_signals(content):
    """Conservative review hints, never an eligibility PASS.

    Exact passages are returned for human/policy review. Negation and alternative
    qualifications are context-sensitive, so a regex hit cannot alone reject a job.
    """
    text(content)
    patterns = {
        "office_days": r"(?:[1-7]|one|two|three|four|five|six|seven)\s+days?\s+(?:per\s+|a\s+)?week|full[- ]time\s+(?:on[- ]?site|in[- ]office)",
        "travel": r"(?:travel[^.\n]{0,50}\d{1,3}\s*%|\d{1,3}\s*%[^.\n]{0,50}travel)",
        "degree": r"(?:bachelor[’']?s?|master[’']?s?|doctorate|ph\.?d)\b[^.\n]{0,100}",
        "unaided": r"(?:without\s+(?:using\s+)?AI|no[- ]AI|unassisted|unaided)\b[^.\n]{0,100}",
    }
    hints = [{"kind": kind, "passage": m.group(0)} for kind, pattern in patterns.items()
             for m in re.finditer(pattern, content, flags=re.I)]
    return {"eligibility": "REQUIRES_POLICY_REVIEW", "signals": hints, "model_used": False}
