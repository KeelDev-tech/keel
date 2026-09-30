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


def keel_prescreen_packet(packet_brief: str, company: str = "",
                          packet_evidence: dict | None = None) -> dict:
    """Screen an application packet's form intel for human-gated blockers.

    Args:
        packet_brief: the packet's brief text including its FORM INTEL section.
            The production extractor reads the section between the "FORM INTEL"
            header and the "STEP 3" marker, with one question per line like:
            - [text] Why do you want to work here?*
            Required questions end with "*". A brief without that shape
            carries no intel. This brief-only diagnostic cannot establish full
            form/posting coverage and returns PARK for missing source evidence.
        company: employer name, used for employer-specific blocker patterns.
        packet_evidence: optional structured coverage fields: ats_url, form_intel,
            form_intel_complete, posting_text, posting_text_complete,
            posting_text_source, posting_text_url. Completeness must be explicit;
            the production gate checks shapes, bounds and exact URL binding.
            Caller-supplied evidence is not authenticated by this diagnostic.
            Missing form_intel.source_url binds to ats_url; explicit bindings
            are preserved and checked by the production gate.
    Returns {"verdict": "CLEAN"|"PARK", "reasons": [...]}. PARK means a human
    must review a genuine blocker; missing evidence instead requires verification
    recovery. This diagnostic never authorizes preparation or submission.
    """
    bank = keel_bridge.load_fixture("answer_bank.example.json")
    packet = {"brief": packet_brief or "", "company": company or ""}
    if packet_evidence is not None:
        if not isinstance(packet_evidence, dict):
            return {"verdict": "PARK", "reasons": ["Malformed packet evidence; verification required."],
                    "answer_bank": "example-fixture", "execution_authorized": False}
        # Do not accept file paths, role IDs, derived answers or caller overrides
        # of the fixture identity. Those can activate production write-back hooks.
        fields = ("ats_url", "form_intel", "form_intel_complete", "posting_text",
                  "posting_text_complete", "posting_text_source", "posting_text_url")
        packet.update({key: packet_evidence[key] for key in fields if key in packet_evidence})
        # Match screen_entry_prepromotion without mutating caller-owned evidence.
        if isinstance(packet.get("form_intel"), dict):
            packet["form_intel"] = dict(packet["form_intel"])
            packet["form_intel"].setdefault("source_url", packet.get("ats_url"))
    # Explicit empty patterns: the production employer-pattern file lives in
    # the operator's private pipeline tree and is never loaded here.
    result = keel_bridge.prescreen_mod.screen_packet(packet, bank, employer_patterns={})
    result["answer_bank"] = "example-fixture"
    result["execution_authorized"] = False
    return result
