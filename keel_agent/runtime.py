"""Host-controlled composition of snapshots, durable jobs and local reviews.

Models never choose paths, tools, accounts, approvals, clocks or destinations.
A completed REVIEW job is not permission to act. All preparation rechecks the
latest imported snapshot, exact bundle and stored blind review.
"""
from base64 import b64decode, b64encode
from datetime import datetime, timedelta, timezone
from pathlib import Path

from keel_flow.common import digest, keys, require
from keel_workflow.delivery import Bundle, build_bundle, validate_bundle
from keel_workflow.reviews import ReviewStore
from .io import private_home, read_json
from .models import ReviewerConfig, reviewer_config_digest, run_blind_review, validate_roster_diversity
from .scope import prepare_candidate, preflight_candidate
from .state import LocalState


def utcnow():
    return datetime.now(timezone.utc)


def roster(config):
    keys(config, {"reviewers"})
    require(type(config["reviewers"]) is list, "reviewer list required")
    reviewers = [ReviewerConfig(**row) for row in config["reviewers"]]
    require(2 <= len(reviewers) <= 16 and len({r.reviewer_id for r in reviewers}) == len(reviewers),
            "two to sixteen distinct configured reviewers required")
    return reviewers


def config_binding(reviewers):
    return {row.reviewer_id: reviewer_config_digest(row) for row in reviewers}


def pack_bundle(bundle):
    validate_bundle(bundle)
    return {"canonical_json": bundle.canonical_json,
            "attachments": {name: b64encode(raw).decode("ascii") for name, raw in bundle.attachments}}


def unpack_bundle(value):
    keys(value, {"canonical_json", "attachments"})
    require(type(value["attachments"]) is dict, "attachment map required")
    bundle = Bundle(value["canonical_json"], tuple(sorted((name, b64decode(raw, validate=True))
                    for name, raw in value["attachments"].items())))
    validate_bundle(bundle)
    return bundle


def load_bundle(path):
    path = Path(path).absolute()
    data = read_json(path)
    keys(data, {"bundle"})
    config = data["bundle"]
    keys(config, {"workspace_id", "role_id", "action", "destination", "account_id", "revisions", "content", "attachments"})
    require(type(config["attachments"]) is dict, "attachment paths required")
    attachments = {}
    for name, relative in config["attachments"].items():
        require(type(relative) is str and not Path(relative).is_absolute(), "relative attachment path required")
        target = path.parent / relative
        require(target.resolve(strict=True).is_relative_to(path.parent.resolve(strict=True)), "attachment escapes input folder")
        require(target.absolute() == target.resolve(strict=True), "attachment symlinks forbidden")
        attachments[name] = target
    return build_bundle(**{k: v for k, v in config.items() if k != "attachments"}, attachments=attachments)


