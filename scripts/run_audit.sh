#!/usr/bin/env bash
# AUDIT mode: preflight, deploy only what the audit needs, run the read-only checks.
# Usage: scripts/run_audit.sh [--no-deploy] <target> <warehouse-id> [catalog] [account-profile] [workspace-id]
#   The target must be positively identified as AUDIT-ONLY (resolved resources: just lab_audit, the
#   results schema without grants and the dashboard), e.g. customer_audit. Anything else, or a target
#   that can't be inspected, is refused, with or without --no-deploy. (In demo mode the audit already
#   runs as part of scripts/run_demo.sh.) --no-deploy runs lab_audit without deploying.
set -euo pipefail
DEPLOY=1
if [ "${1:-}" = "--no-deploy" ]; then DEPLOY=0; shift; fi
TARGET="${1:?target}"; WH="${2:?warehouse id}"; CAT="${3:-main}"; ACC="${4:-}"; WS="${5:-}"
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
source scripts/lib.sh
valid_target "$TARGET"; valid_name "$WH" "warehouse id"; valid_name "$CAT" catalog
VARS=(--var "warehouse_id=$WH" --var "catalog=$CAT")

echo "== Preflight"
MODE=$(bundle_field mode "$TARGET" "${VARS[@]}" 2>/dev/null) || MODE="uninspectable"
if [ "$MODE" != audit ]; then
  echo "Refusing: target '$TARGET' is not positively identified as audit-only (inspection: $MODE)." >&2
  echo "Use the customer_audit target. Demo targets run the audit as part of scripts/run_demo.sh." >&2
  exit 1
fi
databricks bundle validate --strict -t "$TARGET" "${VARS[@]}" >/dev/null
PROFILE=$(bundle_profile "$TARGET" "${VARS[@]}")
P=(); [ -n "$PROFILE" ] && P=(-p "$PROFILE")
databricks ${P[@]+"${P[@]}"} current-user me -o json | python3 -c 'import json,sys;print("signed in as", json.load(sys.stdin)["userName"])'
databricks ${P[@]+"${P[@]}"} warehouses get "$WH" -o json | python3 -c 'import json,sys;w=json.load(sys.stdin);print("warehouse", w["name"], w["state"])'

SUITE="${SUITE_RUN_ID:-$(new_suite_id)}"
echo "== Audit suite $SUITE"
[ "$DEPLOY" = 1 ] && databricks bundle deploy -t "$TARGET" "${VARS[@]}"
databricks bundle run -t "$TARGET" "${VARS[@]}" --params "suite_run_id=$SUITE" lab_audit

if [ -n "$ACC" ]; then
  echo "== Account-level checks (read-only inventory)"
  python3 scripts/account_checks.py "$ACC" "$WS"
else
  echo "Skipped account-level checks (no account profile). See docs/manual-steps.md M-01, M-02, M-06."
fi
