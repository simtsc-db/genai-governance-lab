#!/usr/bin/env bash
# Export the latest suite run (same rows as the dashboard, including MISSING / PENDING) to
# evidence/results-<target>-<suite>.csv (+ evidence/results-<target>-<suite>.meta.json) for the sign-off pack.
# Usage: scripts/export_results.sh <target> <warehouse-id> [catalog]
# The schema (and catalog) are resolved from the bundle target, the same way teardown and the jobs see them.
# Writes evidence/results-<target>-<suite>.csv (never overwrites: adds -2, -3, ...) and a .meta.json (workspace,
# target, catalog, schema, suite, rows, sha256), which teardown.sh accepts as a verified prior export.
set -euo pipefail
TARGET="${1:?target}"; WH="${2:?warehouse id}"; CAT_ARG="${3:-}"
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib.sh
source scripts/lib.sh
valid_target "$TARGET"; valid_name "$WH" "warehouse id"
VARS=(--var "warehouse_id=$WH"); [ -n "$CAT_ARG" ] && valid_name "$CAT_ARG" catalog && VARS+=(--var "catalog=$CAT_ARG")
CAT=$(bundle_field catalog "$TARGET" "${VARS[@]}"); SCH=$(bundle_field schema "$TARGET" "${VARS[@]}")
valid_name "$CAT" catalog; valid_name "$SCH" schema
PROFILE=$(bundle_profile "$TARGET" "${VARS[@]}")
P=(); [ -n "$PROFILE" ] && P=(-p "$PROFILE")
WS_ID=$(workspace_id "$PROFILE")
mkdir -p evidence
OUT_DIR="evidence"
# Catalog and schema go in the request's catalog/schema fields (not into the SQL text).
BODY=$(python3 - "$WH" "$CAT" "$SCH" <<'PY'
import json, sys
wh, cat, sch = sys.argv[1:]
d = json.load(open("src/dashboards/results.lvdash.json"))
sql = "".join(d["datasets"][0]["queryLines"]) + "\nORDER BY layer, test_id, persona"
print(json.dumps({"warehouse_id": wh, "catalog": cat, "schema": sch, "statement": sql,
                  "wait_timeout": "50s", "format": "JSON_ARRAY", "disposition": "INLINE"}))
PY
)
databricks ${P[@]+"${P[@]}"} api post /api/2.0/sql/statements --json "$BODY" | python3 -c '
import csv, hashlib, json, os, sys
r = json.load(sys.stdin)
if r["status"]["state"] != "SUCCEEDED": sys.exit("query failed: " + json.dumps(r["status"]))
cols = [c["name"] for c in r["manifest"]["schema"]["columns"]]
rows = r["result"].get("data_array", []) if r.get("result") else []
if not rows: sys.exit("query returned no rows; nothing exported")
out_dir, target, cat, sch, ws = sys.argv[1:6]
suites = sorted({row[cols.index("suite_run_id")] for row in rows if row[cols.index("suite_run_id")]})
base = os.path.join(out_dir, f"results-{target}-{suites[0] if len(suites) == 1 else 'mixed'}")
out, n = base + ".csv", 1
while os.path.exists(out):          # never overwrite an earlier export
    n += 1; out = f"{base}-{n}.csv"
with open(out, "w", newline="") as f:
    w = csv.writer(f); w.writerow(cols); w.writerows(rows)
meta = {"workspace_id": ws, "target": target, "catalog": cat, "schema": sch, "suite_run_ids": suites, "rows": len(rows),
        "sha256": hashlib.sha256(open(out, "rb").read()).hexdigest()}
json.dump(meta, open(out[:-4] + ".meta.json", "w"), indent=1)
print("wrote", out, f"({len(rows)} rows, suite {suites})")' "$OUT_DIR" "$TARGET" "$CAT" "$SCH" "$WS_ID"