class LocalAgent:
    def __init__(self, home, workspace_id, *, clock=None):
        self.home = private_home(home)
        self.clock = clock or utcnow
        self.state = LocalState(self.home / "agent.sqlite3", workspace_id)
        self.reviews = ReviewStore(self.home / "reviews.sqlite3")

    def context(self):
        body = self.state.latest_snapshot(now=self.clock())["body"]
        require(body["assurance"] is not None and body["trust"] is not None,
                "assurance and trust exports required; missing evidence cannot be inferred")
        return {"flow_export": body["flow"], "assurance_export": body["assurance"], "trust_export": body["trust"]}

    def enqueue_review(self, bundle, config, *, idempotency_key, round_id, ttl_seconds=600):
        require(type(ttl_seconds) is int and 30 <= ttl_seconds <= 900, "review lifetime must be 30..900 seconds")
        reviewers = roster(config)
        context = self.context()
        validate_roster_diversity(reviewers, context["assurance_export"]["reviewers"])
        candidate = prepare_candidate(bundle, **context, now=self.clock(),
                                      reviewer_config_sha256=config_binding(reviewers))
        require(candidate["state"] == "READY_FOR_BLIND_REVIEW", "candidate blocked: " + ",".join(candidate["reasons"]))
        # No model call yet. The original anchor and byte bundle are persisted.
        payload = {"bundle": pack_bundle(bundle), "config": config, "candidate": candidate,
                   "round_id": round_id, "ttl_seconds": ttl_seconds}
        return self.state.enqueue("REVIEW", payload, idempotency_key=idempotency_key,
                                  material_hash=candidate["subject_sha256"], now=self.clock())

    def worker_once(self, worker_id, *, transport=None):
        # Upper bound exceeds the maximum round duration; no automatic retries of
        # a partially written review round. Duplicate round IDs fail closed.
        job = self.state.claim(worker_id, now=self.clock(), lease_seconds=960, kinds={"REVIEW"})
        if job is None:
            return {"state": "IDLE", "execution_authorized": False}
        try:
            payload = job["payload"]
            bundle = unpack_bundle(payload["bundle"])
            reviewers = roster(payload["config"])
            context = self.context()
            validate_roster_diversity(reviewers, context["assurance_export"]["reviewers"])
            fresh = prepare_candidate(bundle, **context, now=self.clock(),
                lead_anchor=payload["candidate"]["lead_anchor"], reviewer_config_sha256=config_binding(reviewers))
            require(fresh["state"] == "READY_FOR_BLIND_REVIEW" and fresh["subject_sha256"] == job["material_hash"],
                    "candidate changed or blocked before review")
            result = run_blind_review(self.reviews, round_id=payload["round_id"], subject=fresh["subject"],
                reviewers=reviewers, expires_at=self.clock()+timedelta(seconds=payload["ttl_seconds"]),
                clock=self.clock, transport=transport)
            # COMPLETED means processing completed, including a review HOLD.
            result["execution_authorized"] = False
            return self.state.complete(job["job_id"], job["token"], result, now=self.clock())
        except (ValueError, TypeError, KeyError, OSError) as exc:
            # Error classes are sufficient for public audit; no prompt or secret.
            return self.state.fail(job["job_id"], job["token"], type(exc).__name__ + ": review blocked", now=self.clock())

    def preflight(self, job_id):
        job = self.state.get_job(job_id)
        require(job["kind"] == "REVIEW" and job["state"] == "COMPLETED", "completed review job required")
        payload = job["payload"]
        bundle = unpack_bundle(payload["bundle"])
        result = preflight_candidate(bundle, **self.context(), now=self.clock(),
            lead_anchor=payload["candidate"]["lead_anchor"], reviews=self.reviews, round_id=payload["round_id"],
            reviewer_config_sha256=config_binding(roster(payload["config"])))
        require(result["candidate"]["subject_sha256"] == job["material_hash"], "job material hash changed")
        return result

    def approve_prepare(self, job_id, *, expires_at):
        result = self.preflight(job_id)
        require(result["state"] == "PREFLIGHT_CHECKS_PASSED", "preparation checks blocked")
        bundle = unpack_bundle(self.state.get_job(job_id)["payload"]["bundle"]).document
        return self.state.approve(bundle["role_id"], result["candidate"]["subject_sha256"], "PREPARE",
            bundle["destination"], bundle["account_id"], expires_at=expires_at, now=self.clock())

    def prepare_browser(self, job_id, contract, *, approval_id, adapter):
        from .browser import validate_contract
        contract = validate_contract(contract)
        require(contract["mode"] == "external_prepare", "agent preparation supports external prepare-only contracts")
        result = self.preflight(job_id)
        require(result["state"] == "PREFLIGHT_CHECKS_PASSED", "preparation checks blocked")
        job = self.state.get_job(job_id)
        bundle = unpack_bundle(job["payload"]["bundle"])
        document = bundle.document
        require(contract["url"] == document["destination"] and contract["account_id"] == document["account_id"],
                "browser destination/account differs from reviewed bundle")
        answers = {row["label"]: row["value"] for row in contract["fields"]}
        require(answers == document["content"]["application"]["answers"], "browser answers differ from reviewed bundle")
        attachment = contract.get("attachment")
        require(attachment is not None and len(bundle.attachments) == 1, "browser requires exactly one reviewed attachment")
        require((attachment["name"], b64decode(attachment["base64"], validate=True)) == bundle.attachments[0],
                "browser attachment differs from reviewed bytes")
        self.state.consume_approval(approval_id, {"role_id": document["role_id"], "material_hash": job["material_hash"],
            "action": "PREPARE", "destination": document["destination"], "account_id": document["account_id"]}, now=self.clock())
        observed = adapter.prepare(contract)
        return {"state": "PREPARED", "reviewed_bundle_sha256": bundle.sha256,
                "browser_evidence": observed, "execution_authorized": False, "external_submission": False}


def public_job(job):
    if "job_id" not in job:
        return job
    result = {key: job[key] for key in ("job_id", "kind", "state", "material_hash", "error")}
    result["execution_authorized"] = False
    if job.get("result"):
        result["review"] = job["result"].get("evaluation")
    return result
