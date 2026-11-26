#!/usr/bin/env bash
# Create the lab personas as ACCOUNT-level service principals and groups, and assign them
# to the workspace.
#
# Usage: scripts/bootstrap_personas.sh [--adopt] [--target <bundle-target>] <account-profile> <workspace-id> [prefix] [deployer-email]
#   --target         bundle target these personas are for (default: demo). Recorded with the workspace ID in
#                    .lab.env; teardown.sh only deletes identities recorded for its own target and workspace.
#   account-profile  Databricks CLI profile for the account console (account admin)
#   workspace-id     account-level workspace ID (Account console > Workspaces)
#   prefix           name prefix for the personas and groups (default: lab). Use a unique prefix
#                    in a shared account so the lab never reuses or deletes someone else's objects.
#   deployer-email   user who deploys the bundle; gets the Service Principal User role on each
#                    persona so jobs can run_as them (default: the account profile's user)
#   --adopt          reuse service principals / groups with the same display name that this script did
#                    NOT create. Without it the script refuses. Adopted identities are never deleted by
#                    scripts/teardown.sh.
# Every identity this script creates carries the marker externalId "genie-governance-lab:<name>" and is
# recorded in .lab.env (LAB_CREATED_SP_IDS / LAB_CREATED_GROUP_IDS). Rerunning reuses marked identities.
# teardown.sh deletes only recorded identities that still carry the marker.
# Optional env WORKSPACE_PROFILE: workspace CLI profile; if set, the personas also get the
# 'workspace-access' and 'databricks-sql-access' entitlements (needed to run jobs and use Genie).
#
# No account admin? Ask your identity team to create the same three service principals and four
# groups through your IdP / SCIM, then write .lab.env by hand (see .lab.env.example).
set -euo pipefail
ADOPT=0; LAB_TARGET=demo
while [ $# -gt 0 ]; do
  case "$1" in
    --adopt) ADOPT=1; shift ;;
    --target) LAB_TARGET="${2:?target}"; shift 2 ;;
    *) break ;;
  esac
done
ACC="${1:?account profile}"; WS="${2:?workspace id}"; PFX="${3:-lab}"; DEPLOYER="${4:-}"
# same validators as the notebooks (src/lib/checks.py)
python3 "$(dirname "$0")/validate_names.py" "target=$LAB_TARGET" "prefix=$PFX" "workspace_id=$WS" || exit 2
cd "$(dirname "$0")/.."
a() { databricks -p "$ACC" account "$@"; }
MARK="genie-governance-lab:"   # technical marker: keep it even though the display name is "GenAI governance lab"
SPS=("${PFX}-clinician" "${PFX}-pii-reader" "${PFX}-outsider")
GROUPS_=("${PFX}_clinicians" "${PFX}_pii_readers" "${PFX}_all_facilities" "${PFX}_outsiders")

lookup() {  # kind name -> "id appId|- externalId|-" or empty
  a "$1" list --filter "displayName eq \"$2\"" -o json | python3 -c '
import json,sys
x=json.load(sys.stdin)
if len(x) > 1: sys.exit("more than one %s named %s; resolve by hand" % (sys.argv[1], sys.argv[2]))
if x: print(x[0]["id"], x[0].get("applicationId") or "-", x[0].get("externalId") or "-")' "$1" "$2"
}

