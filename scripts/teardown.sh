#!/usr/bin/env bash
# Remove what the lab created, and only that.
# Usage: scripts/teardown.sh [--dry-run] [--accept-export-suite <suite-id>] <target> <warehouse-id> [catalog]
# Order: export results -> lab_teardown job (deletes lab_manifest items only; tables last) -> bundle destroy ->
#        scripts/teardown_identities.sh (identities bootstrap recorded for THIS target and workspace; retryable).
# Export: a fresh export is tried first. If it fails, an earlier export is accepted only if its .meta.json
#         checksum matches and it was made for this workspace, target, catalog and schema, and its suite is the
#         deployment's latest suite (or, if that can't be read any more, the one given with --accept-export-suite).
# If the teardown job reports any failed deletion it fails, and this script stops BEFORE bundle destroy.
# Audit-only targets: no teardown job and no identity cleanup; only bundle destroy.
# --dry-run: runs lab_teardown with dry_run=true and teardown_identities.sh --dry-run. Changes nothing.
set -euo pipefail
DRY=0; ACCEPT_SUITE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --accept-export-suite) ACCEPT_SUITE="${2:?suite id}"; shift 2 ;;
    *) break ;;
  esac
done
[ -z "$ACCEPT_SUITE" ] || python3 "$(dirname "$0")/validate_names.py" "suite_id=$ACCEPT_SUITE" || exit 2
TARGET="${1:?target}"; WH="${2:?warehouse id}"; CAT="${3:-main}"
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
source scripts/lib.sh
valid_target "$TARGET"; valid_name "$WH" "warehouse id"; valid_name "$CAT" catalog
VARS=(--var "warehouse_id=$WH" --var "catalog=$CAT")
if [ -f .lab.env ]; then
  # shellcheck disable=SC1091
  source .lab.env
  VARS+=(--var "clinician_sp=$CLINICIAN_SP" --var "pii_reader_sp=$PII_READER_SP" --var "outsider_sp=$OUTSIDER_SP" \
    --var "clinicians_group=${CLINICIANS_GROUP:-lab_clinicians}" --var "pii_readers_group=${PII_READERS_GROUP:-lab_pii_readers}" \
    --var "all_facilities_group=${ALL_FACILITIES_GROUP:-lab_all_facilities}" --var "outsiders_group=${OUTSIDERS_GROUP:-lab_outsiders}")
fi
MARKER_PREFIX="genie-governance-lab:"
MODE=$(bundle_field mode "$TARGET" "${VARS[@]}")
SCH=$(bundle_field schema "$TARGET" "${VARS[@]}")
PROFILE=$(bundle_profile "$TARGET" "${VARS[@]}")

WS_ID=$(workspace_id "$PROFILE")
P=(); [ -n "$PROFILE" ] && P=(-p "$PROFILE")

latest_suite() {   # latest suite run ID of this deployment, empty if it can't be read
  local body
  body=$(python3 -c 'import json,sys;print(json.dumps({"warehouse_id":sys.argv[1],"catalog":sys.argv[2],"schema":sys.argv[3],
    "statement":"SELECT run_id FROM suite_runs ORDER BY started_at DESC LIMIT 1","wait_timeout":"30s","disposition":"INLINE","format":"JSON_ARRAY"}))' "$WH" "$CAT" "$SCH")
  databricks ${P[@]+"${P[@]}"} api post /api/2.0/sql/statements --json "$body" 2>/dev/null | python3 -c '
import json,sys
r=json.load(sys.stdin)
if r.get("status",{}).get("state")=="SUCCEEDED" and (r.get("result") or {}).get("data_array"): print(r["result"]["data_array"][0][0])' 2>/dev/null || true
}

verified_prior_export() {   # newest export matching workspace, deployment and suite, with a valid checksum
  python3 - "$WS_ID" "$TARGET" "$CAT" "$SCH" "$1" <<'PY'
import glob, hashlib, json, os, sys
ws, target, cat, sch, suite = sys.argv[1:]
for f in sorted(glob.glob("evidence/results-*.csv"), key=os.path.getmtime, reverse=True):
    m = f[:-4] + ".meta.json"
    if not os.path.exists(m):
        continue
    meta = json.load(open(m))
    if (str(meta.get("workspace_id")), meta.get("target"), meta.get("catalog"), meta.get("schema")) == (ws, target, cat, sch) \
       and meta.get("suite_run_ids") == [suite] and meta.get("rows", 0) > 0 \
       and hashlib.sha256(open(f, "rb").read()).hexdigest() == meta.get("sha256"):
        print(f); break
PY
}

if [ "$DRY" = 1 ]; then
  echo "== DRY RUN: nothing is changed (target mode: $MODE)"
  if [ "$MODE" = demo ]; then
    databricks bundle run -t "$TARGET" "${VARS[@]}" --params dry_run=true lab_teardown
  else
    echo "target $TARGET has no lab_teardown job ($MODE mode): only 'bundle destroy' would run"
  fi
  echo "== Identities (dry run)"
  if [ "$MODE" = demo ]; then scripts/teardown_identities.sh --dry-run "$TARGET" "$WS_ID" || true
  else echo "  none (audit-only target: identities are never touched)"; fi
  exit 0
fi

if ! scripts/export_results.sh "$TARGET" "$WH" "$CAT"; then
  SUITE=$(latest_suite); SUITE="${SUITE:-$ACCEPT_SUITE}"
  if [ -z "$SUITE" ]; then
    echo "Export failed and the deployment's latest suite can't be read. Pass --accept-export-suite <suite-id> to" >&2
    echo "accept an earlier export of that suite. Not tearing down." >&2; exit 1
  fi
  prior=$(verified_prior_export "$SUITE")
  if [ -n "$prior" ]; then
    echo "Fresh export failed; using the verified prior export $prior (workspace $WS_ID, $TARGET/$CAT/$SCH, suite $SUITE)"
  else
    echo "Export failed and no verified prior export matches workspace $WS_ID, $TARGET/$CAT/$SCH, suite $SUITE. Not tearing down." >&2
    exit 1
  fi
fi
if [ "$MODE" = demo ]; then
  if ! databricks bundle run -t "$TARGET" "${VARS[@]}" --params dry_run=false lab_teardown; then
    echo "lab_teardown reported failed deletions (see the job run). NOT destroying the bundle; fix and rerun." >&2
    exit 1
  fi
fi
databricks bundle destroy -t "$TARGET" "${VARS[@]}" --auto-approve

if [ "$MODE" != demo ]; then
  echo "Audit-only target: no identities to remove."
else
  scripts/teardown_identities.sh "$TARGET" "$WS_ID"   # retryable on its own if it fails
fi
