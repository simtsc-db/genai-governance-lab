# Databricks notebook source
# MAGIC %md
# MAGIC # 10 · Unity Catalog policies (DEMO mode)
# MAGIC Applies the controls the deck describes in **Layer 2**, all on the sandbox schema:
# MAGIC
# MAGIC 1. **Column masks** on `nric` and `patient_name`: cleartext only for the PII readers group (D-01).
# MAGIC 2. **Row filter** on `facility`: one facility unless you're in the all-facilities group (D-02).
# MAGIC 3. **ABAC**: the system classification tag `class.phone_number` on `phone` plus one schema-level mask policy (D-03).
# MAGIC 4. **ABAC for Singapore identifiers**: one schema-level mask policy for every column that Data Classification
# MAGIC    tags `class.sg_nric`, `class.sg_fin` or `class.sg_work_permit_number` (D-08).
# MAGIC 5. **Grants**: `USE SCHEMA` on the lab and tools schemas (run time, so no identity is needed at deploy time); personas can read `patient_visits` and the masked notes, never the raw notes (D-04); each persona
# MAGIC    can write only its own results staging table.
# MAGIC
# MAGIC Outcomes go to `lab_setup_status`, which the persona probes read to tell FAIL from "not set up".
# MAGIC Everything created or granted is recorded in `lab_manifest`.

# COMMAND ----------
# MAGIC %md Pinned dependencies (one place for every notebook: `src/constraints.txt`).

# COMMAND ----------
# MAGIC %pip install -q -c ../constraints.txt databricks-sdk pyyaml

# COMMAND ----------
dbutils.library.restartPython()

# COMMAND ----------
import os, sys
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
from checks import Manifest, q, qp, sql_str, principal, tag_key, suite_run_id, first_line, lab_names, name

dbutils.widgets.text("catalog", "main")
dbutils.widgets.text("schema", "genie_governance_lab")
dbutils.widgets.text("resource_prefix", "lab")
N = lab_names(dbutils.widgets.get("resource_prefix"))
MASK_FN, FILTER_FN, ABAC_FN = N["mask_fn"], N["filter_fn"], N["abac_mask_fn"]
for g in ("clinicians_group", "pii_readers_group", "all_facilities_group", "outsiders_group"):
    dbutils.widgets.text(g, "")
dbutils.widgets.text("pii_tag_key", "class.phone_number")
dbutils.widgets.text("pii_tag_value", "")
dbutils.widgets.text("nric_tag_keys", "class.sg_nric,class.sg_fin,class.sg_work_permit_number")
TAG, TAGV = tag_key(dbutils.widgets.get("pii_tag_key")), dbutils.widgets.get("pii_tag_value")
if TAGV:
    tag_key(TAGV)
SG_TAGS = [tag_key(t.strip()) for t in dbutils.widgets.get("nric_tag_keys").split(",") if t.strip()]
C, S = name(dbutils.widgets.get("catalog"), "catalog"), name(dbutils.widgets.get("schema"), "schema")
T = f"{q(C)}.{q(S)}"
G_CLIN, G_PII, G_ALL, G_OUT = (principal(dbutils.widgets.get(g), g) for g in
                               ("clinicians_group", "pii_readers_group", "all_facilities_group", "outsiders_group"))
PERSONA_GROUP = {"clinician": G_CLIN, "pii_reader": G_PII, "outsider": G_OUT}
RUN = suite_run_id(dbutils)
man = Manifest(spark, C, S, "10_uc_policies", RUN)
legacy = {r.key: r.value for r in spark.table(f"{T}.lab_setup_status").collect()} \
    if spark.catalog.tableExists(f"{C}.{S}.lab_setup_status") else {}
status = {}

def run(key, sql, args=None):
    try:
        spark.sql(sql, args=args) if args else spark.sql(sql)
        status[key] = "ok"
    except Exception as e:  # keep going; the persona probes report SKIP with this reason
        status[key] = f"error: {first_line(e)}"
    print(key, "->", status[key])
    return status[key] == "ok"

