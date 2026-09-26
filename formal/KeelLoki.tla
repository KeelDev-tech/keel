----------------------------- MODULE KeelLoki -----------------------------
EXTENDS Naturals, Integers, TLC
CONSTANT MaxFence
VARIABLE s
vars == <<s>>
Init == s = [revision |-> 0, approval |-> -1, fence |-> 0, owner |-> "",
             phase |-> "IDLE", start_revision |-> -1, start_approval |-> -1,
             start_fence |-> 0, finish_fence |-> 0, held |-> FALSE,
             ever_held |-> FALSE, rate_limited |-> FALSE,
             ever_rate_limited |-> FALSE, ever_unknown |-> FALSE,
             escalated |-> FALSE]
Unknown(x) == [x EXCEPT !.phase = "UNKNOWN", !.ever_unknown = TRUE, !.owner = ""]
Blocked == s.phase = "UNKNOWN" \/ s.held \/ s.rate_limited \/ s.approval # s.revision
           \/ (s.phase = "IDLE" /\ s.owner = "" /\ s.fence = MaxFence)
Approve == /\ s.phase = "IDLE" /\ s.approval # s.revision
           /\ s' = [s EXCEPT !.approval = s.revision]
Revoke == /\ s.approval # -1
          /\ LET next == [s EXCEPT !.approval = -1]
             IN s' = IF s.phase = "ACTIVE" THEN Unknown(next) ELSE next
Revise == /\ s.revision = 0
          /\ LET next == [s EXCEPT !.revision = 1]
             IN s' = IF s.phase = "ACTIVE" THEN Unknown(next) ELSE next
Claim(w) == /\ s.phase = "IDLE" /\ s.owner = "" /\ s.fence < MaxFence
            /\ s' = [s EXCEPT !.owner = w, !.fence = @ + 1]
Expire == /\ s.owner # ""
          /\ s' = IF s.phase = "ACTIVE" THEN Unknown(s) ELSE [s EXCEPT !.owner = ""]
Start == /\ s.phase = "IDLE" /\ s.owner # "" /\ ~s.held /\ ~s.rate_limited
         /\ s.approval = s.revision
         /\ s' = [s EXCEPT !.phase = "ACTIVE", !.start_revision = s.revision,
                          !.start_approval = s.approval, !.start_fence = s.fence]
Finish == /\ s.phase = "ACTIVE"
          /\ s' = [s EXCEPT !.phase = "DONE", !.finish_fence = s.fence, !.owner = ""]
Crash == /\ s.phase = "ACTIVE" /\ s' = Unknown(s)
Hold == /\ ~s.held
        /\ LET next == [s EXCEPT !.held = TRUE, !.ever_held = TRUE]
           IN s' = IF s.phase = "ACTIVE" THEN Unknown(next) ELSE next
Rate429 == /\ ~s.rate_limited
           /\ LET next == [s EXCEPT !.rate_limited = TRUE, !.ever_rate_limited = TRUE]
              IN s' = IF s.phase = "ACTIVE" THEN Unknown(next) ELSE next
Escalate == /\ s.phase # "DONE" /\ ~s.escalated /\ Blocked
            /\ s' = [s EXCEPT !.escalated = TRUE]
Next == Approve \/ Revoke \/ Revise \/ Claim("a") \/ Claim("b") \/ Expire
        \/ Start \/ Finish \/ Crash \/ Hold \/ Rate429 \/ Escalate
Safety == /\ (s.phase = "ACTIVE" => (s.owner # "" /\ s.approval = s.revision
             /\ s.start_approval = s.start_revision /\ s.start_revision = s.revision
             /\ s.start_fence = s.fence /\ ~s.held /\ ~s.rate_limited))
          /\ (s.phase \in {"ACTIVE", "DONE"} => s.start_approval = s.start_revision)
          /\ (s.phase = "DONE" => s.finish_fence = s.start_fence)
          /\ (s.ever_unknown => s.phase = "UNKNOWN")
          /\ (s.ever_rate_limited => s.rate_limited)
          /\ (s.ever_held => s.held)
Spec == Init /\ [][Next]_vars
=============================================================================
