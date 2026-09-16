#!/usr/bin/env bash
# Keel — "Make it mine" setup wizard.
# Copies the example files into your personal working copies and walks you
# through the fields that must be YOUR truth. Nothing here asks for secrets.
set -euo pipefail
cd "$(dirname "$0")"
export KEEL_HOME="${KEEL_HOME:-$PWD}"

echo "=============================================="
echo " Keel — Make it mine"
echo "=============================================="
echo "This wizard personalizes YOUR copy. Example files stay untouched;"
echo "your working copies live in data/ and are yours alone."
echo

mkdir -p data/queues data/resumes data/mail dashboard engines/briefs

copy_example() {
  local src="$1" dst="$2"
  if [ -f "$dst" ]; then
    echo "  (kept existing $dst)"
  else
    cp "$src" "$dst"
    echo "  created $dst"
  fi
}

echo "--- Personal files ---"
copy_example engines/answer_bank.example.json data/answer_bank.json
copy_example engines/employer_form_patterns.example.json data/employer_form_patterns.json
copy_example sample_data/applicant_profile.example.json data/applicant_profile.json
copy_example engines/edge_case_registry.example.json data/edge_case_registry.json
copy_example engines/policy.example.json data/policy.json
if [ ! -f data/employer-blocklist.md ]; then
  printf '# Employer blocklist\n\nEmployers you will never apply to (scam flags, bad experiences, etc.).\nOne per line, plain text.\n' > data/employer-blocklist.md
  echo "  created data/employer-blocklist.md"
fi
if [ ! -f data/queues/standard-queue.json ]; then
  echo '[]' > data/queues/standard-queue.json
  echo "  created data/queues/standard-queue.json (empty)"
fi
if [ ! -f data/queues/strategic-queue.json ]; then
  echo '[]' > data/queues/strategic-queue.json
  echo "  created data/queues/strategic-queue.json (empty)"
fi
if [ ! -f data/queues/needs_input-queue.json ]; then
  echo '[]' > data/queues/needs_input-queue.json
  echo "  created data/queues/needs_input-queue.json (empty)"
fi

echo
echo "--- Identity (these flow into your answer bank and briefs) ---"
if [ -t 0 ]; then
  read -rp "Your full name: " FULLNAME
  read -rp "Your email: " EMAIL
  read -rp "Your phone (e.g. +1-555-0100): " PHONE
  read -rp "Your location (City, State, Country): " LOCATION
  read -rp "Your LinkedIn URL: " LINKEDIN
  read -rp "Timezone (default America/Los_Angeles): " TZ; TZ="${TZ:-America/Los_Angeles}"
else
  echo "  (non-interactive stdin: identity prompts skipped)"
  echo "  Placeholders kept — edit data/applicant_profile.json and"
  echo "  data/answer_bank.json with YOUR truth afterward."
  FULLNAME=""; EMAIL=""; PHONE=""; LOCATION=""; LINKEDIN=""; TZ=""
fi

if [ -n "${FULLNAME:-}" ]; then
# Identity flows into data/ only (engines read data/ via KEEL_HOME).
# The repo-root keel.config.json is NEVER personalized by this wizard —
# it stays the stock template so your private details can't be committed
# or packaged by accident. Edit its lanes/metro locally at your own
# discretion (see docs/PERSONALIZE.md), but never commit it with your values.
python3 - "$FULLNAME" "$EMAIL" "$PHONE" "$LOCATION" "$LINKEDIN" << 'PYEOF'
import json, sys
name, email, phone, loc, li = sys.argv[1:6]
parts = name.split()
first, last = parts[0], (parts[-1] if len(parts) > 1 else "")
bank = json.load(open("data/answer_bank.json"))
a = bank["answers"]
a.update({"first_name": first, "last_name": last, "email": email,
          "phone": phone, "location": loc, "linkedin": li})
json.dump(bank, open("data/answer_bank.json", "w"), indent=2)
prof = json.load(open("data/applicant_profile.json"))
prof["name"] = name
prof["contact"].update({"email": email, "phone": phone,
                        "location": loc, "linkedin": li})
json.dump(prof, open("data/applicant_profile.json", "w"), indent=2)
print("  wrote your identity into data/answer_bank.json and data/applicant_profile.json")
PYEOF
fi

# Engines resolve your personal files from KEEL_HOME/data/ first — no copies
# into engines/ are needed or made. (A private working copy inside the repo
# tree would go stale after edits and could be committed by accident;
# engines/answer_bank.json is gitignored as defense in depth.)
echo "  engines read your personal files from data/ (KEEL_HOME)"

echo
echo "--- Truthfulness checkpoint ---"
echo "Keel's rule: it only ever claims what you tell it is true."
echo "Next steps:"
echo "  1. Edit data/applicant_profile.json — real history, real credentials."
echo "  2. Edit data/answer_bank.json — verified answers (tenure figures, auth, education)."
echo "  3. Set your boundaries in data/policy.json — travel/office/relocation caps and"
echo "     attestation rules. Prescreen parks these for your explicit answer"
echo "     (it never invents a cap); your policy keeps your answers consistent."
echo "  4. Put any job-site accounts in credentials/ (one file per account, chmod 600);"
echo "     see docs/CREDENTIALS.md. The directory is gitignored — never commit it."
echo "  5. Fill data/employer-blocklist.md with employers to avoid."
echo "  6. Add leads: python3 engines/score_roles.py --in your_roles.json --out data/scored.json"
echo "     High-fit-but-unverifiable leads can also wait in the strategic queue:"
echo "     data/queues/strategic-queue.json"
echo "  7. Keep the verify cadence running: KEEL_HOME=\$PWD python3 engines/verify_retry.py"
echo "     (re-checks parked leads over HTTP; promotes LIVE ones, buries dead ones)"
echo "  8. Build a launch packet: KEEL_HOME=\$PWD python3 engines/apply_loop.py"
echo "  9. Answer the input tray when a lead parks for your words:"
echo "     data/queues/needs_input-queue.json — answer there and the lane resumes"
echo "  10. Render the dashboard: KEEL_HOME=\$PWD python3 engines/build_dashboard.py"
echo
echo "Done. Your workspace is ready — run ./start.sh for a status overview."
