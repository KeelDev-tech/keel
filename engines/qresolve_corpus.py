"""Bounded, read-only local evidence retrieval for question resolution.

Only the answer bank can supply candidate answers. Memory, queue notes, logs,
ledger labels and earlier tray drafts are research evidence, never authority.
The snapshot detects later changes; it is not a cross-file atomic snapshot or
protection against a privileged local writer. No files are created on import
or retrieval, and no network or model service is used.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
from datetime import date, datetime, timezone

import answer_resolver
import queue_io
from qresolve_semantics import canonical_fingerprint, fact_key

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_MEMORY_FILES = 60
MAX_ROWS = 100_000
MAX_RECORDS = 150_000
MAX_INDEX_POSTINGS = 2_000_000
MAX_LINE_BYTES = 64 * 1024
MAX_RESEARCH_HITS = 12
MAX_EXCERPT = 2000
_STOP = frozenset("the and that this with your you have what how many are for can will would of to in a an is do does about please years experience".split())
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_MEMORY_NAME = re.compile(r"\d{4}-\d{2}-\d{2}\.md\Z")
# These explicit denials reduce authority; they never establish who authored
# the answer. Keep this finite instead of guessing the meaning of free prose.
_OWN_WORDS_DENIAL_RE = re.compile(
    r"\b(?:not|never)\s+(?:in\s+)?(?:(?:the\s+)?"
    r"(?:applicant|operator|trent|my|your|his|her|their)(?:['\u2019]s)?\s+)?own words\b", re.I)


def _tokens(text):
    return frozenset(t for t in re.findall(r"[a-z0-9]{3,}", text.casefold())
                     if t not in _STOP)


def _pointer(value):
    return str(value).replace("~", "~0").replace("/", "~1")


def _open_parent(path):
    """Pin every directory with no-follow descriptors, including ancestors."""
    path = Path(os.path.abspath(path))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd, path.name
    except BaseException:
        os.close(fd)
        raise


def _read(path, limit=MAX_FILE_BYTES, *, bank=False):
    """Bounded descriptor read; reject symlink/FIFO/device sources."""
    parent, name = _open_parent(path)
    fd = None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=parent)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("unsafe_or_oversized_source")
        chunks, count = [], 0
        while True:
            chunk = os.read(fd, min(65536, limit + 1 - count))
            if not chunk:
                break
            chunks.append(chunk)
            count += len(chunk)
            if count > limit:
                raise ValueError("source_byte_limit")
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("source_changed_during_read")
        raw = b"".join(chunks)
        text = raw.decode("utf-8")
        parsed = None
        if bank:
            # Reuse the canonical loader through the pinned parent descriptor.
            # Unlike a /proc/self/fd/<file> symlink, the final component remains
            # the real file and the loader's own O_NOFOLLOW stays effective.
            fd_root = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
            parsed = answer_resolver.load_bank(f"{fd_root}/{parent}/{name}")
            if parsed != queue_io.strict_loads(text):
                raise ValueError("bank_changed_during_read")
        return text, hashlib.sha256(raw).hexdigest(), parsed, len(raw)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def _string_leaves(value, pointer="", depth=0):
    if depth > 30:
        raise ValueError("json_depth_limit")
    if isinstance(value, str):
        yield pointer, value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _string_leaves(child, pointer + "/" + _pointer(key), depth + 1)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _string_leaves(child, pointer + "/" + str(index), depth + 1)


def _value_spans(text):
    """Locate actual JSON token slices once; duplicate keys already rejected."""
    decoder = json.JSONDecoder()
    spans = {}

    def ws(pos):
        while pos < len(text) and text[pos].isspace():
            pos += 1
        return pos

    def walk(pos, pointer, depth):
        if depth > 30:
            raise ValueError("json_depth_limit")
        pos = ws(pos)
        start = pos
        if text[pos] == "{":
            pos = ws(pos + 1)
            while text[pos] != "}":
                key, pos = decoder.raw_decode(text, pos)
                pos = ws(pos)
                if text[pos] != ":":
                    raise ValueError("json_object_separator")
                pos = walk(pos + 1, pointer + "/" + _pointer(key), depth + 1)
                pos = ws(pos)
                if text[pos] == ",":
                    pos = ws(pos + 1)
                else:
                    break
            pos += 1
        elif text[pos] == "[":
            pos, index = ws(pos + 1), 0
            while text[pos] != "]":
                pos = walk(pos, pointer + "/" + str(index), depth + 1)
                index += 1
                pos = ws(pos)
                if text[pos] == ",":
                    pos = ws(pos + 1)
                else:
                    break
            pos += 1
        else:
            _, pos = decoder.raw_decode(text, pos)
        spans[pointer] = (start, pos)
        return pos

    walk(0, "", 0)
    return spans


def _provenance(entry, metadata):
    source = entry.get("provenance") or entry.get("source") if isinstance(entry, dict) else None
    if source is None or source == "":
        source = metadata
    if isinstance(source, dict):
        source = source.get("provenance") or source.get("source")
    return source if isinstance(source, str) and source.strip() else ""


def _provenance_date(provenance):
    if "own words" not in provenance.casefold() or _OWN_WORDS_DENIAL_RE.search(provenance):
        return None
    for match in _DATE_RE.finditer(provenance):
        try:
            # Match the resolver's UTC freshness clock across worker timezones.
            if date.fromisoformat(match.group(1)) <= datetime.now(timezone.utc).date():
                return match.group(1)
        except ValueError:
            pass
    return None


def provenance_restrictions(entry, metadata):
    """Read explicit restrictions; neither recency nor a label removes them.

    Preferred per-entry (or per-key ``_provenance``) metadata is
    ``provenance_state: {schema: keel.qresolve.provenance.v1,
    status: provisional|unreconciled|pending_reconciliation|reconciled}``.
    Legacy ``provisional: true``, ``reconciliation_required: true`` and
    ``provenance_status`` are also restrictive. Explicit provisional or
    unreconciled sweep notes are recognized in provenance/source/notes fields.
    An unmarked bank entry is NOT assumed to belong to the private sweep.

    Authorship metadata or explicit assistant-generated provenance can only
    reduce authority. ``approved_verbatim`` never turns assisted content into
    the applicant's original words. Raw memory/logs remain research-only.
    """
    provisional, assisted, reasons = False, False, set()
    records = [entry, metadata]
    # The existing provenance convention also permits one metadata object in
    # a provenance/source field. Inspect only those named objects, never an
    # arbitrary tree of applicant data and never the answer's text.
    for record in (entry, metadata):
        if isinstance(record, dict):
            records.extend(record[name] for name in ("provenance", "source")
                           if isinstance(record.get(name), dict))
    for record in records:
        fields = []
        if isinstance(record, str):
            fields.append(record)
        elif isinstance(record, dict):
            for name in ("provenance", "source", "origin", "source_kind", "notes", "queue_notes",
                         "status_reason", "reconciliation_note"):
                value = record.get(name)
                if isinstance(value, str):
                    fields.append(value)
            for name in ("provisional", "reconciliation_required"):
                if name in record:
                    if type(record[name]) is not bool:
                        provisional = True
                        reasons.add("invalid_provisional_marker")
                    elif record[name]:
                        provisional = True
                        reasons.add("explicit_provisional_marker")
            status = record.get("provenance_status")
            status = re.sub(r"[\s-]+", "_", status.casefold()) if isinstance(status, str) else status
            if status in ("provisional", "unreconciled", "pending_reconciliation", "reconciliation_pending"):
                provisional = True
                reasons.add("explicit_provisional_status")
            if "provenance_state" in record:
                state = record["provenance_state"]
                valid = (isinstance(state, dict)
                         and state.get("schema") == "keel.qresolve.provenance.v1"
                         and state.get("status") in ("provisional", "unreconciled", "pending_reconciliation", "reconciled"))
                if not valid or state["status"] != "reconciled":
                    provisional = True
                    reasons.add("provenance_state_restricted" if valid else "invalid_provenance_state")
            if "assisted" in record and (type(record["assisted"]) is not bool or record["assisted"]):
                assisted = True
                reasons.add("assisted_authorship" if type(record["assisted"]) is bool else "invalid_assisted_marker")
            authorship = record.get("authorship")
            if isinstance(authorship, str):
                authorship = re.sub(r"[\s-]+", "_", authorship.casefold())
                if re.search(r"(?:^|[\W_])(?:assistant|agent|ai|llm|model|assisted|automated|chatgpt|claude)(?:$|[\W_])", authorship):
                    assisted = True
                    reasons.add("assisted_authorship")
            elif "authorship" in record:
                assisted = True
                reasons.add("invalid_authorship_marker")
        for text in fields:
            text = text.casefold()
            if re.search(r"\b(?:provisional|unreconciled|pending[ _-]reconciliation|reconciliation[ _-]pending)\b", text):
                provisional = True
                reasons.add("provisional_provenance_note")
            # A dated sweep alone is not proof it was reconciled. Require an
            # explicit unreconciled marker or overnight-sweep origin, without
            # allowing contradictory own-words labels to promote it.
            if (re.search(r"\bovernight[\s_-]+(?:bank[\s_-]+)?sweep\b", text)
                    or (re.search(r"\b(?:bank[\s_-]+)?sweep\b", text)
                        and re.search(r"\b(?:awaiting|still owed|not reconciled|reconciliation required|report pending)\b", text))):
                provisional = True
                reasons.add("unreconciled_sweep_note")
            if re.search(r"\b(?:assistant|agent|ai|llm|model|chatgpt|claude)[\s_-]+(?:generated|written|suggested|authored|"
                         r"draft(?:ed)?|inferred|derived|rewritten|summari[sz]ed|assisted)\b|\bassisted[\s_-]+draft\b|"
                         r"\b(?:assisted|generated|written|drafted|authored|suggested|inferred|derived|rewritten|summari[sz]ed)"
                         r"\s+(?:by|with|using)\s+(?:(?:an?|the)\s+)?"
                         r"(?:ai|artificial intelligence|assistant|agent|chatgpt|claude|llm|model)\b", text):
                assisted = True
                reasons.add("assisted_provenance_note")
            if _OWN_WORDS_DENIAL_RE.search(text):
                reasons.add("negated_applicant_authorship")
    return provisional, assisted, sorted(reasons)


class Corpus:
    """One cached retrieval corpus; ``errors`` explicitly reports incompleteness.

    Missing optional sources are recorded in ``missing`` and the snapshot so
    their later appearance invalidates it. Malformed/unsafe/over-limit sources
    are errors: a caller must not interpret a partial corpus as exhaustive.
    ``eligible`` is machine-scoped answer eligibility, not permission to apply.
    ``draft_eligible`` additionally permits explicitly flagged legacy answers.
    """

    def __init__(self, workspace, *, memory_dir=None):
        self.workspace = Path(os.path.abspath(workspace))
        self.memory_dir = Path(os.path.abspath(memory_dir or self.workspace / "memory"))
        self.snapshot = {}
        self.errors = []
        self.missing = []
        self._documents = {}
        self._records = []
        self._index = {}
        self._context_index = {}
        self._index_postings = 0
        self._bank = {}
        self._bank_spans = {}
        self._bank_path = str(self.workspace / "data/answer_bank.json")
        self._bytes = 0
        self._inventory = self._memory_inventory()
        files = [(self._bank_path, "bank", 0),
                 (self.workspace / "USER.md", "memory", 1),
                 (self.workspace / "MEMORY.md", "memory", 1)]
        files += [(self.memory_dir / name, "memory", 1)
                  for name in sorted(self._inventory, reverse=True)[:MAX_MEMORY_FILES]]
        files += [(self.workspace / "data/queues/needs_input-queue.json", "queue", 2),
                  (self.workspace / "data/queues/standard-queue.json", "queue", 2),
                  (self.workspace / "data/telemetry/events.jsonl", "telemetry", 3),
                  (self.workspace / "data/application-ledger.json", "ledger", 4),
                  (self.workspace / "hidden_files/input-tray.json", "tray", 5)]
        for path, kind, tier in files:
            self._load(str(path), kind, tier)

    def _error(self, path, reason):
        self.errors.append({"source": str(path), "reason": reason})

    def _memory_inventory(self):
        fd = None
        parent = None
        try:
            parent, name = _open_parent(self.memory_dir)
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=parent)
            names = []
            with os.scandir(fd) as entries:
                for entry in entries:
                    if _MEMORY_NAME.fullmatch(entry.name):
                        names.append(entry.name)
                        if len(names) > MAX_MEMORY_FILES:
                            self._error(self.memory_dir, "memory_file_limit")
                            return tuple(sorted(names))
            return tuple(sorted(names))
        except FileNotFoundError:
            return ()
        except (OSError, ValueError):
            self._error(self.memory_dir, "memory_directory_unsafe_or_unreadable")
            return ()
        finally:
            if fd is not None:
                os.close(fd)
            if parent is not None:
                os.close(parent)

    def _load(self, path, kind, tier):
        try:
            limit = min(answer_resolver.BANK_MAX_BYTES if kind == "bank" else MAX_FILE_BYTES,
                        MAX_TOTAL_BYTES - self._bytes)
            if limit <= 0:
                raise ValueError("corpus_byte_limit")
            text, digest, loaded, size = _read(path, limit, bank=kind == "bank")
            self._bytes += size
            self.snapshot[path] = digest
            if self._bytes > MAX_TOTAL_BYTES:
                raise ValueError("corpus_byte_limit")
            self._documents[path] = (text, digest, kind, tier)
            if kind == "bank":
                if not isinstance(loaded.get("answers", {}), dict):
                    raise ValueError("bank_answers_shape")
                if not isinstance(loaded.get("_provenance", {}), dict):
                    raise ValueError("bank_provenance_shape")
                if not isinstance(loaded.get("_quarantined", {}), (dict, list)):
                    raise ValueError("bank_quarantine_shape")
                self._bank_spans = _value_spans(text)
                self._bank = loaded
            elif kind in {"memory", "telemetry"}:
                staged = []
                for lineno, line in enumerate(text.splitlines(), 1):
                    if lineno > MAX_ROWS or len(line.encode("utf-8")) > MAX_LINE_BYTES:
                        raise ValueError("source_row_or_line_limit")
                    if not line.strip():
                        continue
                    row = queue_io.strict_loads(line) if kind == "telemetry" else None
                    if kind == "telemetry" and not isinstance(row, dict):
                        raise ValueError("telemetry_row_shape")
                    staged.append((f"line:{lineno}", line, row))
                for pointer, excerpt, row in staged:
                    self._add_record(path, pointer, excerpt, row, tier)
            else:
                data = queue_io.strict_loads(text)
                rows = data.get("cards") if kind == "tray" and isinstance(data, dict) else data
                if not isinstance(rows, list) or len(rows) > MAX_ROWS:
                    raise ValueError("source_rows_shape_or_limit")
                # Locating spans once preserves exact raw evidence, including
                # escapes and Unicode, rather than regenerating JSON excerpts.
                spans = _value_spans(text)
                base = "/cards" if kind == "tray" and isinstance(data, dict) else ""
                staged = []
                for index, row in enumerate(rows):
                    if not isinstance(row, dict):
                        raise ValueError("source_row_shape")
                    pointer = base + "/" + str(index)
                    start, end = spans[pointer]
                    staged.append((pointer, text[start:end], row))
                for pointer, excerpt, row in staged:
                    self._add_record(path, pointer, excerpt, row, tier)
        except FileNotFoundError:
            self.snapshot[path] = None
            self.missing.append(path)
        except (OSError, ValueError, TypeError, RecursionError, IndexError):
            self._error(path, "source_unsafe_invalid_changed_or_over_limit")
            if kind == "bank":
                self._bank = {}

    def _add_record(self, path, pointer, excerpt, row, tier):
        # Bounded research snippets do not become candidate answers. A long
        # record remains searchable only inside the displayed exact slice.
        excerpt = excerpt[:MAX_EXCERPT]
        terms = _tokens(excerpt)
        if (len(self._records) >= MAX_RECORDS
                or self._index_postings + len(terms) > MAX_INDEX_POSTINGS):
            raise ValueError("corpus_record_or_index_limit")
        self._index_postings += len(terms)
        index = len(self._records)
        event_id = row.get("event_id") or row.get("id") if isinstance(row, dict) else None
        if not isinstance(event_id, (str, int)) or isinstance(event_id, bool):
            event_id = None
        self._records.append((path, pointer, excerpt, terms, tier, event_id))
        for term in terms:
            self._index.setdefault(term, set()).add(index)
        if isinstance(row, dict):
            for field in ("role_id", "company", "employer"):
                val = row.get(field)
                if isinstance(val, str) and val.strip():
                    self._context_index.setdefault((field, val.casefold()), set()).add(index)

    def _bank_hits(self, question, contexts):
        fp, fact = canonical_fingerprint(question), fact_key(question)
        terms = _tokens(question)
        answers = self._bank.get("answers", {})
        quarantine = self._bank.get("_quarantined", {})
        registry = {c.get("company") or c.get("employer") for c in contexts
                    if isinstance(c.get("company") or c.get("employer"), str)}
        text, digest, _, _ = self._documents.get(self._bank_path, ("", "", "", 0))
        for key, original in answers.items():
            entry = dict(original) if isinstance(original, dict) else {"value": original}
            questions = [entry.get("question")]
            variants = entry.get("question_variants", [])
            if isinstance(variants, list):
                questions.extend(variants)
            exact = any(isinstance(q, str) and q.strip() and canonical_fingerprint(q) == fp
                        for q in questions)
            # A canonical key is a fallback for older entries without a
            # question. It cannot override an explicitly different question.
            has_question = any(isinstance(q, str) and q.strip() for q in questions)
            exact = exact or (not has_question and fact is not None and key == fact)
            if not exact and not terms.intersection(_tokens(key.replace("_", " "))):
                continue
            raw_value = entry.get("value", entry.get("answer"))
            metadata = self._bank.get("_provenance", {}).get(key)
            prov = _provenance(entry, metadata)
            provisional, assisted, restrictions = provenance_restrictions(entry, metadata)
            if prov and not entry.get("provenance") and not entry.get("source"):
                entry["provenance"] = prov
            resolutions = [answer_resolver.resolve(
                key, entry, employer=context.get("company") or context.get("employer"),
                role_context=context, registry=registry) for context in (contexts or [{}])]
            denied_authorship = "negated_applicant_authorship" in restrictions
            forbidden = key in quarantine or entry.get("draftable") is False or assisted or denied_authorship
            approved = (not forbidden and bool(prov) and isinstance(raw_value, str)
                        and bool(raw_value.strip()) and all(
                            r.status in {"resolved", "legacy"} for r in resolutions))
            legacy = any(r.legacy or r.status == "legacy" for r in resolutions)
            machine_scope = entry.get("scope")
            legacy = legacy or not (machine_scope == "global" or (
                isinstance(machine_scope, str) and machine_scope.startswith("employer:")))
            # Don't replace exact bank strings with the resolver's stripped
            # version or coerce numbers/bools into applicant wording.
            value_pointer = "/answers/" + _pointer(key)
            if isinstance(original, dict):
                value_pointer += "/value" if "value" in original else "/answer"
            start, end = self._bank_spans.get(value_pointer, (0, 0))
            yield {"source": self._bank_path, "pointer": value_pointer,
                   "excerpt": text[start:end][:MAX_EXCERPT], "answer": raw_value if approved else None,
                   "bank_key": key, "match": "exact" if exact else "keyword",
                   "provenance": prov, "own_words": not (provisional or assisted or denied_authorship) and _provenance_date(prov) is not None,
                   "provenance_date": _provenance_date(prov),
                   "approved_verbatim": entry.get("approved_verbatim") is True and not (provisional or assisted or denied_authorship),
                   "eligible": bool(exact and approved and not legacy and not provisional),
                   "draft_eligible": bool(exact and approved), "legacy": legacy,
                   "provisional": provisional, "assisted": assisted,
                   "authority_restrictions": restrictions,
                   "scope": sorted({r.scope for r in resolutions}),
                   "scope_status": [r.status for r in resolutions],
                   "document_sha256": digest, "tier": "provisional" if provisional else "bank"}

    def retrieve(self, card, contexts):
        """Return all matching bank candidates plus bounded lower-trust evidence."""
        if not isinstance(card, dict) or not isinstance(card.get("question"), str):
            raise ValueError("card_question_required")
        if not isinstance(contexts, list) or any(not isinstance(c, dict) for c in contexts):
            raise ValueError("contexts_must_be_objects")
        # Digest ``question`` is a display excerpt; ``norm`` retains the
        # complete prompt and its consent, quantity, and scope qualifiers.
        question = card.get("norm") or card["question"]
        if not isinstance(question, str):
            raise ValueError("card_norm_must_be_text")
        bank_hits = list(self._bank_hits(question, contexts))
        hits = [hit for hit in bank_hits if not hit["provisional"]]
        provisional_hits = [hit for hit in bank_hits if hit["provisional"]]
        terms = _tokens(question)
        candidates, contextual = set(), set()
        for term in terms:
            candidates.update(self._index.get(term, ()))
        for context in contexts:
            for field in ("role_id", "company", "employer"):
                value = context.get(field)
                if isinstance(value, str):
                    contextual.update(self._context_index.get((field, value.casefold()), ()))
        candidates.update(contextual)
        ranked = []
        for index in candidates:
            path, pointer, excerpt, rtokens, tier, event_id = self._records[index]
            overlap = len(terms.intersection(rtokens))
            if not overlap and index not in contextual:
                continue
            ranked.append((tier, -overlap, index not in contextual, index))
        for _, _, _, index in sorted(ranked)[:MAX_RESEARCH_HITS]:
            path, pointer, excerpt, _, _, event_id = self._records[index]
            _, digest, kind, _ = self._documents[path]
            if kind != "memory" and provisional_hits:
                hits.extend(provisional_hits)
                provisional_hits = []
            hit = {"source": path, "pointer": pointer, "excerpt": excerpt,
                   "answer": None, "bank_key": None, "match": "keyword",
                   "provenance": "research_only", "own_words": False,
                   "provenance_date": None, "approved_verbatim": False,
                   "eligible": False, "draft_eligible": False, "legacy": False,
                   "scope": [], "document_sha256": digest, "tier": kind}
            if event_id is not None:
                hit["event_id"] = event_id
            hits.append(hit)
        hits.extend(provisional_hits)
        return hits

    def verify_snapshot(self):
        """Revalidate observed bytes and source inventory; fail closed on errors."""
        if self.errors:
            return False
        if self._memory_inventory() != self._inventory or self.errors:
            return False
        for path, expected in self.snapshot.items():
            try:
                _, actual, _, _ = _read(path)
                if actual != expected:
                    return False
            except FileNotFoundError:
                if expected is not None:
                    return False
            except (OSError, ValueError):
                return False
        return True
