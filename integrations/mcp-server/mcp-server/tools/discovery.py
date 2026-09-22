"""Discovery tools: search roles from the bundled sample fixtures.

Production Keel discovers roles with its sweep workers across job boards;
this tool exposes the same *stage* (query -> candidate postings) over the
shipped sample fixtures so the MCP surface is fully demoable without
credentials or API keys.
"""
from __future__ import annotations

import keel_bridge


def keel_search_roles(query: str, work_model: str = "", location: str = "",
                      limit: int = 10) -> dict:
    """Search discovered job postings by keyword, with optional filters.

    Searches the bundled sample fixtures (fixture=true in the response).
    Args:
        query: keyword(s), matched against title, company, and description.
        work_model: optional filter, e.g. "remote", "hybrid", "onsite".
        location: optional substring filter on the location field.
        limit: max results (1-50).
    """
    roles = keel_bridge.load_fixture("discovered_roles.example.json")
    q = (query or "").lower()
    hits = []
    for r in roles:
        hay = " ".join(str(r.get(k, "")) for k in ("title", "company", "description")).lower()
        if q and q not in hay:
            continue
        if work_model and work_model.lower() not in str(r.get("work_model", "")).lower():
            continue
        if location and location.lower() not in str(r.get("location", "")).lower():
            continue
        hits.append(r)
    limit = max(1, min(int(limit or 10), 50))
    return {
        "query": query,
        "filters": {"work_model": work_model, "location": location},
        "count": len(hits),
        "fixture": True,
        "note": ("Sample fixtures. Production discovery runs Keel's sweep workers; "
                 "this tool demonstrates the stage's input/output contract."),
        "roles": hits[:limit],
    }
