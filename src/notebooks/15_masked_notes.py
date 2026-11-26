# Databricks notebook source
# MAGIC %md
# MAGIC # 15 · Masked copy of the clinical notes (DEMO mode)
# MAGIC Pattern from the deck: **mask free text before an AI surface can see it.** `ai_mask` 1.0
# MAGIC (Public Preview, HIPAA compliant) writes `clinical_notes_masked`. That copy is the only notes
# MAGIC table the personas and the Genie Agent can read; `clinical_notes_raw` stays private.
# MAGIC
# MAGIC If `ai_mask` isn't available, a regex fallback is used. The notes are masked into a **private candidate
# MAGIC table** first and checked for leftover NRICs, phone numbers, patient labels and empty text. Only a clean
# MAGIC candidate replaces `clinical_notes_masked` and gets grants; otherwise nobody gets access, Genie doesn't attach
# MAGIC the notes and the vector index isn't built (`notes_masking_check` in `lab_setup_status`).

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
from checks import Manifest, PHONE_SQL, note_issue, q, qp, principal, suite_run_id, first_line

dbutils.widgets.text("catalog", "main")
dbutils.widgets.text("schema", "genie_governance_lab")
C, S = dbutils.widgets.get("catalog"), dbutils.widgets.get("schema")
T = f"{q(C)}.{q(S)}"
man = Manifest(spark, C, S, "15_masked_notes", suite_run_id(dbutils))

import uuid
CAND_NAME = f"clinical_notes_masked_candidate_{uuid.uuid4().hex[:8]}"   # unique per run, recorded before creation
CAND = f"{T}.{q(CAND_NAME)}"                       # private: no grants, never attached to Genie or an index
man.add("table", f"{C}.{S}.{CAND_NAME}", {"candidate": True})
n_seed = spark.table(f"{T}.clinical_notes_raw").count()
if n_seed == 0:
    raise RuntimeError("clinical_notes_raw is empty: nothing to mask and validate")

def leaks(table):
    """Rows of `table` that still contain an NRIC, phone number or patient label, or have no note text."""
    rows = spark.table(table).select("visit_id", "note").collect()
    return [x for x in (note_issue(r.visit_id, r.note) for r in rows) if x]

REGEX_SQL = f"""
    CREATE OR REPLACE TABLE {CAND}
    COMMENT 'Candidate: notes masked by regex (ai_mask unavailable or incomplete). Private until validated.'
    AS SELECT visit_id, facility, visit_date,
              regexp_replace(regexp_replace(regexp_replace(note,
                 '[STFGM][0-9]{{7}}[A-Z]', '[NRIC]'), '{PHONE_SQL}', '[PHONE]'),
                 'Patient [^(]+\\\\(', 'Patient [NAME] (') AS note
    FROM {T}.clinical_notes_raw"""

# COMMAND ----------
# 1. Build the candidate (private). 2. Validate it. 3. Only then replace clinical_notes_masked and grant.
method = "ai_mask 1.0"
try:
    spark.sql(f"""
    CREATE OR REPLACE TABLE {CAND}
    COMMENT 'Candidate: notes masked by ai_mask 1.0. Private until validated.'
    AS SELECT visit_id, facility, visit_date,
              ai_mask(note, array('person name', 'national identity card number', 'phone number')) AS note
    FROM {T}.clinical_notes_raw""")
    bad = leaks(CAND)
    if bad:
        method = f"regex fallback (ai_mask left PII or empty text in {len(bad)} rows)"
        spark.sql(REGEX_SQL)
except Exception as e:
    method = f"regex fallback (ai_mask failed: {first_line(e, 200)})"
    spark.sql(REGEX_SQL)
print("masking method:", method)
bad = leaks(CAND)
n_raw = n_seed
n_cand = spark.table(CAND).count()
check = ("ok" if not bad and n_cand == n_raw else
         f"error: {len(bad)} rows still contain PII or empty text (e.g. visit_ids {[b['visit_id'] for b in bad[:5]]}); "
         f"{n_cand} of {n_raw} rows")
print("masking check:", check)

# COMMAND ----------
# Same readers as patient_visits (granted in 10_uc_policies), and only after the check passed.
groups = []
for row in spark.sql(f"SHOW GRANTS ON TABLE {T}.patient_visits").collect():
    if row.ActionType == "SELECT" and row.Principal not in groups:
        groups.append(principal(row.Principal))
if check == "ok":
    spark.sql(f"""CREATE OR REPLACE TABLE {T}.clinical_notes_masked
                  COMMENT 'Validated masked notes ({method.split(" (")[0]}): no NRIC, phone number or patient name'
                  AS SELECT * FROM {CAND}""")
    man.add("table", f"{C}.{S}.clinical_notes_masked")
    for g in groups:
        spark.sql(f"GRANT SELECT ON TABLE {T}.clinical_notes_masked TO {qp(g)}")
    print("validated notes published; SELECT granted to:", groups)
elif spark.catalog.tableExists(f"{C}.{S}.clinical_notes_masked"):
    for g in groups:
        spark.sql(f"REVOKE SELECT ON TABLE {T}.clinical_notes_masked FROM {qp(g)}")
    print("NOT published: candidate failed the leak check; access to the existing masked table revoked")
spark.sql(f"DROP TABLE IF EXISTS {CAND}")
man.remove("table", f"{C}.{S}.{CAND_NAME}")

spark.sql(f"DELETE FROM {T}.lab_setup_status WHERE key IN ('notes_masking', 'notes_masking_check')")
spark.sql(f"INSERT INTO {T}.lab_setup_status VALUES ('notes_masking', :m), ('notes_masking_check', :c)",
          args={"m": method, "c": check})
if check == "ok":
    display(spark.sql(f"SELECT note FROM {T}.clinical_notes_masked LIMIT 5"))
