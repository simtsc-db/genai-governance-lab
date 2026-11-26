#!/usr/bin/env python3
"""Read-only account-level checks (deck Layers 1 and 6). Needs an account-admin CLI profile.

Usage: python3 scripts/account_checks.py <account-profile> [workspace-id]
Prints a table and writes evidence/account_checks.json. Changes nothing.

Read-only INVENTORY (lists values, doesn't judge them): P-04 serverless network policies, P-05
partner-powered AI (account value + enforce), P-06 geo-related account settings, P-08 CSP and ESM
account defaults, C-02 budgets and budget (usage) policies.
NOT covered: context-based ingress (manual step M-02) and the account-level Genie One switch (M-06).
"""
import json
import os
import sys

from databricks.sdk import AccountClient

prof = sys.argv[1] if len(sys.argv) > 1 else sys.exit(__doc__)
ws_id = sys.argv[2] if len(sys.argv) > 2 else None
a = AccountClient(profile=prof)
out = {}


def safe(key, fn):
    try:
        v = fn()
        out[key] = v.as_dict() if hasattr(v, "as_dict") else v
    except Exception as e:  # report, don't fail
        out[key] = {"error": str(e).splitlines()[0][:300]}


safe("P-05 partner-powered AI (account)", lambda: a.settings.llm_proxy_partner_powered_account.get())
safe("P-05 partner-powered AI enforce", lambda: a.settings.llm_proxy_partner_powered_enforce.get())
safe("P-08 CSP account default", lambda: a.settings.csp_enablement_account.get())
safe("P-08 ESM account default", lambda: a.settings.esm_enablement_account.get())
safe("settings metadata", lambda: [m.as_dict() for m in a.settings_v2.list_account_settings_metadata()])
meta = out.get("settings metadata") if isinstance(out.get("settings metadata"), list) else []
for m in meta:
    name = m.get("name", "")
    if "geo" in json.dumps(m).lower():
        safe(f"P-06 {name}", lambda n=name: a.settings_v2.get_public_account_setting(n))
safe("P-04 network policies", lambda: [p.as_dict() for p in a.network_policies.list_network_policies_rpc()])
if ws_id:
    safe("P-04 workspace network option", lambda: a.workspace_network_configuration.get_workspace_network_option_rpc(int(ws_id)))
safe("C-02 budgets", lambda: [b.as_dict() for b in a.budgets.list()])
safe("C-02 budget (usage) policies", lambda: [b.as_dict() for b in a.budget_policy.list()])

os.makedirs("evidence", exist_ok=True)
with open("evidence/account_checks.json", "w") as f:
    json.dump(out, f, indent=1, default=str)
for k, v in out.items():
    if k == "settings metadata":
        continue
    s = json.dumps(v, default=str)
    print(f"{k:42} {s[:150]}{'…' if len(s) > 150 else ''}")
print("\nFull output: evidence/account_checks.json")
