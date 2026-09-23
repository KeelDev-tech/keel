# Keel honesty-gates demo

`honesty_gates_demo.py` runs the real repo engines against synthetic
fixture input (fictional applicant "Alex Candidate") — three scenes from
the honest-automation contract:

1. **Truthfulness gate** — `engines/prescreen.py`: a question with no
   banked answer is UNMAPPED ("no banked answer for this question" —
   the gap is reported), never invented.
2. **Fail closed** — `engines/prescreen.py`: a posting demanding 5 days/week
   on-site (policy cap: 3) gets a PARK verdict and is moved to the parked
   queue.
3. **Explicit confirmation** — the production submit-intent module is
   intentionally private (submission techniques stay private per SPLIT.md),
   so this scene implements the same rule on a demo-local scratch ledger:
   intent recorded pre-attempt; an ambiguous outcome stays UNRESOLVED and
   bars retries; closing SUBMITTED with empty confirmation is refused;
   the count increments only on explicit confirmation text.

Fully offline; all state lives in a temp scratch dir that is cleaned up.
No network, no real queues, no private pipeline paths.

```bash
python3 demo/honesty_gates_demo.py        # run it (KEEL_DEMO_PAUSE=0 for no pauses)
```

`render_demo_gif.py` replays a `script`-captured typescript of that run
into `docs/assets/honesty-gates.gif` (PIL frames + ffmpeg palette
encoding). The session is genuine — the renderer only draws what the
demo printed:

```bash
COLUMNS=80 LINES=24 TERM=xterm-256color KEEL_DEMO_PAUSE=0.9 \
  script -qec "python3 demo/honesty_gates_demo.py" /tmp/honesty-demo.typescript
python3 demo/render_demo_gif.py
```
