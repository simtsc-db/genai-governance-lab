#!/usr/bin/env bash
# Release check (see scripts/check_release.py for what it scans). Run before publishing:
#   scripts/check_release.sh --range <revs-you-will-push>   every commit's tree and metadata, e.g. --range HEAD
#   scripts/check_release.sh                                HEAD's committed tree (warns if the working tree differs)
#   scripts/check_release.sh --worktree                     the working copy, before committing
# Exit codes: 0 clean, 1 findings, 2 usage or scanner error.
set -euo pipefail
exec python3 "$(dirname "$0")/check_release.py" "$@"