# Preflight: decide for every name BEFORE creating anything.
refuse=()
check() {  # kind name
  local r; r=$(lookup "$1" "$2")
  if [ -n "$r" ] && [ "$(echo "$r" | awk '{print $3}')" != "$MARK$2" ] && [ "$ADOPT" = 0 ]; then refuse+=("$1 $2"); fi
}
for n in "${SPS[@]}"; do check service-principals "$n"; done
for n in "${GROUPS_[@]}"; do check groups "$n"; done
if [ ${#refuse[@]} -gt 0 ]; then
  echo "Refusing: these already exist and were not created by this script (no lab marker):" >&2
  printf '  %s\n' "${refuse[@]}" >&2
  echo "Use another prefix, or pass --adopt to reuse them (teardown will then never delete them)." >&2
  exit 1
fi

CREATED_SP=(); CREATED_G=()
sp() {  # name -> "id appId externalId"; creates with the marker if missing
  local n="$1" r; r=$(lookup service-principals "$1")
  if [ -z "$r" ]; then
    r=$(a service-principals create --display-name "$n" --external-id "$MARK$n" -o json | python3 -c 'import json,sys;x=json.load(sys.stdin);print(x["id"], x["applicationId"], x.get("externalId") or "-")')
    echo "created service principal $n" >&2
  fi
  echo "$r"
}
grp() {
  local n="$1" r; r=$(lookup groups "$1")
  if [ -z "$r" ]; then
    r=$(a groups create --display-name "$n" --external-id "$MARK$n" -o json | python3 -c 'import json,sys;x=json.load(sys.stdin);print(x["id"], "-", x.get("externalId") or "-")')
    echo "created group $n" >&2
  fi
  echo "$r"
}
add_member() {  # group_id principal_id
  a groups patch "$1" --json "{\"schemas\":[\"urn:ietf:params:scim:api:messages:2.0:PatchOp\"],\"Operations\":[{\"op\":\"add\",\"path\":\"members\",\"value\":[{\"value\":\"$2\"}]}]}" >/dev/null
}
assign() {  # principal_id
  a workspace-assignment update "$WS" "$1" --json '{"permissions":["USER"]}' >/dev/null
}

read -r CLIN_ID CLIN_APP CLIN_X <<<"$(sp "${SPS[0]}")"
read -r PII_ID PII_APP PII_X <<<"$(sp "${SPS[1]}")"
read -r OUT_ID OUT_APP OUT_X <<<"$(sp "${SPS[2]}")"
read -r G_CLIN _ G_CLIN_X <<<"$(grp "${GROUPS_[0]}")"
read -r G_PII _ G_PII_X <<<"$(grp "${GROUPS_[1]}")"
read -r G_ALL _ G_ALL_X <<<"$(grp "${GROUPS_[2]}")"
read -r G_OUT _ G_OUT_X <<<"$(grp "${GROUPS_[3]}")"
# Recorded as lab-created only if they carry the marker (created now or by an earlier run of this script).
[ "$CLIN_X" = "$MARK${SPS[0]}" ] && CREATED_SP+=("$CLIN_ID")
[ "$PII_X" = "$MARK${SPS[1]}" ] && CREATED_SP+=("$PII_ID")
[ "$OUT_X" = "$MARK${SPS[2]}" ] && CREATED_SP+=("$OUT_ID")
[ "$G_CLIN_X" = "$MARK${GROUPS_[0]}" ] && CREATED_G+=("$G_CLIN")
[ "$G_PII_X" = "$MARK${GROUPS_[1]}" ] && CREATED_G+=("$G_PII")
[ "$G_ALL_X" = "$MARK${GROUPS_[2]}" ] && CREATED_G+=("$G_ALL")
[ "$G_OUT_X" = "$MARK${GROUPS_[3]}" ] && CREATED_G+=("$G_OUT")

add_member "$G_CLIN" "$CLIN_ID"
add_member "$G_PII" "$PII_ID"; add_member "$G_ALL" "$PII_ID"
add_member "$G_OUT" "$OUT_ID"
for p in "$CLIN_ID" "$PII_ID" "$OUT_ID" "$G_CLIN" "$G_PII" "$G_ALL" "$G_OUT"; do assign "$p"; done

# Let the deployer bind the personas as job run_as identities (Service Principal User role).
python3 - "$ACC" "$DEPLOYER" "$CLIN_APP" "$PII_APP" "$OUT_APP" <<'PY'
import sys
from databricks.sdk import AccountClient
from databricks.sdk.service.iam import GrantRule, RuleSetUpdateRequest
acc, deployer, *apps = sys.argv[1:]
a = AccountClient(profile=acc)
if not deployer:
    deployer = a.config.username or ""
if not deployer:
    sys.exit("Pass the deployer email as the 4th argument")
for app in apps:
    name = f"accounts/{a.config.account_id}/servicePrincipals/{app}/ruleSets/default"
    rs = a.access_control.get_rule_set(name=name, etag="")
    rules = list(rs.grant_rules or [])
    who = f"users/{deployer}"
    if not any(r.role == "roles/servicePrincipal.user" and who in (r.principals or []) for r in rules):
        rules.append(GrantRule(role="roles/servicePrincipal.user", principals=[who]))
        a.access_control.update_rule_set(name=name, rule_set=RuleSetUpdateRequest(name=name, etag=rs.etag, grant_rules=rules))
    print("servicePrincipal.user for", deployer, "on", app[:8] + "…")
PY

if [ -n "${WORKSPACE_PROFILE:-}" ]; then
  python3 - "$WORKSPACE_PROFILE" "$CLIN_APP" "$PII_APP" "$OUT_APP" <<'PY'
import sys
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.iam import Patch, PatchOp, PatchSchema
prof, *apps = sys.argv[1:]
w = WorkspaceClient(profile=prof)
for app in apps:
    sp = next(iter(w.service_principals.list(filter=f'applicationId eq "{app}"')), None)
    if not sp:
        print("not yet visible in the workspace:", app[:8] + "…"); continue
    w.service_principals.patch(sp.id, schemas=[PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP],
        operations=[Patch(op=PatchOp.ADD, path="entitlements",
                          value=[{"value": "workspace-access"}, {"value": "databricks-sql-access"}])])
    print("entitlements set for", sp.display_name)
PY
else
  echo "WORKSPACE_PROFILE not set: give the three personas 'Workspace access' and 'Databricks SQL access' in the workspace admin settings."
fi

cat > .lab.env <<EOF
# Generated by scripts/bootstrap_personas.sh. Not committed.
CLINICIAN_SP=$CLIN_APP
PII_READER_SP=$PII_APP
OUTSIDER_SP=$OUT_APP
LAB_PREFIX=$PFX
# Identities created by bootstrap_personas.sh (carry the lab marker); teardown.sh deletes only these.
LAB_CREATED_SP_IDS=$(IFS=,; echo "${CREATED_SP[*]:-}")
LAB_CREATED_GROUP_IDS=$(IFS=,; echo "${CREATED_G[*]:-}")
LAB_IDENTITY_TARGET=$LAB_TARGET
LAB_IDENTITY_WORKSPACE_ID=$WS
CLINICIANS_GROUP=${PFX}_clinicians
PII_READERS_GROUP=${PFX}_pii_readers
ALL_FACILITIES_GROUP=${PFX}_all_facilities
OUTSIDERS_GROUP=${PFX}_outsiders
ACCOUNT_PROFILE=$ACC
WORKSPACE_ID=$WS
EOF
echo "Personas ready. Application IDs written to .lab.env"
