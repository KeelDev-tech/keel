"""Durable, scoped binary-failure sentry; a no-alarm report proves no absence.

Host-only measurements and a host-selected frozen baseline are required. Hashes
bind provenance and versions; they are not backend or provider authentication.
"""
from contextlib import contextmanager
import json
import math
import time

from .common import MachineError, PrivateDB, require, clone, digest, ident, sha, canonical

_BINDINGS = {"source_sha256", "config_sha256", "model_sha256", "calibration_sha256"}


def _bindings(value):
    require(type(value) is dict and set(value) == _BINDINGS, "sentry_bindings_required")
    for item in value.values():
        sha(item)
    return clone(value)


def _number(value, lower, upper, code):
    require(type(value) in (int,float) and lower <= value <= upper and math.isfinite(value), code)
    return value


def _baseline(value):
    value = clone(value)
    require(type(value) is dict and set(value) == {"schema", "baseline_id", "bindings", "sample_count", "failures", "tolerance", "delta", "min_samples", "max_samples", "evidence_sha256"}, "sentry_baseline_fields")
    require(value["schema"] == "keel.machine.failure-baseline.v1", "sentry_baseline_schema")
    ident(value["baseline_id"]); sha(value["evidence_sha256"])
    _bindings(value["bindings"])
    n,f=value["sample_count"],value["failures"]
    require(type(n) is int and type(f) is int and 1 <= n <= 1000000000 and 0 <= f <= n, "sentry_baseline_counts")
    _number(value["tolerance"],0,1,"sentry_tolerance_invalid")
    _number(value["delta"],1e-12,.25,"sentry_delta_invalid")
    minimum,maximum=value["min_samples"],value["max_samples"]
    require(type(minimum) is int and type(maximum) is int and 1 <= minimum <= maximum <= 100000,"sentry_sample_limits")
    return value


def failure_bounds(*, baseline_count, baseline_failures, sample_count, failures, delta):
    """One-sided baseline upper and stream lower, with one total alpha budget.

    P[baseline mean > its upper] <= delta/2. For stream look n, allocate
    delta/[2*n*(n+1)] to its lower endpoint. Their union controls any false
    increase declaration over all cumulative looks at <= delta. Live target:
    average conditional failure probability, not a guaranteed future rate.
    """
    require(type(baseline_count) is int and 1 <= baseline_count <= 1000000000
            and type(baseline_failures) is int and 0 <= baseline_failures <= baseline_count,
            "sentry_baseline_counts")
    require(type(sample_count) is int and 0 <= sample_count <= 100000
            and type(failures) is int and 0 <= failures <= sample_count, "sentry_live_counts")
    _number(delta,1e-12,.25,"sentry_delta_invalid")
    baseline_rate=baseline_failures/baseline_count
    baseline_radius=math.sqrt(math.log(2/delta)/(2*baseline_count))
    baseline_upper=min(1.0,math.nextafter(baseline_rate+baseline_radius,math.inf))
    if sample_count == 0:
        return {"baseline_rate":baseline_rate,"baseline_upper":baseline_upper,
                "failure_rate":None,"failure_lower":None,"increase_lower":None}
    live_rate=failures/sample_count
    live_radius=math.sqrt(math.log(2*sample_count*(sample_count+1)/delta)/(2*sample_count))
    live_lower=max(0.0,math.nextafter(live_rate-live_radius,-math.inf))
    difference=math.nextafter(live_lower-baseline_upper,-math.inf)
    return {"baseline_rate":baseline_rate,"baseline_upper":baseline_upper,
            "failure_rate":live_rate,"failure_lower":live_lower,"increase_lower":difference}


