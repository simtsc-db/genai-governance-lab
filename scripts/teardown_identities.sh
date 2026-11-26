#!/usr/bin/env bash
# Delete the persona identities that scripts/bootstrap_personas.sh recorded as created for ONE target and
# workspace. Separate and retryable: needs only .lab.env and the account profile, not the bundle.
# Usage: scripts/teardown_identities.sh [--dry-run] <target> <workspace-id>
# Rules:
#   * only IDs in LAB_CREATED_SP_IDS / LAB_CREATED_GROUP_IDS, and only if LAB_IDENTITY_TARGET and
#     LAB_IDENTITY_WORKSPACE_ID match the arguments;
#   * an identity is deleted only if its externalId is exactly "genie-governance-lab:<displayName>";
#   * an account GET that fails for any reason other than "not found" (expired auth, 5xx, network) is UNKNOWN:
#     the record is kept and the script fails;
#   * after DELETE the identity must be gone (GET -> not found);
#   * .lab.env keeps every record that wasn't deleted and verified, so you can rerun.
set -euo pipefail
DRY=0
if [ "${1:-}" = "--dry-run" ]; then DRY=1; shift; fi
TARGET="${1:?target}"; WS="${2:?workspace id}"
cd "$(dirname "$0")/.."
python3 scripts/validate_names.py "target=$TARGET" "workspace_id=$WS" || exit 2   # same validators as the notebooks
[ -f .lab.env ] || { echo "no .lab.env: nothing recorded" >&2; exit 0; }
# shellcheck disable=SC1091
source .lab.env
[ -n "${ACCOUNT_PROFILE:-}" ] || { echo "No ACCOUNT_PROFILE in .lab.env: remove identities through your identity process."; exit 0; }
if [ "${LAB_IDENTITY_TARGET:-}" != "$TARGET" ] || [ "${LAB_IDENTITY_WORKSPACE_ID:-}" != "$WS" ]; then
  echo "Identities in .lab.env are recorded for target '${LAB_IDENTITY_TARGET:-none}' / workspace '${LAB_IDENTITY_WORKSPACE_ID:-none}',"
  echo "not '$TARGET' / '$WS': nothing deleted."
  exit 0
fi
MARK="genie-governance-lab:"   # technical marker: keep it even though the display name is "GenAI governance lab"

state_of() {  # kind id -> "present <displayName> <externalId>" | "gone" | "unknown <reason>"
  local out rc=0
  out=$(databricks -p "$ACCOUNT_PROFILE" account "$1" get "$2" -o json 2>&1) || rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "$out" | python3 -c 'import json,sys;x=json.load(sys.stdin);print("present\t" + json.dumps(x.get("displayName","")) + "\t" + json.dumps(x.get("externalId") or ""))' \
      || echo "unknown unparseable-response"
  elif echo "$out" | grep -qiE 'does not exist|not found|RESOURCE_DOES_NOT_EXIST|status code 404'; then
    echo "gone"
  else
    echo "unknown $(echo "$out" | head -1 | tr -d '\n' | cut -c1-120)"
  fi
}

failed=0; KEEP_SP=(); KEEP_G=()
for kind in service-principals groups; do
  if [ "$kind" = groups ]; then list="${LAB_CREATED_GROUP_IDS:-}"; else list="${LAB_CREATED_SP_IDS:-}"; fi
  for id in ${list//,/ }; do
    keep=1
    if [[ "$id" =~ ^[0-9]+$ ]]; then
      st=$(state_of "$kind" "$id")
      case "$st" in
        gone) echo "already gone: $kind $id"; keep=0 ;;
        present*)
          IFS=$'\t' read -r _ dn ext <<<"$st"
          if python3 -c 'import json,sys;sys.exit(0 if json.loads(sys.argv[2])==sys.argv[3]+json.loads(sys.argv[1]) else 1)' "$dn" "$ext" "$MARK"; then
            if [ "$DRY" = 1 ]; then echo "would delete $kind $id $dn"
            elif databricks -p "$ACCOUNT_PROFILE" account "$kind" delete "$id" >/dev/null 2>&1 && [ "$(state_of "$kind" "$id")" = gone ]; then
              echo "deleted (verified) $kind $id $dn"; keep=0
            else echo "FAILED to delete or verify $kind $id $dn" >&2; failed=$((failed + 1)); fi
          else
            echo "KEEP $kind $id $dn: externalId $ext is not exactly the lab marker" >&2; failed=$((failed + 1))
          fi ;;
        *) echo "UNKNOWN $kind $id ($st): record kept" >&2; failed=$((failed + 1)) ;;
      esac
    else
      echo "invalid recorded id '$id': kept" >&2; failed=$((failed + 1))
    fi
    if [ "$keep" = 1 ]; then if [ "$kind" = groups ]; then KEEP_G+=("$id"); else KEEP_SP+=("$id"); fi; fi
  done
done

if [ "$DRY" = 0 ]; then   # rewrite only the two record lines; everything else in .lab.env stays
  python3 - "$(IFS=,; echo "${KEEP_SP[*]:-}")" "$(IFS=,; echo "${KEEP_G[*]:-}")" <<'PY'
import re, sys
sp, g = sys.argv[1:]
s = open(".lab.env").read()
s = re.sub(r"(?m)^LAB_CREATED_SP_IDS=.*$", f"LAB_CREATED_SP_IDS={sp}", s)
s = re.sub(r"(?m)^LAB_CREATED_GROUP_IDS=.*$", f"LAB_CREATED_GROUP_IDS={g}", s)
open(".lab.env", "w").write(s)
PY
fi
if [ "$failed" -gt 0 ]; then echo "$failed identity record(s) unresolved; kept in .lab.env. Fix and rerun." >&2; exit 1; fi
echo "identity cleanup complete"
