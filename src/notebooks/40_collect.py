# Databricks notebook source
# MAGIC %md
# MAGIC # 40 · Collect persona results (DEMO mode, trusted)
# MAGIC Runs as the deployer. Each persona writes only to its own staging table `test_results_<persona>`
# MAGIC (only that persona's group has MODIFY on it), so no persona can change another persona's results.
# MAGIC This step copies staging rows into `test_results`, keeping only rows whose `persona` matches the
# MAGIC staging table. From each persona's probe log (`probe_log_<persona>`, written BEFORE every B-probe create) it
# MAGIC records every probe object whose deletion wasn't verified in `lab_manifest`, so teardown removes it. A name
# MAGIC must equal the exact per-run name for that persona, kind and run ID, so a persona can't point teardown elsewhere.

# COMMAND ----------
import os, sys, json, re
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
from checks import PERSONAS, Manifest, q, name, suite_run_id, expected_probe_name, lab_names

dbutils.widgets.text("catalog", "main")
dbutils.widgets.text("schema", "genie_governance_lab")
dbutils.widgets.text("resource_prefix", "lab")
N = lab_names(dbutils.widgets.get("resource_prefix"))
C, S = name(dbutils.widgets.get("catalog")), name(dbutils.widgets.get("schema"))
T = f"{q(C)}.{q(S)}"
RUN = suite_run_id(dbutils)
man = Manifest(spark, C, S, "40_collect", RUN)
TOOLS = f"{C}.{S}_tools"

# COMMAND ----------
report = {}
for p in PERSONAS + ("facilitator",):
    st = f"{T}.test_results_{p}"
    if not spark.catalog.tableExists(f"{C}.{S}.test_results_{p}"):
        continue
    bad = spark.sql(f"SELECT count(*) AS n FROM {st} WHERE persona <> :p", args={"p": p}).first().n
    spark.sql(f"""MERGE INTO {T}.test_results t
                  USING (SELECT * FROM {st} WHERE persona = :p) s
                  ON t.run_id = s.run_id AND t.test_id = s.test_id AND t.persona = s.persona AND t.ts = s.ts
                  WHEN NOT MATCHED THEN INSERT *""", args={"p": p})
    n = spark.sql(f"SELECT count(*) AS n FROM {st} WHERE persona = :p", args={"p": p}).first().n
    report[p] = {"rows": n, "rejected_wrong_persona": bad}
    # Probe objects for teardown, from the persona's probe log (latest state per object)
    log = f"{T}.probe_log_{p}"
    if spark.catalog.tableExists(f"{C}.{S}.probe_log_{p}"):
        latest = spark.sql(f"""SELECT * FROM {log} QUALIFY row_number() OVER (PARTITION BY kind, name ORDER BY ts DESC) = 1""").collect()
        pending, rejected = 0, 0
        for r in latest:
            if r.state in ("deleted_verified", "create_failed"):
                continue      # verified gone, or never created
            if r.name != expected_probe_name(r.kind, p, r.rid, TOOLS, r.test_id, N["probe_endpoint"]):
                rejected += 1; print("REJECTED probe record (not this persona's per-run name):", p, r.kind, r.name)
                continue
            man.add("probe_object", f"{r.kind}|{r.name}", {"source": f"probe_log_{p}", "state": r.state, "run_id": r.run_id})
            pending += 1
        report[p].update({"probe_objects_pending": pending, "probe_records_rejected": rejected})
print(json.dumps(report, indent=1))
dbutils.notebook.exit(json.dumps(report))