class FailureMonitor:
    """Append-only cumulative measurement window against one immutable baseline.

    This object belongs to trusted host instrumentation, never a worker. There
    is no arbitrary event importer, automatic route edit, reset, or baseline
    refresh. Clearing a confirmed alert needs an explicit new host deployment/
    baseline decision; separate monitors require a host-wide alpha budget.
    """
    def __init__(self,path,*,baseline,clock=time.time):
        self.baseline=_baseline(baseline)
        self.baseline_sha256=digest(self.baseline)
        require(callable(clock),"sentry_clock_required")
        self.clock=clock
        self.storage=PrivateDB(path)
        with self.storage.transaction() as db:
            now=self._now()
            db.execute("CREATE TABLE IF NOT EXISTS failure_baseline(id INTEGER PRIMARY KEY CHECK(id=1),body BLOB NOT NULL,pin TEXT NOT NULL,created REAL NOT NULL,last_time REAL NOT NULL,observations INTEGER NOT NULL,failures INTEGER NOT NULL,alarm_sequence INTEGER)")
            db.execute("CREATE TABLE IF NOT EXISTS failure_samples(sequence INTEGER PRIMARY KEY,sample_id TEXT UNIQUE NOT NULL,failed INTEGER NOT NULL,bindings BLOB NOT NULL,provenance TEXT UNIQUE NOT NULL,observed REAL NOT NULL)")
            row=db.execute("SELECT * FROM failure_baseline WHERE id=1").fetchone()
            if row is None:
                require(db.execute("SELECT COUNT(*) FROM failure_samples").fetchone()[0] == 0,"sentry_orphaned_samples")
                db.execute("INSERT INTO failure_baseline VALUES(1,?,?,?,?,0,0,NULL)",
                    (canonical(self.baseline),self.baseline_sha256,now,now))
            else:
                require(row["pin"]==self.baseline_sha256 and digest(json.loads(row["body"]))==self.baseline_sha256,"sentry_frozen_baseline_changed")
                require(now >= row["last_time"],"sentry_clock_regressed")
            end=self._now()
            require(end >= now,"sentry_clock_regressed")
            db.execute("UPDATE failure_baseline SET last_time=? WHERE id=1",(end,))

    def _now(self):
        return _number(self.clock(),0,2**53,"sentry_clock_invalid")

    def _state(self,db):
        state=db.execute("SELECT * FROM failure_baseline WHERE id=1").fetchone()
        require(digest(self.baseline)==self.baseline_sha256,"sentry_frozen_baseline_changed")
        require(state is not None and state["pin"]==self.baseline_sha256
                and digest(json.loads(state["body"]))==self.baseline_sha256,"sentry_frozen_baseline_changed")
        require(0 <= state["failures"] <= state["observations"] <= self.baseline["max_samples"],"sentry_state_invalid")
        return state

    @contextmanager
    def _transaction(self):
        # Observe time after SQLite serialization, including read-only reports
        # and idempotent replays. A later rollback cannot hide behind an older
        # last-ingested-sample timestamp.
        with self.storage.transaction() as db:
            state=self._state(db)
            now=self._now()
            regressed=now < state["last_time"]
            if not regressed:
                db.execute("UPDATE failure_baseline SET last_time=? WHERE id=1",(now,))
            yield db,state,now
            if not regressed:
                end=self._now()
                require(end >= now,"sentry_clock_regressed")
                db.execute("UPDATE failure_baseline SET last_time=? WHERE id=1",(end,))

    def _result(self,state,current_bindings,now):
        bounds=failure_bounds(baseline_count=self.baseline["sample_count"],baseline_failures=self.baseline["failures"],
            sample_count=state["observations"],failures=state["failures"],delta=self.baseline["delta"])
        holds=[]
        if current_bindings != self.baseline["bindings"]:
            holds.append("BINDINGS_CHANGED")
        if now < state["last_time"]:
            holds.append("CLOCK_REGRESSED")
        if state["alarm_sequence"] is not None:
            holds.append("FAILURE_RATE_INCREASE_CONFIRMED")
        if state["observations"] >= self.baseline["max_samples"]:
            holds.append("MEASUREMENT_CAPACITY_REACHED")
        status="HOLD" if holds else ("INSUFFICIENT_DATA" if state["observations"] < self.baseline["min_samples"] else "NO_ALARM")
        return clone({"schema":"keel.machine.failure-report.v1","baseline_id":self.baseline["baseline_id"],
            "baseline_sha256":self.baseline_sha256,"bindings":self.baseline["bindings"],
            "status":status,"hold_reasons":holds,"sample_count":state["observations"],"failures":state["failures"],
            "window_sequence":state["observations"],"alarm_sequence":state["alarm_sequence"],
            "observed_at":now,"tolerance":self.baseline["tolerance"],"delta":self.baseline["delta"],
            "method":"two_sample_one_sided_hoeffding_time_union","bounds":bounds,
            "estimand":"running_average_conditional_failure_increase_against_fixed_baseline_population",
            "no_drift_proven":False,"provenance_authenticated_here":False,
            "execution_authorized":False,"route_writes":0})

    def record(self,sample_id,failed,*,current_bindings,provenance_sha256):
        """Record one actual host-observed outcome, never a prediction of failure.

        Exact same-ID replay is idempotent. Conflicting or aliased observations
        reject the transaction. Changed bindings return HOLD without ingestion.
        """
        ident(sample_id);sha(provenance_sha256)
        require(type(failed) is bool,"sentry_boolean_outcome_required")
        current_bindings=_bindings(current_bindings)
        with self._transaction() as (db,state,now):
            if current_bindings != self.baseline["bindings"]:
                return {**self._result(state,current_bindings,now),"recorded":False,"idempotent":False}
            require(now >= state["last_time"],"sentry_clock_regressed")
            prior=db.execute("SELECT * FROM failure_samples WHERE sample_id=?",(sample_id,)).fetchone()
            if prior is not None:
                require(bool(prior["failed"])==failed and prior["provenance"]==provenance_sha256
                        and json.loads(prior["bindings"])==current_bindings,"sentry_sample_conflict")
                return {**self._result(state,current_bindings,now),"recorded":False,"idempotent":True}
            require(db.execute("SELECT 1 FROM failure_samples WHERE provenance=?",(provenance_sha256,)).fetchone() is None,"sentry_repeated_observation")
            if state["observations"] >= self.baseline["max_samples"]:
                return {**self._result(state,current_bindings,now),"recorded":False,"idempotent":False}
            n,f=state["observations"]+1,state["failures"]+int(failed)
            limits=failure_bounds(baseline_count=self.baseline["sample_count"],baseline_failures=self.baseline["failures"],sample_count=n,failures=f,delta=self.baseline["delta"])
            alarm=state["alarm_sequence"]
            if alarm is None and n >= self.baseline["min_samples"] and limits["increase_lower"] > self.baseline["tolerance"]:
                alarm=n
            db.execute("INSERT INTO failure_samples VALUES(?,?,?,?,?,?)",(n,sample_id,int(failed),canonical(current_bindings),provenance_sha256,now))
            db.execute("UPDATE failure_baseline SET observations=?,failures=?,alarm_sequence=?,last_time=? WHERE id=1",(n,f,alarm,now))
            return {**self._result(self._state(db),current_bindings,now),"recorded":True,"idempotent":False}

    def report(self,*,current_bindings):
        current_bindings=_bindings(current_bindings)
        with self._transaction() as (db,state,now):
            return self._result(state,current_bindings,now)
