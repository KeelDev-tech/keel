"""Private HTML artifacts: inert data, pinned metrics and safe publication."""
import base64
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path

import pytest

from keel_loki.common import digest
from keel_muse.dashboard import demo, render_dashboard, write_dashboard
from keel_muse.review import ReviewError, example_snapshot, project_review


class Document(HTMLParser):
    def __init__(self, raw):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.blocks = []
        self.current = None
        self.feed(raw)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag in ("script", "style"):
            self.current = {"tag": tag, "attrs": dict(attrs), "text": ""}
            self.blocks.append(self.current)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.current = None

    def handle_data(self, data):
        if self.current is not None:
            self.current["text"] += data

    def data(self):
        return json.loads(next(b["text"] for b in self.blocks if b["attrs"].get("id") == "review-data"))


@pytest.fixture
def report():
    snapshot = example_snapshot()
    return project_review(snapshot, now=snapshot["captured_at"])


def test_untrusted_html_script_svg_and_reference_are_inert_text():
    attack = '</script><svg onload="globalThis.pwned=true"><script>evil()</script>&\u2028\u2029'
    snapshot = example_snapshot()
    app = snapshot["applications"][0]
    app["role"] = attack
    app["packet"]["fields"][0]["value"] = attack
    app["evidence"][0]["reference"] = 'javascript:evil("secret")'
    app["questions"][0]["prompt"] = attack
    raw = render_dashboard(project_review(snapshot, now=snapshot["captured_at"]))
    doc = Document(raw)
    assert attack not in raw
    assert "\\u003c/script\\u003e" in raw
    assert "\\u2028\\u2029" in raw
    assert not any(tag == "svg" for tag, _ in doc.tags)
    assert len([b for b in doc.blocks if b["tag"] == "script"]) == 2
    data = doc.data()
    assert data["applications"][0]["role"] == attack
    assert data["applications"][0]["questions"][0]["prompt"] == attack
    assert data["applications"][0]["evidence"][0]["reference"] == 'javascript:evil("secret")'
    for _, attrs in doc.tags:
        assert not any(name.startswith("on") for name in attrs)
        assert not any(value and value.startswith("javascript:") for value in attrs.values())


def test_csp_pins_exact_static_script_and_style_and_forbids_network(report):
    doc = Document(render_dashboard(report))
    csp = next(attrs["content"] for tag, attrs in doc.tags if tag == "meta" and
               attrs.get("http-equiv") == "Content-Security-Policy")
    for directive in ("default-src 'none'", "connect-src 'none'", "form-action 'none'",
                      "object-src 'none'", "frame-src 'none'", "img-src 'none'", "base-uri 'none'"):
        assert directive in csp
    for block in doc.blocks:
        if block["tag"] == "style" or block["tag"] == "script" and not block["attrs"]:
            pin = "sha256-" + base64.b64encode(hashlib.sha256(block["text"].encode()).digest()).decode()
            assert pin in csp
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp
    assert not any(tag in ("iframe", "object", "embed", "img", "link", "form") for tag, _ in doc.tags)
    assert not any(attrs.get("src") for _, attrs in doc.tags)


def test_embedded_projection_is_pinned_and_contains_no_claim_of_authority(report):
    data = Document(render_dashboard(report)).data()
    assert "snapshot" not in data
    assert data["report_sha256"] == digest(report)
    assert data["snapshot_sha256"] == report["snapshot_sha256"]
    assert data["execution_authorized"] is False
    assert data["human_identity_authenticated"] is False
    assert data["metrics"]["model_calls"]["value"] is None
    assert data["metrics"]["submissions"]["success_rate"] is None


def test_dynamic_ui_uses_text_nodes_and_download_only(report):
    doc = Document(render_dashboard(report))
    script = next(b["text"] for b in doc.blocks if b["tag"] == "script" and not b["attrs"])
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(",
                      "Function(", "fetch(", "XMLHttpRequest", "WebSocket", "sendBeacon", "localStorage",
                      "sessionStorage", "document.cookie", "window.open", "createElementNS"):
        assert forbidden not in script
    assert "textContent" in script
    assert "URL.createObjectURL" in script
    assert "requires_authenticated_host_ingestion:true" in script
    assert "execution_authorized:false" in script
    assert "scope_ids:q.kind==='fact'&&q.reuse_authorized?q.scope_ids:[app.application_id]" in script


def test_keyboard_tab_navigation_labels_live_updates_and_reduced_motion(report):
    raw = render_dashboard(report)
    doc = Document(raw)
    assert len([a for _, a in doc.tags if a.get("role") == "tab"]) == 4
    controls = {a.get("id"): a for t, a in doc.tags if t in ("input", "select")}
    labels = {a.get("for") for t, a in doc.tags if t == "label"}
    assert {"search", "status-filter"} <= labels
    assert {"search", "status-filter"} <= set(controls)
    assert any(a.get("role") == "status" and a.get("aria-live") == "polite" for _, a in doc.tags)
    for key in ("ArrowLeft", "ArrowRight", "Home", "End", "prefers-reduced-motion"):
        assert key in raw


@pytest.mark.parametrize("field,value", [("applications", 999), ("blocked", 0)])
def test_forged_metrics_cannot_be_rendered(report, field, value):
    report["metrics"][field] = value
    with pytest.raises(ReviewError, match="projection_changed"):
        render_dashboard(report)


def test_empty_queue_renders_valid_data_without_fake_metrics():
    snapshot = example_snapshot()
    snapshot.update(applications=[], events=[])
    doc = Document(render_dashboard(project_review(snapshot, now=snapshot["captured_at"])))
    assert doc.data()["applications"] == []
    assert doc.data()["metrics"]["model_latency_ms"]["p95"] is None


def test_html_publication_is_private_exact_and_refuses_overwrite(tmp_path, report):
    target = tmp_path / "desk.html"
    published = write_dashboard(target, report)
    assert target.stat().st_mode & 0o777 == 0o600
    assert published["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert published["bytes"] == len(target.read_bytes())
    assert published["network_requests"] == 0
    old = target.read_bytes()
    with pytest.raises(FileExistsError):
        write_dashboard(target, report)
    assert target.read_bytes() == old
    assert [p.name for p in tmp_path.iterdir()] == ["desk.html"]


def test_symlink_target_and_parent_cannot_redirect_publication(tmp_path, report):
    other = tmp_path / "other.html"
    other.write_text("keep")
    alias = tmp_path / "alias.html"
    alias.symlink_to(other)
    with pytest.raises(FileExistsError):
        write_dashboard(alias, report)
    assert other.read_text() == "keep"
    real = tmp_path / "actual"
    real.mkdir()
    parent_alias = tmp_path / "parent-alias"
    parent_alias.symlink_to(real, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        write_dashboard(parent_alias / "new.html", report)
    assert list(real.iterdir()) == []


def test_demo_is_synthetic_and_does_not_claim_browser_validation(tmp_path):
    summary = demo(tmp_path)
    assert summary["synthetic"] is True
    assert summary["browser_render_verified"] is False
    assert summary["execution_authorized"] is False
    assert summary["network_requests"] == 0
    assert Path(summary["dashboard"]["path"]).read_text().startswith("<!doctype html>")
    assert json.loads((tmp_path / "review-summary.json").read_text()) == summary
    assert (tmp_path / "review.html").stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        demo(tmp_path)


def test_demo_does_not_follow_a_preexisting_dangling_output_symlink(tmp_path):
    (tmp_path / "review-summary.json").symlink_to(tmp_path / "absent")
    with pytest.raises(FileExistsError):
        demo(tmp_path)
    assert not (tmp_path / "review.html").exists()
