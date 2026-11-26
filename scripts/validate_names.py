#!/usr/bin/env python3
"""The CLI scripts' name validation, using the SAME validators as the notebooks and 00_run_lab (src/lib/checks.py).

Usage: scripts/validate_names.py kind=value [kind=value ...]
  kinds: catalog, schema, warehouse_id, name, target, prefix (resource or persona prefix), workspace_id,
         suite_id, principal, app_id
Exit 0 if every value is valid; exit 2 with one line per problem otherwise.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "lib"))
from checks import app_id, name, prefix_ok, principal, run_id_ok, target_ok, workspace_id_ok  # noqa: E402

VALIDATORS = {
    "catalog": lambda v: name(v, "catalog"), "schema": lambda v: name(v, "schema"),
    "warehouse_id": lambda v: name(v, "warehouse id"), "name": lambda v: name(v, "name"),
    "target": target_ok, "prefix": lambda v: prefix_ok(v, "prefix"), "workspace_id": workspace_id_ok,
    "suite_id": run_id_ok, "principal": principal, "app_id": app_id,
}


def main(argv) -> int:
    problems = []
    for arg in argv:
        kind, sep, value = arg.partition("=")
        if not sep or kind not in VALIDATORS:
            problems.append(f"usage: kind=value with kind in {', '.join(sorted(VALIDATORS))} (got {arg!r})")
            continue
        try:
            VALIDATORS[kind](value)
        except ValueError as e:
            problems.append(str(e))
    for p in problems:
        print(p, file=sys.stderr)
    return 2 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
