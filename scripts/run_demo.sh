#!/usr/bin/env bash
# DEMO mode end to end: deploy, set up the sandbox, run the three personas, collect, then the audit.
# All jobs share one suite run ID, so the dashboard and the export show this run only.
# Usage: scripts/run_demo.sh <target> <warehouse-id> [catalog]
# Needs .lab.env from scripts/bootstrap_personas.sh (or written by hand).
set -euo pipefail
TARGET="${1:?target, e.g. demo}"; WH="${2:?serverless SQL warehouse id}"; CAT="${3:-main}"
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
source scripts/lib.sh
valid_target "$TARGET"; valid_name "$WH" "warehouse id"; valid_name "$CAT" catalog
# shellcheck disable=SC1091
source .lab.env
VARS=(--var "warehouse_id=$WH" --var "catalog=$CAT"
      --var "clinician_sp=$CLINICIAN_SP" --var "pii_reader_sp=$PII_READER_SP" --var "outsider_sp=$OUTSIDER_SP"
      --var "clinicians_group=${CLINICIANS_GROUP:-lab_clinicians}" --var "pii_readers_group=${PII_READERS_GROUP:-lab_pii_readers}"
      --var "all_facilities_group=${ALL_FACILITIES_GROUP:-lab_all_facilities}" --var "outsiders_group=${OUTSIDERS_GROUP:-lab_outsiders}")
[ -n "${GATEWAY_ENDPOINT:-}" ] && VARS+=(--var "gateway_endpoint=$GATEWAY_ENDPOINT")
MODE=$(bundle_field mode "$TARGET" "${VARS[@]}" 2>/dev/null) || MODE="uninspectable"
if [ "$MODE" != demo ]; then
  echo "Refusing: target '$TARGET' is not a demo-mode target (inspection: $MODE). Use scripts/run_audit.sh for audit-only targets." >&2
  exit 1
fi
SUITE="${SUITE_RUN_ID:-$(new_suite_id)}"
echo "== Suite run $SUITE"

databricks bundle validate --strict -t "$TARGET" "${VARS[@]}"
databricks bundle deploy -t "$TARGET" "${VARS[@]}"
databricks bundle run -t "$TARGET" "${VARS[@]}" --params "suite_run_id=$SUITE" lab_setup
for p in lab_persona_clinician lab_persona_pii_reader lab_persona_outsider; do
  databricks bundle run -t "$TARGET" "${VARS[@]}" --params "suite_run_id=$SUITE" "$p" || echo "WARN: $p reported an error; see the job run"
done
databricks bundle run -t "$TARGET" "${VARS[@]}" --params "suite_run_id=$SUITE" lab_collect
databricks bundle run -t "$TARGET" "${VARS[@]}" --params "suite_run_id=$SUITE" lab_audit
echo "Done (suite $SUITE). Open the dashboard 'GenAI governance lab – results'. Tomorrow: databricks bundle run -t $TARGET lab_evidence"
