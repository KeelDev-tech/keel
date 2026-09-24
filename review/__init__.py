"""Keel human-review workstream (Workstream F).

review.packet   -- immutable review packets built from validated six-family
                   canary exports (Keel 0.8 capture schema).
review.decision -- genuine human decision capture. Mechanism only: no genuine
                   decision exists yet, and this package never manufactures one.

Hard rules (Trent-authorized 2026-09-18):
  - No approval request is ever issued on incomplete evidence.
  - Source completeness is NEVER execution authorization.
  - Keel never infers approval from enthusiasm, synthetic tests, successful
    validation, or absence of objections. The genuine decision belongs to
    Trent only.
"""