# COMMAND ----------
# MAGIC %md ## 1–2 · Column masks and row filter (classic UC functions)

# COMMAND ----------
if run("mask_fn", f"""CREATE OR REPLACE FUNCTION {T}.{q(MASK_FN)}(v STRING) RETURNS STRING
  COMMENT 'Cleartext for the PII readers group, otherwise masked'
  RETURN CASE WHEN is_account_group_member({sql_str(G_PII)}) THEN v ELSE concat('*****', right(v, 1)) END"""):
    man.add("function", f"{C}.{S}.{MASK_FN}")
if run("filter_fn", f"""CREATE OR REPLACE FUNCTION {T}.{q(FILTER_FN)}(f STRING) RETURNS BOOLEAN
  COMMENT 'All facilities for the all-facilities group, otherwise Polyclinic North only'
  RETURN is_account_group_member({sql_str(G_ALL)}) OR f = 'Polyclinic North'"""):
    man.add("function", f"{C}.{S}.{FILTER_FN}")
run("mask_nric", f"ALTER TABLE {T}.patient_visits ALTER COLUMN nric SET MASK {T}.{q(MASK_FN)}")
run("mask_name", f"ALTER TABLE {T}.patient_visits ALTER COLUMN patient_name SET MASK {T}.{q(MASK_FN)}")
run("row_filter", f"ALTER TABLE {T}.patient_visits SET ROW FILTER {T}.{q(FILTER_FN)} ON (facility)")

# COMMAND ----------
# MAGIC %md ## 3 · ABAC: governed tag + one policy for every tagged column
# MAGIC Governed tags are account-level objects. If the lab can't create one (no permission, or the
# MAGIC account's tag limit is reached), set the bundle variables `pii_tag_key` / `pii_tag_value` to an
# MAGIC existing governed tag and rerun this task. System tags (`class.*`) are never created or deleted.

# COMMAND ----------
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.tags import TagPolicy, Value
w = WorkspaceClient()
try:
    existing = {t.tag_key for t in w.tag_policies.list_tag_policies()}
    if TAG not in existing and not TAG.startswith("class."):
        w.tag_policies.create_tag_policy(TagPolicy(tag_key=TAG, description="GenAI governance lab: PII class",
                                                   values=[Value(name=TAGV)] if TAGV else None))
        man.add("governed_tag", TAG)
    elif legacy.get("governed_tag_created_by_lab") == TAG:
        man.add("governed_tag", TAG, {"adopted_from": "lab_setup_status"})
    status["governed_tag"] = "ok"
except Exception as e:
    status["governed_tag"] = f"error: {first_line(e)}"
print("governed_tag ->", status["governed_tag"])

MATCH = f"has_tag_value('{TAG}', '{TAGV}')" if TAGV else f"has_tag('{TAG}')"
if run("abac_mask_fn", f"""CREATE OR REPLACE FUNCTION {T}.{q(ABAC_FN)}(v STRING) RETURNS STRING
  RETURN concat('*****', right(v, 1))"""):
    man.add("function", f"{C}.{S}.{ABAC_FN}")
run("tag_phone", f"ALTER TABLE {T}.patient_visits ALTER COLUMN phone SET TAGS ('{TAG}' = '{TAGV}')" if TAGV
    else f"ALTER TABLE {T}.patient_visits ALTER COLUMN phone SET TAGS ('{TAG}')")
if run("abac_policy", f"""CREATE OR REPLACE POLICY {q(N["abac_policy"])} ON SCHEMA {T}
  COMMENT 'GenAI governance lab: mask every column tagged {TAG}, except for PII readers'
  COLUMN MASK {T}.{q(ABAC_FN)}
  TO `account users` EXCEPT {qp(G_PII)}
  FOR TABLES
  MATCH COLUMNS {MATCH} AS c
  ON COLUMN c"""):
    man.add("policy", f"{C}.{S}:{N['abac_policy']}")

