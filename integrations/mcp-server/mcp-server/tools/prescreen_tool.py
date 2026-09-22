"""Prescreen tool: dry-run of Keel's application-packet gate screen.

screen_packet is the guardrail that keeps Keel honest: it scans a launch
packet's form intel for office/relocation/travel commitments, essays,
attestations, and required questions unmappable to the answer bank — and
PARKs the packet for a human instead of inventing answers.

This tool runs the real screen_packet against the EXAMPLE answer bank
(synthetic fixtures). Pure function: no queues are read or written.
"""
from __future__ import annotations

import keel_bridge


def keel_prescreen_packet(packet_brief: str, company: str = "") -> dict:
    """Screen an application packet's form intel for human-gated blockers.

    Args:
        packet_brief: the packet's brief text including its FORM INTEL section.
            The production extractor reads the section between the "FORM INTEL"
            header and the "STEP 3" marker, with one question per line like:
            - [text] Why do you want to work here?*
            Required questions end with "*". A brief without that shape
            carries no intel and screens CLEAN (no intel = nothing to flag).
        company: employer name, used for employer-specific blocker patterns.
    Returns {"verdict": "CLEAN"|"PARK", "reasons": [...]}. PARK means a human
    must answer before any application proceeds — the packet is never faked.
    """
    bank = keel_bridge.load_fixture("answer_bank.example.json")
    packet = {"brief": packet_brief or "", "company": company or ""}
    # Explicit empty patterns: the production employer-pattern file lives in
    # Trent's private pipeline tree and is never loaded here.
    result = keel_bridge.prescreen_mod.screen_packet(packet, bank, employer_patterns={})
    result["answer_bank"] = "example-fixture"
    return result
