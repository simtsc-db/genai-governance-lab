# Shared helpers for the lab scripts (sourced, not executed).
# Strict patterns for anything that becomes a SQL identifier or a CLI argument: the SAME validators the notebooks
# and 00_run_lab use (src/lib/checks.py), through scripts/validate_names.py.
_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
validate() {     # kind=value ...
  python3 "$_LIB_DIR/validate_names.py" "$@" || exit 2
}
valid_name() {   # value what   (catalog / schema / warehouse id)
  case "$2" in catalog) validate "catalog=$1" ;; schema) validate "schema=$1" ;; "warehouse id") validate "warehouse_id=$1" ;;
               *) validate "name=$1" ;; esac
}
valid_target() { validate "target=$1"; }
new_suite_id() { echo "suite-$(date -u +%Y%m%dT%H%M%SZ)-$(od -An -N3 -tx1 /dev/urandom | tr -d ' \n')"; }
# Resolved bundle config for a target as JSON (exit non-zero if validation fails).
bundle_json() {   # warnings are shown only if validation fails
  local err rc=0 out; err=$(mktemp)
  out=$(databricks bundle validate -t "$1" "${@:2}" -o json 2>"$err") || rc=$?
  [ "$rc" -eq 0 ] || cat "$err" >&2
  rm -f "$err"; [ "$rc" -eq 0 ] && printf '%s\n' "$out"
}
# Field from the resolved config: profile | catalog | schema | mode
bundle_field() {   # field target [vars...]
  local f="$1"; shift
  bundle_json "$@" | python3 -c '
import json,sys
b=json.load(sys.stdin); f=sys.argv[1]
r=b.get("resources",{})
if f=="profile": print(b["workspace"].get("profile",""))
elif f=="catalog": print(r["schemas"]["lab"]["catalog_name"])
elif f=="schema": print(r["schemas"]["lab"]["name"])
elif f=="mode":
    jobs=set(r.get("jobs",{})); other={k for k in r if k not in ("jobs","schemas","dashboards")}
    if jobs=={"lab_audit"} and not other and set(r.get("schemas",{}))=={"lab"} and not r["schemas"]["lab"].get("grants"):
        print("audit")
    elif r.get("model_services") or any(j.startswith("lab_persona_") for j in jobs): print("demo")
    else: print("unknown")' "$f"
}
bundle_profile() { bundle_field profile "$@"; }
workspace_id() {  # profile -> numeric workspace ID, from the server's x-databricks-org-id header (authoritative;
                  # a locally configured workspace_id can be stale). Fails if it can't be read.
  python3 -c '
import sys, requests
from databricks.sdk import WorkspaceClient as W
w = W(profile=sys.argv[1] or None)
r = requests.get(w.config.host.rstrip("/") + "/api/2.0/preview/scim/v2/Me", headers=w.config.authenticate(), timeout=30)
org = r.headers.get("x-databricks-org-id")
if r.status_code != 200 or not (org or "").isdigit():
    sys.exit(f"could not read the workspace ID (HTTP {r.status_code})")
print(org)' "$1"
}