# COMMAND ----------
# MAGIC %md ## 4 · ABAC for Singapore identifiers (Data Classification tags)
# MAGIC Data Classification tags NRIC, FIN and work permit numbers with `class.sg_nric`, `class.sg_fin` and
# MAGIC `class.sg_work_permit_number` (SG-region workspaces). One policy masks every such column in the schema.
# MAGIC Data Classification needs cross-Geo processing in the Asia Geo (Designated Services), so with in-Geo
# MAGIC enforcement on it is unavailable and tags nothing. The documented fallback is to apply the same tags by
# MAGIC hand, so the lab sets `class.sg_nric` **manually** on one column of a separate table,
# MAGIC `sg_identifiers_simulated`, and labels it SIMULATED everywhere (table comment, manifest, D-08 evidence).
# MAGIC If Unity Catalog refuses to set the system tag by hand, nothing is faked and D-08 reports that.

# COMMAND ----------
SG_MATCH = " OR ".join(f"has_tag('{t}')" for t in SG_TAGS)
sg_ok = run("abac_sg_policy", f"""CREATE OR REPLACE POLICY {q(N["sg_policy"])} ON SCHEMA {T}
  COMMENT 'GenAI governance lab: mask every column Data Classification tags as a Singapore NRIC, FIN or work permit number, except for PII readers'
  COLUMN MASK {T}.{q(ABAC_FN)}
  TO `account users` EXCEPT {qp(G_PII)}
  FOR TABLES
  MATCH COLUMNS {SG_MATCH} AS c
  ON COLUMN c""")
if sg_ok:
    man.add("policy", f"{C}.{S}:{N['sg_policy']}")
else:  # older runtimes: one policy per tag key
    status["abac_sg_policy_combined"] = status["abac_sg_policy"]
    oks = []
    for i, t in enumerate(SG_TAGS):
        pname = f"{N['sg_policy']}_{i + 1}"
        if run(f"abac_sg_policy_{i + 1}", f"""CREATE OR REPLACE POLICY {q(pname)} ON SCHEMA {T}
              COMMENT 'GenAI governance lab: mask columns tagged {t}, except for PII readers'
              COLUMN MASK {T}.{q(ABAC_FN)} TO `account users` EXCEPT {qp(G_PII)}
              FOR TABLES MATCH COLUMNS has_tag('{t}') AS c ON COLUMN c"""):
            man.add("policy", f"{C}.{S}:{pname}")
            oks.append(pname)
    status["abac_sg_policy"] = "ok" if len(oks) == len(SG_TAGS) else status["abac_sg_policy"]

if run("sg_sim_table", f"""CREATE OR REPLACE TABLE {T}.sg_identifiers_simulated
    COMMENT 'SIMULATED classification: the lab set class.sg_nric on nric by hand because Data Classification is not available here. Synthetic data.'
    AS SELECT visit_id, nric FROM {T}.patient_visits WHERE visit_id <= 20"""):
    man.add("table", f"{C}.{S}.sg_identifiers_simulated")
    if run("sg_tag_simulated", f"ALTER TABLE {T}.sg_identifiers_simulated ALTER COLUMN nric SET TAGS ('{SG_TAGS[0]}')"):
        man.add("tag_assignment", f"{C}.{S}.sg_identifiers_simulated.nric", {"tag": SG_TAGS[0], "simulated": True})

# COMMAND ----------
# MAGIC %md ## 5 · Grants
# MAGIC `USE CATALOG` is granted only where a group doesn't already hold it, and recorded, so teardown revokes
# MAGIC only what the lab added. Each persona gets SELECT + MODIFY on **its own** staging results table only.

# COMMAND ----------
def direct_privs(securable: str, obj: str) -> set:
    return {(r.Principal, r.ActionType.replace("_", " ").upper())
            for r in spark.sql(f"SHOW GRANTS ON {securable} {obj}").collect()}

