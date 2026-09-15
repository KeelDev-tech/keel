#!/usr/bin/env bash
# Keel — package.sh
# Builds a clean distributable zip: source + examples + docs only.
# NEVER includes: data/ working copies, launch packets, briefs, telemetry,
# answer_bank.json (personalized), or any *_history/pycache.
set -euo pipefail
cd "$(dirname "$0")"
VER="$(cat VERSION)"
OUT="dist/keel-${VER}.zip"

rm -rf /tmp/keel-pkg && mkdir -p /tmp/keel-pkg "dist"
cp -r engines docs sample_data tests dist "/tmp/keel-pkg/" 2>/dev/null || true
mkdir -p /tmp/keel-pkg/dist
for f in README.md LICENSE SPLIT.md VERSION setup.sh start.sh package.sh keel.config.json .gitignore; do
  [ -f "$f" ] && cp "$f" "/tmp/keel-pkg/$f"
done
# Scrub anything that must never ship (defense in depth; also checked by CI).
rm -rf /tmp/keel-pkg/engines/__pycache__ \
       /tmp/keel-pkg/engines/briefs \
       /tmp/keel-pkg/engines/answer_bank.json \
       /tmp/keel-pkg/engines/employer_form_patterns.json \
       /tmp/keel-pkg/dist
mkdir -p /tmp/keel-pkg/dist

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
HITS="$(grep -rliE "$PATTERN" /tmp/keel-pkg 2>/dev/null | grep -v '/package.sh$' || true)"
SELF="$(grep -iE "$PATTERN" /tmp/keel-pkg/package.sh 2>/dev/null | grep -v '^PATTERN=' || true)"
if [ -n "$HITS" ] || [ -n "$SELF" ]; then
  echo "BLOCKED: personal-data hits in package payload:"
  echo "$HITS"
  echo "$SELF"
  exit 1
fi

cd /tmp/keel-pkg
zip -qr "$OLDPWD/$OUT" . -x '*/.*'
cd "$OLDPWD"
echo "Built $OUT ($(du -h "$OUT" | cut -f1))"
unzip -l "$OUT" | tail -5
