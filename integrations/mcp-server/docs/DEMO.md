# Demo video shot list (< 3:00, per official rules)

Target: show the MCP server functioning over Streamable HTTP, then the tools
doing real pipeline work on sample data. No real personal data on screen.

1. **(0:00–0:20) The problem.** "Applying for jobs means repeating the same
   research hundreds of times. Keel automates the tedious parts — and refuses
   to invent qualifications."
2. **(0:20–0:45) Boot.** Terminal: `python mcp-server/server.py --port 8765`.
   Show the startup line with the data_dir. One sentence: self-hosted MCP,
   spec 2025-11-25+.
3. **(0:45–1:30) Tools live.** MCP client (e.g. inspector or curl): `tools/list`
   → call `keel_search_roles` → `keel_verify_posting` → `keel_score_role`.
   Narrate what each stage does.
4. **(1:30–2:15) The agentic bit.** Call `keel_run_pipeline` once: discover →
   verify → score → prescreen, chained, with the prescreen parking a bad-fit
   role instead of faking it. This is the "refuses to lie" moment.
5. **(2:15–2:45) Safety.** Show a refused path: point `--data-dir` at the live
   pipeline tree → server refuses. "The public demo can never touch real data."
6. **(2:45–3:00) Close.** Repo URL on screen, Apache-2.0, "built during the
   hackathon window, with tests."

Recording: terminal + voiceover, no face needed. Upload public to YouTube,
link on the Devpost submission form.
