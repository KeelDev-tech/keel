"""Read-only observations; no scheduler, queue writer, or browser-lane action."""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime, timezone
from .contracts import keys, version, require, integer, identifier, text, digest


def timestamp(value: str) -> datetime:
    text(value,"timestamp",80)
    try: dt=datetime.fromisoformat(value.replace("Z","+00:00"))
    except ValueError as exc: raise ValueError("invalid timestamp") from exc
    require(dt.utcoffset() is not None,"timestamp offset required")
    return dt


def supply_health(observation: dict, *, evaluated_at: str, maximum_age_seconds: int=300) -> dict:
    keys(observation,{"schema_version","ready","actionable","observed_at"});version(observation)
    integer(maximum_age_seconds,maximum=3600)
    age=(timestamp(evaluated_at)-timestamp(observation["observed_at"])).total_seconds()
    for key in ("ready","actionable"):
        if observation[key] is not None:integer(observation[key],key)
    if age<0 or age>maximum_age_seconds or None in (observation["ready"],observation["actionable"]):
        status="UNVERIFIED"
    elif observation["actionable"]==0:
        status="BUFFER_PRESENT_SUPPLY_STARVED" if observation["ready"]>=5 else "BUFFER_LOW_SUPPLY_STARVED"
    else:
        status="BUFFER_AND_SUPPLY_PRESENT" if observation["ready"]>=5 else "BUFFER_LOW_SUPPLY_PRESENT"
    return {"status":status,"ready":observation["ready"],"actionable":observation["actionable"],
            "age_seconds":age,"ready_floor":5,"source":"CALLER_PROVIDED_OBSERVATION",
            "whole_system_health":"NOT_ESTABLISHED","suggested_action":"REPORT_ONLY",
            "browser_action":"NONE","queue_writes":0,"scheduler_changes":0}


STATES={"PREPARED","AUTHORIZED","QUEUED","IN_FLIGHT","UNKNOWN","COMPLETED","HELD"}

def conformance(events: list[dict]) -> dict:
    require(type(events) is list and len(events)<=10000,"bounded normalized event list required")
    byid={};attempts=defaultdict(list);duplicates=0
    for row in events:
        keys(row,{"schema_version","event_id","attempt_id","sequence","state","evidence_revision","observed_at"})
        version(row);identifier(row["event_id"]);identifier(row["attempt_id"]);integer(row["sequence"])
        require(type(row["state"]) is str and row["state"] in STATES,"unmapped event state")
        text(row["evidence_revision"],"evidence_revision",200);timestamp(row["observed_at"])
        identity=row["event_id"]
        if identity in byid:
            require(digest(row)==digest(byid[identity]),"conflicting duplicate event_id")
            duplicates+=1;continue
        byid[identity]=row;attempts[row["attempt_id"]].append(row)
    findings=[];gaps=[]
    for attempt,rows in sorted(attempts.items()):
        rows.sort(key=lambda x:x["sequence"])
        require(len({r["sequence"] for r in rows})==len(rows),"conflicting attempt sequence")
        held=defaultdict(list)
        for row in rows:
            if row["state"]=="HELD":held[row["evidence_revision"]].append(row["event_id"])
        for rev,ids in held.items():
            if len(ids)>=3:
                findings.append({"attempt_id":attempt,"kind":"REPEATED_HOLD_WITH_UNCHANGED_EVIDENCE", "event_ids":ids})
        for left,right in zip(rows,rows[1:]):
            pair=[left["event_id"],right["event_id"]]
            if right["sequence"]!=left["sequence"]+1:gaps.append({"attempt_id":attempt,"event_ids":pair})
            if timestamp(right["observed_at"])<timestamp(left["observed_at"]):
                findings.append({"attempt_id":attempt,"kind":"NONMONOTONIC_OBSERVATION_TIME","event_ids":pair})
            if left["state"]=="UNKNOWN" and right["state"] in {"QUEUED","IN_FLIGHT"}:
                findings.append({"attempt_id":attempt,"kind":"RETRY_AFTER_UNKNOWN_REQUIRES_CANONICAL_RECONCILIATION","event_ids":pair})
            if left["state"]=="COMPLETED" and right["state"] in {"QUEUED","IN_FLIGHT"}:
                findings.append({"attempt_id":attempt,"kind":"WORK_AFTER_COMPLETED_REQUIRES_REVIEW","event_ids":pair})
    return {"status":"FINDINGS" if findings else ("NO_FINDINGS_IN_EXPORT" if events else "NO_DATA"),
            "observed_attempts":len(attempts),"unique_events":len(byid),"duplicate_rows_ignored":duplicates,
            "sequence_gaps":gaps,"findings":findings,"coverage":"CALLER_EXPORT_ONLY_NOT_FULL_HISTORY",
            "provider_acceptance_verified":False,"queue_writes":0}
