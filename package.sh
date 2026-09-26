#!/usr/bin/env bash
# Keel — package.sh
# Builds a clean distributable zip: source + examples + docs only.
# Reproducible: mktemp staging, fixed file timestamps, sorted entry order,
# normalized modes, per-file SHA-256 MANIFEST.json, post-build verify step.
# NEVER includes: data/ working copies, launch packets, briefs, telemetry,
# answer_bank.json (personalized), or any *_history/pycache.
# --release additionally runs the privacy egress gate (gate_release.py):
# the zip is cleared for publication ONLY on a verified counsel decision
# (G1 sign-off) binding its exact digest; otherwise it stays local.
set -euo pipefail
umask 022
# ZIP stores wall-clock timestamps; pin its timezone as well as source mtimes.
export TZ=UTC
cd "$(dirname "$0")"
KEEL_ROOT="$PWD"

FORCE=0
VERIFY_ONLY=""
RELEASE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    --verify) VERIFY_ONLY="${2:?--verify needs a zip path}"; shift 2 ;;
    --release) RELEASE=1; shift ;;
    *) echo "usage: $0 [--force] [--verify ZIP] [--release]" >&2; exit 2 ;;
  esac
done

# verify_archive <zip>: integrity check adapted from the review candidate's
# verify() — duplicate/unsafe paths, size limits, manifest set + hash check.
verify_archive() {
  python3 - "$1" <<'PYEOF'
import hashlib, json, re, stat, sys, unicodedata, zipfile
from pathlib import PurePosixPath
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_BUNDLE_BYTES = 100 * 1024 * 1024
MAX_RELEASE_FILES = 10000
def strict_json(raw):
    """Reject duplicate keys and non-standard constants at every object depth."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key: " + key)
            result[key] = value
        return result

    def constant(value):
        raise ValueError("invalid JSON constant: " + value)

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)

def validate_paths(names):
    """Require canonical, portable file paths and unambiguous directory topology."""
    folded = set()
    directory_spelling = {}
    reserved = {"CON", "PRN", "AUX", "NUL", *("COM" + str(n) for n in range(1, 10)),
                *("LPT" + str(n) for n in range(1, 10))}
    for name in names:
        if type(name) is not str:
            raise ValueError("invalid source path")
        path = PurePosixPath(name)
        if (not path.parts or path.is_absolute() or ".." in path.parts
                or any(c in name for c in '\\:<>"|?*') or str(path) != name
                or any(ord(c) < 32 or ord(c) == 127 for c in name)
                or unicodedata.normalize("NFC", name) != name
                or any(part.endswith((".", " ")) or part.split(".")[0].upper() in reserved
                       for part in path.parts)):
            raise ValueError("unsafe source path")
        key = name.casefold()
        if key in folded:
            raise ValueError("duplicate or case-conflicting source path")
        folded.add(key)
        for parent in path.parents:
            spelling = str(parent)
            parent_key = spelling.casefold()
            if parent_key in directory_spelling and directory_spelling[parent_key] != spelling:
                raise ValueError("case-conflicting directory path")
            directory_spelling[parent_key] = spelling
    for name in names:
        if any(str(parent).casefold() in folded for parent in PurePosixPath(name).parents):
            raise ValueError("file/directory path conflict")

def verify(archive_path):
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        names = [info.filename for info in entries]
        if not 1 <= len(entries) <= MAX_RELEASE_FILES + 1:
            raise ValueError('invalid archive file count')
        validate_paths(names)
        for info in entries:
            mode = info.external_attr >> 16
            if (info.orig_filename != info.filename or info.is_dir() or info.flag_bits & 1 or info.external_attr & 0x10
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                raise ValueError('archive contains a non-regular, encrypted or ambiguous member')
            if info.file_size > MAX_FILE_BYTES:
                raise ValueError('archive exceeds limits')
        if sum(info.file_size for info in entries) > MAX_BUNDLE_BYTES:
            raise ValueError('archive exceeds limits')
        if 'MANIFEST.json' not in names:
            raise ValueError('MANIFEST.json missing')
        manifest = strict_json(archive.read('MANIFEST.json'))
        if (not isinstance(manifest, dict) or type(manifest.get('schema_version')) is not int
                or manifest['schema_version'] != 1 or not isinstance(manifest.get('version'), str)
                or not isinstance(manifest.get('files'), dict)):
            raise ValueError('invalid archive manifest')
        if set(names) != set(manifest['files']) | {'MANIFEST.json'} or 'MANIFEST.json' in manifest['files']:
            raise ValueError('manifest file set mismatch')
        for name, record in manifest['files'].items():
            if (not isinstance(record, dict) or type(record.get('bytes')) is not int
                    or not 0 <= record['bytes'] <= MAX_FILE_BYTES
                    or type(record.get('sha256')) is not str
                    or not re.fullmatch(r'[0-9a-f]{64}', record['sha256'])):
                raise ValueError('invalid manifest file record: ' + name)
            with archive.open(name) as stream:
                body = stream.read(MAX_FILE_BYTES + 1)
            if len(body) != record['bytes'] or hashlib.sha256(body).hexdigest() != record['sha256']:
                raise ValueError('manifest mismatch: ' + name)
    return {'verified_integrity': True, 'files': len(names),
            'authenticity': 'not signed; compare a trusted archive digest'}

try:
    result = verify(sys.argv[1])
except (ValueError, TypeError, KeyError, OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile) as exc:
    print(json.dumps({'verified_integrity': False, 'error': str(exc)}))
    sys.exit(1)
print(json.dumps(result))
PYEOF
}

if [ -n "$VERIFY_ONLY" ]; then
  verify_archive "$VERIFY_ONLY"
  exit 0
fi

VER="$(cat VERSION)"
OUT="dist/keel-${VER}.zip"
if [ -e "$OUT" ] && [ "$FORCE" -ne 1 ]; then
  echo "refusing: $OUT already exists (use --force to overwrite)" >&2
  exit 1
fi

# Secure staging dir (mktemp, never rm -rf on a fixed path); auto-cleaned.
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/keel-pkg.XXXXXX")"
LIST="$(mktemp "${TMPDIR:-/tmp}/keel-files.XXXXXX")"
trap 'rm -rf "$STAGE" "$LIST"' EXIT

mkdir -p "$STAGE" "dist"
cp -r engines docs monitors worker-charter sample_data tests "$STAGE/"
for f in README.md LICENSE SPLIT.md VERSION setup.sh start.sh package.sh keel.config.json .gitignore; do
  [ -f "$f" ] && cp "$f" "$STAGE/$f"
done
# Scrub anything that must never ship (defense in depth; also checked by CI).
rm -rf "$STAGE/engines/__pycache__" \
       "$STAGE/engines/briefs" \
       "$STAGE/engines/answer_bank.json" \
       "$STAGE/engines/employer_form_patterns.json" \
       "$STAGE/dist"
mkdir -p "$STAGE/dist"

# Pre-flight personal-data scan on the package payload.
# Personal patterns live OUTSIDE the repo (~/.config/keel/scrub-patterns) so
# this script never embeds personal identifiers itself. The fallback is a
# generic shape-based pattern (emails, phone-like numbers, SSNs) with no names.
SCRUB_FILE="$HOME/.config/keel/scrub-patterns"
if [ -f "$SCRUB_FILE" ]; then
  PATTERN="$(cat "$SCRUB_FILE")"
else
  PATTERN='[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}|(\+?1[-. ]?)?\(?[0-9]{3}\)?[-. ][0-9]{3}[-. ][0-9]{4}|[0-9]{3}-[0-9]{2}-[0-9]{4}'
fi
HITS="$(grep -rliE "$PATTERN" "$STAGE" 2>/dev/null | grep -v '/package.sh$' || true)"
SELF="$(grep -iE "$PATTERN" "$STAGE/package.sh" 2>/dev/null | grep -v '^PATTERN=\|^SCRUB_FILE=\|^# Personal patterns' || true)"
if [ -n "$HITS" ] || [ -n "$SELF" ]; then
  echo "BLOCKED: personal-data hits in package payload:"
  echo "$HITS"
  echo "$SELF"
  exit 1
fi

# --- Reproducible payload (K37) -------------------------------------------
# Fixed timestamps, normalized modes, sorted entry order, per-file manifest.
FIXED_TS='2020-01-01 00:00:00 UTC'

if [ -n "$(find "$STAGE" -type l -print -quit)" ]; then
  echo "refusing: symlinks in package payload:" >&2
  find "$STAGE" -type l >&2
  exit 1
fi
oversize="$(find "$STAGE" -type f -size +8M -print -quit)"
if [ -n "$oversize" ]; then
  echo "refusing: payload file exceeds 8 MiB: $oversize" >&2
  exit 1
fi

find "$STAGE" -type f -name '*.sh' -exec chmod 0755 {} +
find "$STAGE" -type f ! -name '*.sh' -exec chmod 0644 {} +
find "$STAGE" -exec touch -h -d "$FIXED_TS" {} +

# Entry list: relative paths, byte-sorted, no dotfiles, no bytecode caches.
(cd "$STAGE" && find . -type f \
  | grep -v '/\.' \
  | grep -v '/__pycache__/' \
  | grep -v '/\.pytest_cache/' \
  | sed 's|^\./||' | LC_ALL=C sort) > "$LIST"

# Per-file SHA-256 MANIFEST.json (adapted from the review candidate).
python3 - "$STAGE" "$LIST" <<'PYEOF'
import hashlib, json, sys
stage, list_path = sys.argv[1], sys.argv[2]
names = open(list_path).read().split()
version = open(stage + '/VERSION').read().strip()
files = {}
for name in names:
    with open(stage + '/' + name, 'rb') as fh:
        body = fh.read()
    files[name] = {'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body)}
manifest = {'schema_version': 1, 'version': version, 'files': files}
with open(stage + '/MANIFEST.json', 'w') as fh:
    fh.write(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
PYEOF
chmod 0644 "$STAGE/MANIFEST.json"
touch -d "$FIXED_TS" "$STAGE/MANIFEST.json"
printf 'MANIFEST.json\n' >> "$LIST"
LC_ALL=C sort -o "$LIST" "$LIST"

(cd "$STAGE" && zip -q -X -9 "$KEEL_ROOT/$OUT" -@ < "$LIST")

# Post-build verify step: the artifact must verify against its own manifest.
verify_archive "$KEEL_ROOT/$OUT"
SHA="$(sha256sum "$KEEL_ROOT/$OUT" | cut -d' ' -f1)"
echo "Built $OUT"
echo "sha256: $SHA"
unzip -l "$KEEL_ROOT/$OUT" | tail -5

# Egress gate (bypass-3 fix): --release cuts a publishable release. The
# artifact may leave the private workspace ONLY on a verified counsel
# decision binding this exact digest (privacy-counsel sign-off, G1).
# Default DENY: without G1 this step fails and the zip stays local.
if [ "$RELEASE" -eq 1 ]; then
  echo "Egress gate: requesting publication approval for $OUT ..."
  python3 "$KEEL_ROOT/privacy/gate_release.py" \
    --release-id "keel-${VER}" --artifact "$KEEL_ROOT/$OUT" \
    || { echo "EGRESS DENIED: $OUT stays private (see above)" >&2; exit 1; }
  echo "Egress ALLOWED: $OUT is cleared for publication."
fi
