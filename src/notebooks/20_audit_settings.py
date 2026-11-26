# Databricks notebook source
# MAGIC %md
# MAGIC # 20 · Audit workspace settings (AUDIT mode, read-only)
# MAGIC Reads settings that the deck covers in Layers 1, 4 and 6 and records them in `test_results`.
# MAGIC It **changes nothing** outside the lab's results schema. Account-level settings (cross-Geo enforcement,
# MAGIC partner-powered AI enforcement, budgets, serverless egress) are read by `scripts/account_checks.py`
# MAGIC from your laptop with an account-admin profile, or checked by hand (docs/manual-steps.md).

# COMMAND ----------
# MAGIC %md Pinned dependencies (one place for every notebook: `src/constraints.txt`).

# COMMAND ----------
# MAGIC %pip install -q -c ../constraints.txt databricks-sdk pyyaml

# COMMAND ----------
dbutils.library.restartPython()

# COMMAND ----------
import os, sys, json
def _lab_lib():  # src/lib next to this notebook, in a Git folder or in the bundle's deployed files
    here = [os.getcwd()]
    try:
        nb = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
        here.append(os.path.dirname(nb if nb.startswith("/Workspace") else "/Workspace" + nb))
    except Exception:
        pass
    for d in (os.path.normpath(os.path.join(h, "..", "lib")) for h in here):
        if os.path.isfile(os.path.join(d, "checks.py")):
            return d
    raise RuntimeError(f"src/lib/checks.py not found next to this notebook (looked in {here})")
sys.path.insert(0, _lab_lib())
from checks import Recorder, name, q, register_suite, suite_run_id, first_line
from databricks.sdk import WorkspaceClient

for k, d in [("catalog", "main"), ("schema", "genie_governance_lab"), ("warehouse_id", ""),
             ("expected_region", "ap-southeast-1"), ("max_token_days", "90"), ("mode", "audit")]:
    dbutils.widgets.text(k, d)
C, S = name(dbutils.widgets.get("catalog")), name(dbutils.widgets.get("schema"))
WH, REGION = dbutils.widgets.get("warehouse_id"), dbutils.widgets.get("expected_region")
MAX_DAYS = int(dbutils.widgets.get("max_token_days") or 90)
MODE = dbutils.widgets.get("mode") if dbutils.widgets.get("mode") in ("audit", "demo") else "audit"
if WH:
    name(WH, "warehouse_id")
T = f"{q(C)}.{q(S)}"
RUN = suite_run_id(dbutils)
register_suite(spark, C, S, RUN, MODE)
w = WorkspaceClient()
rec = Recorder(spark, C, S, persona="admin", run_id=RUN)

def safe(fn, default=None):
    try:
        return fn()
    except Exception as e:
        return {"error": first_line(e)} if default is None else default

def failed(v):
    return isinstance(v, dict) and "error" in v

# COMMAND ----------
# P-01 region
region = safe(lambda: w.metastores.summary().region)
if failed(region):
    rec.record("P-01", "MANUAL", "Couldn't read metastore summary", region)
elif REGION:
    rec.record("P-01", "PASS" if region == REGION else "FAIL", f"region={region}, expected {REGION}")
else:
    rec.record("P-01", "INFO", f"region={region}")

# P-02 IP access lists: unreadable -> MANUAL (not FAIL)
conf = safe(lambda: w.workspace_conf.get_status(keys="enableIpAccessLists,enableTokensConfig,maxTokenLifetimeDays"))
lists = safe(lambda: [l.as_dict() for l in w.ip_access_lists.list()])
if failed(conf) or failed(lists):
    rec.record("P-02", "MANUAL", "Couldn't read the IP access list settings (needs workspace admin); check Settings > Security",
               {"conf": conf if failed(conf) else "ok", "lists": lists if failed(lists) else "ok"})
else:
    allow = [l for l in lists if l.get("list_type") == "ALLOW" and l.get("enabled")]
    ip_on = str(conf.get("enableIpAccessLists", "")).lower() == "true"
    rec.record("P-02", "PASS" if ip_on and allow else "FAIL",
               f"enabled={ip_on}, enabled allow lists={len(allow)}", {"labels": [l.get("label") for l in lists]})

# P-03 tokens: check the actual maximum lifetime value
if failed(conf):
    rec.record("P-03", "MANUAL", "Couldn't read token settings (needs workspace admin)", conf)
