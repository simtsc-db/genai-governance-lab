#!/usr/bin/env bash
# Fail if any file that would be shared (tracked, or new and not gitignored) contains internal information.
# Generic patterns live here (e-mail addresses, go/ and Slack links, ticket keys, workspace hosts and IDs,
# notes about a build environment). Literal internal names (CLI profiles, endpoint names, prefixes) must NOT
# be committed, so they are loaded from the gitignored file local/shareable_patterns.txt (one extended regex
# per line, # comments allowed); see local.example/shareable_patterns.txt.
# Exit codes: 0 clean, 1 forbidden content found, 2 scanner error (also a failure).
# Usage: scripts/check_shareable.sh [--require-local]   (--require-local: fail if the local list is missing)
set -euo pipefail
cd "$(dirname "$0")/.."
SELF="scripts/check_shareable.sh"
LOCAL="local/shareable_patterns.txt"
PATTERNS=(
  'ka-[0-9a-f]{8}-endpoint'
  '[A-Za-z0-9._%+-]+@databricks\.com'
  '(^|[^A-Za-z0-9.])go/[A-Za-z0-9_-]+'
  'slack\.com' 'slack-[a-z0-9]+\.'
  '\b[A-Z][A-Z0-9]{1,9}-[0-9]{3,}\b'
  '[a-z0-9-]+\.cloud\.databricks\.com' 'azuredatabricks\.net' 'gcp\.databricks\.com'
  '\b[0-9]{15,17}\b' '\b[0-9a-f]{16}\b'
  '[Bb]uild[- ]workspace' 'Build-and-test'
)
NLOCAL=0
if [ -f "$LOCAL" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    PATTERNS+=("$line"); NLOCAL=$((NLOCAL + 1))
  done < "$LOCAL"
elif [ "${1:-}" = "--require-local" ]; then
  echo "check_shareable: $LOCAL missing (--require-local)" >&2; exit 2
else
  echo "check_shareable: note: $LOCAL not found; only the generic patterns are checked" >&2
fi
FILES=()
if ! LIST=$(git ls-files --cached --others --exclude-standard); then
  echo "check_shareable: git ls-files failed" >&2; exit 2
fi
while IFS= read -r f; do
  [ "$f" = "$SELF" ] && continue
  [ -f "$f" ] && FILES+=("$f")
done <<<"$LIST"
[ ${#FILES[@]} -gt 0 ] || { echo "check_shareable: no files to check" >&2; exit 2; }
fail=0
for p in "${PATTERNS[@]}"; do
  rc=0; hits=$(grep -n -I -E -- "$p" "${FILES[@]}") || rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "FORBIDDEN pattern /$p/:"; echo "$hits" | sed 's/^/  /' | head -20; fail=1
  elif [ "$rc" -ne 1 ]; then
    echo "check_shareable: grep error (exit $rc) for pattern /$p/" >&2; exit 2
  fi
done
if [ "$fail" = 1 ]; then echo "check_shareable: FAILED (${#FILES[@]} files checked)"; exit 1; fi
echo "check_shareable: clean (${#FILES[@]} files, ${#PATTERNS[@]} patterns, $NLOCAL from $LOCAL)"
