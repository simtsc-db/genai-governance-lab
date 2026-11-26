# Databricks notebook source
# MAGIC %md
# MAGIC # 00 · Synthetic data (DEMO mode)
# MAGIC Creates the sandbox tables and registers the suite run. **All data is synthetic**: patients are
# MAGIC obviously fictional labels (`Test Patient 017`), and NRIC-like identifiers and phone numbers are generated.
# MAGIC * NRIC-like values match the probes' pattern `[STFGM]` + 7 digits + letter, but use check letters
# MAGIC   (K, L, M, N, P, Q, R, U, V, W, Y) that are **never valid** for the `S` prefix, so none is a real NRIC.
# MAGIC * Phone numbers start with `5`, a level that IMDA's National Numbering Plan reserves, so none can be dialled
# MAGIC   in Singapore. They still match the lab's phone pattern `[35689]` + 7 digits.
# MAGIC
# MAGIC | Table | Purpose |
# MAGIC |---|---|
# MAGIC | `patient_visits` | 2,000 visits across 4 facilities, with NRIC, patient label and phone |
# MAGIC | `clinical_notes_raw` | free-text notes that contain PII (no one but the deployer can read it) |
# MAGIC | `test_results`, `test_results_<persona>` | results (admin) and per-persona staging tables |
# MAGIC | `suite_runs`, `test_catalogue` | suite registry and the expected results, for the dashboard |
# MAGIC | `lab_manifest` | every object the lab created; teardown deletes only these |

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
from checks import RESULTS_DDL, PROBE_LOG_DDL, PERSONAS, Manifest, q, register_suite, suite_run_id

dbutils.widgets.text("catalog", "main")
dbutils.widgets.text("schema", "genie_governance_lab")
C, S = dbutils.widgets.get("catalog"), dbutils.widgets.get("schema")
T = f"{q(C)}.{q(S)}"
RUN = suite_run_id(dbutils)
register_suite(spark, C, S, RUN, "demo")
man = Manifest(spark, C, S, "00_setup_data", RUN)
print("suite run:", RUN)

# COMMAND ----------
spark.sql(f"""
CREATE OR REPLACE TABLE {T}.patient_visits
COMMENT 'Synthetic polyclinic and hospital visits for the GenAI governance lab. Fictional patient labels; NRICs have invalid check letters.'
AS SELECT
  id AS visit_id,
  concat('S', lpad(cast(1000000 + (id * 7919) % 8999999 AS STRING), 7, '0'),
         substr('KLMNPQRUVWY', cast(id % 11 AS INT) + 1, 1))                         AS nric,
  concat('Test Patient ', lpad(cast(id % 97 + 1 AS STRING), 3, '0'))                AS patient_name,
  concat('5', lpad(cast((id * 104729) % 9999999 AS STRING), 7, '0'))                 AS phone,
  element_at(array('Polyclinic North','Polyclinic East','General Hospital West',
                   'Community Hospital Central'), cast(id % 4 AS INT) + 1)            AS facility,
  element_at(array('Type 2 diabetes','Hypertension','Asthma','Hyperlipidaemia',
                   'Chronic kidney disease'), cast(id % 5 AS INT) + 1)                AS diagnosis,
  date_add(DATE'2026-01-01', cast(id % 300 AS INT))                                   AS visit_date,
  cast(40 + (id * 37) % 45 AS INT)                                                    AS age
FROM range(1, 2001) AS t(id)
""")

spark.sql(f"""
CREATE OR REPLACE TABLE {T}.clinical_notes_raw
COMMENT 'Synthetic free-text notes WITH PII. Only the deployer can read this table.'
AS SELECT
  v.visit_id, v.facility, v.visit_date,
  concat('Patient ', v.patient_name, ' (NRIC ', v.nric, ', tel ', v.phone, ') seen at ',
         v.facility, ' for ', lower(v.diagnosis),
         '. Advised follow-up in 3 months; contact next of kin on ', v.phone, ' if needed.') AS note
FROM {T}.patient_visits v
WHERE v.visit_id <= 200
""")

# Results: admin table plus one staging table per persona (grants in 10_uc_policies).
spark.sql(RESULTS_DDL.format(table=f"{T}.test_results"))
for p in PERSONAS:
    spark.sql(RESULTS_DDL.format(table=f"{T}.test_results_{p}"))
    spark.sql(PROBE_LOG_DDL.format(table=f"{T}.probe_log_{p}"))   # B-probe intent/creation log, per persona
for t in ("patient_visits", "clinical_notes_raw", "test_results", "suite_runs", "test_catalogue", "lab_manifest",
          *(f"test_results_{p}" for p in PERSONAS), *(f"probe_log_{p}" for p in PERSONAS)):
    man.add("table", f"{C}.{S}.{t}")
display(spark.sql(f"SELECT facility, count(*) AS visits FROM {T}.patient_visits GROUP BY facility ORDER BY facility"))