else:
    tokens_on = str(conf.get("enableTokensConfig", "true")).lower() == "true"
    raw = conf.get("maxTokenLifetimeDays")
    try:
        days = int(str(raw)) if raw not in (None, "", "null") else None
    except ValueError:
        days = None
    if not tokens_on:
        rec.record("P-03", "PASS", "PATs disabled")
    elif days is None or days <= 0:
        rec.record("P-03", "FAIL", f"PATs enabled with no maximum lifetime (maxTokenLifetimeDays={raw!r})")
    else:
        rec.record("P-03", "PASS" if days <= MAX_DAYS else "FAIL",
                   f"PATs enabled, maximum lifetime {days} days (limit for this check: {MAX_DAYS})", {"max_token_days": days})

# P-04 serverless egress (account level)
rec.record("P-04", "MANUAL", "Account console > Security > Network policies; or scripts/account_checks.py")

# P-05 partner-powered AI (workspace value)
ppai = safe(lambda: w.settings.llm_proxy_partner_powered_workspace.get().as_dict())
rec.record("P-05", "INFO" if not failed(ppai) else "MANUAL",
           f"workspace value={ppai.get('boolean_val', {}).get('value') if not failed(ppai) else 'unreadable'}", ppai)

# P-06 cross-Geo enforcement: look for a geo-related workspace setting; account value via account_checks.py
meta = safe(lambda: [m.as_dict() for m in w.workspace_settings_v2.list_workspace_settings_metadata()], [])
geo = [m for m in meta if "geo" in json.dumps(m).lower()]
snapshot = {"readable_workspace_settings": [m.get("name") for m in meta]}
if geo:
    vals = {m.get("name"): safe(lambda n=m.get("name"): w.workspace_settings_v2.get_public_workspace_setting(n).as_dict()) for m in geo}
    rec.record("P-06", "INFO", f"geo-related settings: {list(vals)}", {**vals, **snapshot})
else:
    rec.record("P-06", "MANUAL", "Account-level setting. Run scripts/account_checks.py or check Account console > Settings", snapshot)

# P-08 Compliance Security Profile
csp = safe(lambda: w.settings.compliance_security_profile.get().as_dict())
csp_on = bool(csp.get("compliance_security_profile_workspace", {}).get("is_enabled")) if not failed(csp) else None
rec.record("P-08", "INFO", f"CSP enabled={csp_on}", csp)
# Share the CSP state with the platform probe (it switches D-07's expectation).
spark.sql(f"CREATE TABLE IF NOT EXISTS {T}.lab_setup_status (key STRING, value STRING)")
spark.sql(f"DELETE FROM {T}.lab_setup_status WHERE key = 'csp_enabled'")
spark.sql(f"INSERT INTO {T}.lab_setup_status VALUES ('csp_enabled', :v)", args={"v": str(csp_on)})

# C-01 statement timeout on the Genie warehouse. Documented field: statement_timeout (seconds, 0-172800; 0 = none)
# on the warehouse (Create/Update warehouse API). Precedence: session > warehouse > workspace.
if WH:
    whd = safe(lambda: w.api_client.do("GET", f"/api/2.0/sql/warehouses/{WH}"))
    wcfg = safe(lambda: w.warehouses.get_workspace_warehouse_config().as_dict(), {})
    params = {p.get("key", ""): p.get("value") for p in (wcfg.get("sql_configuration_parameters", {}) or {}).get("configuration_pairs", [])}
    ws_default = params.get("STATEMENT_TIMEOUT")
    effective = safe(lambda: w.statement_execution.execute_statement(
        warehouse_id=WH, statement="SET STATEMENT_TIMEOUT", wait_timeout="30s").result.data_array)
    if failed(whd):
        rec.record("C-01", "MANUAL", "Couldn't read the warehouse", whd)
    else:
        ev = {"warehouse": whd.get("name"), "statement_timeout_field": whd.get("statement_timeout", "absent"),
              "workspace_STATEMENT_TIMEOUT": ws_default, "SET STATEMENT_TIMEOUT (effective, informational)": effective}
        if "statement_timeout" not in whd:
            rec.record("C-01", "MANUAL", f"warehouse {whd.get('name')}: the API response has no statement_timeout field, so "
                       "the warehouse setting can't be verified; check it in the warehouse settings", ev)
        elif int(whd.get("statement_timeout") or 0) > 0:
            rec.record("C-01", "PASS", f"warehouse {whd.get('name')}: statement_timeout={whd['statement_timeout']} s", ev)
        else:
            rec.record("C-01", "FAIL", f"warehouse {whd.get('name')}: statement_timeout=0 (no warehouse-level timeout)", ev)
else:
    rec.record("C-01", "SKIP", "warehouse_id not given")
rec.record("C-02", "MANUAL", "Budgets are account-level: scripts/account_checks.py lists them (inventory only) or Account console > Usage > Budgets")
rec.flush()