# Earlier lab versions recorded USE CATALOG grants without checking they were new: drop those unproven claims.
spark.sql(f"""DELETE FROM {T}.lab_manifest WHERE kind = 'catalog_grant'
              AND get_json_object(detail, '$.adopted_from') IS NOT NULL""")
# USE SCHEMA on the lab and tools schemas. Demo/customer targets also grant it in the bundle (no-op here); the
# workspace target deploys without any identity, so this run-time grant is what gives the personas schema access.
for sch in (S, f"{S}_tools"):
    try:
        held = direct_privs("SCHEMA", f"{q(C)}.{q(sch)}")
    except Exception as e:
        status[f"use_schema_{sch}"] = f"error: {first_line(e)}"; continue
    for p, g in PERSONA_GROUP.items():
        if (g, "USE SCHEMA") not in held and (g, "ALL PRIVILEGES") not in held:
            run(f"use_schema_{sch}_{p}", f"GRANT USE SCHEMA ON SCHEMA {q(C)}.{q(sch)} TO {qp(g)}")
cat_privs = direct_privs("CATALOG", q(C))
for p, g in PERSONA_GROUP.items():
    held = {a for (pr, a) in cat_privs if pr == g}
    if not held & {"USE CATALOG", "ALL PRIVILEGES"}:
        if run(f"use_catalog_{p}", f"GRANT USE CATALOG ON CATALOG {q(C)} TO {qp(g)}") and \
                (g, "USE CATALOG") in direct_privs("CATALOG", q(C)):
            # proof: absent before this run's GRANT, present after
            man.add("catalog_grant", f"{C}|{g}|USE CATALOG", {"confirmed_absent_before": True})
    elif man.has("catalog_grant", f"{C}|{g}|USE CATALOG"):
        status[f"use_catalog_{p}"] = "ok (added by an earlier run of this lab version, recorded)"
    else:
        status[f"use_catalog_{p}"] = "already held; not recorded (teardown keeps it)"
    run(f"select_visits_{p}", f"GRANT SELECT ON TABLE {T}.patient_visits TO {qp(g)}")
    run(f"results_staging_{p}", f"GRANT SELECT, MODIFY ON TABLE {T}.test_results_{p} TO {qp(g)}")
    run(f"probe_log_{p}", f"GRANT SELECT, MODIFY ON TABLE {T}.probe_log_{p} TO {qp(g)}")
    # Earlier lab versions let every persona MODIFY the shared results table; take that back.
    run(f"revoke_shared_results_{p}", f"REVOKE SELECT, MODIFY ON TABLE {T}.test_results FROM {qp(g)}")
    for other, og in PERSONA_GROUP.items():
        if other != p and og != g:
            run(f"revoke_staging_{other}_from_{p}", f"REVOKE MODIFY ON TABLE {T}.test_results_{other} FROM {qp(g)}")
            run(f"revoke_probe_log_{other}_from_{p}", f"REVOKE MODIFY ON TABLE {T}.probe_log_{other} FROM {qp(g)}")

# Keep keys written by later steps (e.g. genie_space_id) so a rerun updates the same Genie Agent.
for k, v in legacy.items():
    if not k.startswith(("use_catalog_", "select_status_", "modify_results_", "select_setup_status_")):
        status.setdefault(k, v)
spark.createDataFrame([(k, str(v)) for k, v in status.items()], "key STRING, value STRING") \
     .write.mode("overwrite").saveAsTable(f"{T}.lab_setup_status")
man.add("table", f"{C}.{S}.lab_setup_status")
for p, g in PERSONA_GROUP.items():
    run(f"select_setup_status_{p}", f"GRANT SELECT ON TABLE {T}.lab_setup_status TO {qp(g)}")
display(spark.table(f"{T}.lab_setup_status"))
