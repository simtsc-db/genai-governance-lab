# Databricks notebook source
# MAGIC %md
# MAGIC # 16 · Genie Agent (DEMO mode)
# MAGIC Creates (or updates) the lab Genie Agent over `patient_visits` and `clinical_notes_masked`
# MAGIC and sets its permissions, the controls from the deck's **Layer 4a**:
# MAGIC
# MAGIC * clinicians and PII readers: **CAN RUN**
# MAGIC * outsiders: **no permission** (test A-03)
# MAGIC
# MAGIC Note what is *not* configured here: no masking, filtering or PII rules. Genie runs SQL as the
# MAGIC asking user, so the Unity Catalog policies from step 10 do all of that.

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
from checks import Manifest, name, q, principal, suite_run_id, genie_space_owned, has_proof, LAB_GENIE_TITLE, LAB_GENIE_DESCRIPTION
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.iam import AccessControlRequest, PermissionLevel

for k, d in [("catalog", "main"), ("schema", "genie_governance_lab"), ("warehouse_id", ""),
             ("clinicians_group", ""), ("pii_readers_group", "")]:
    dbutils.widgets.text(k, d)
C, S, WH = (dbutils.widgets.get(k) for k in ("catalog", "schema", "warehouse_id"))
name(C, "catalog"); name(S, "schema"); name(WH, "warehouse_id")
TITLE = LAB_GENIE_TITLE   # fixed: ownership checks compare the exact (title, description) pair
T = f"{q(C)}.{q(S)}"
w = WorkspaceClient()
man = Manifest(spark, C, S, "16_genie_agent", suite_run_id(dbutils))

status = {r.key: r.value for r in spark.table(f"{T}.lab_setup_status").collect()}
# The masked notes are attached only if 15_masked_notes validated them (no NRIC, phone or name left).
NOTES_OK = status.get("notes_masking_check") == "ok"
tables = ([{"identifier": f"{C}.{S}.clinical_notes_masked"}] if NOTES_OK else []) + [{"identifier": f"{C}.{S}.patient_visits"}]
print("masked notes attached:", NOTES_OK, "| check:", status.get("notes_masking_check"))
serialized = json.dumps({
    "version": 2,
    "data_sources": {"tables": tables},
    "config": {"sample_questions": [
        {"id": "a1b2c3d4e5f60718293a4b5c6d7e8f90", "question": ["List 5 diabetes patients with their NRIC, name and facility"]},
        {"id": "b1b2c3d4e5f60718293a4b5c6d7e8f90", "question": ["How many visits per facility?"]}]}})

# Only a space whose title AND description are exactly a pair a lab version set (LAB_GENIE_PAIRS) is updated in
# place or adopted. A recorded space that no longer matches is not touched: its manifest row is marked unproven
# (teardown lists it for manual review) and a new lab space is created.
def detail_of(m):
    try:
        d = json.loads(m.get("detail") or "{}")
        return d if isinstance(d, dict) else {}
    except (TypeError, ValueError):
        return {}

space_id, keep_title = None, None
for m in reversed(man.items("genie_space")):
    if detail_of(m).get("unproven"):
        continue
    try:
        sp = w.genie.get_space(m["object_id"])
    except Exception as e:
        print("recorded space not readable:", m["object_id"], str(e)[:200])
        continue
    if genie_space_owned(m.get("detail"), sp.title, sp.description):
        space_id = sp.space_id
        ct = detail_of(m).get("created_title")
        keep_title = sp.title if ct and sp.title == ct else None
        break
    why = ("recorded without creation proof (e.g. adopted by an earlier lab version)" if not has_proof(m.get("detail"))
           else "title/description is not a lab pair or the title recorded at creation")
    man.mark_row_unproven("genie_space", m["object_id"], m.get("detail"), f"{why}; not updated or trashed by the lab")
    print(f"recorded space {m['object_id']} ({sp.title!r}): {why}: not updated, listed for manual review at teardown")
# A space ID that only lab_setup_status names (earlier lab versions) has no independent creation provenance: its
# title/description pair can be copied by anyone, so it is never adopted. It is listed for manual review instead.
legacy_id = status.get("genie_space_id")
if legacy_id and not man.has("genie_space", legacy_id):
    print(f"Genie space {legacy_id} is named only in lab_setup_status (no creation proof): not adopted, not changed; "
          "review it by hand. A new lab space is created.")
if space_id:
    try:
        # in place, same space ID: also brings an existing space to the current title and description
        # (a space whose title the workspace made unique at creation keeps that exact recorded title)
        w.genie.update_space(space_id, serialized_space=serialized, title=keep_title or TITLE,
                             description=LAB_GENIE_DESCRIPTION, warehouse_id=WH)
    except Exception as e:
        print("could not update", space_id, "-", str(e)[:200], "; creating a new one")
        space_id = None
if not space_id:
    space_id = w.genie.create_space(warehouse_id=WH, serialized_space=serialized, title=TITLE,
        description=LAB_GENIE_DESCRIPTION).space_id
    # The workspace may make the title unique (timestamp) if another deployment uses it: record what it really is.
    created_title = w.genie.get_space(space_id).title
    man.record_created("genie_space", space_id, {"title": TITLE, "created_title": created_title})
    if created_title != TITLE:
        print(f"the workspace titled the new space {created_title!r} (title already in use); recorded as created")
print("Genie Agent:", space_id)

acl = [AccessControlRequest(group_name=principal(dbutils.widgets.get(g)), permission_level=PermissionLevel.CAN_RUN)
       for g in ("clinicians_group", "pii_readers_group") if dbutils.widgets.get(g)]
w.permissions.update("genie", space_id, access_control_list=acl)

spark.sql(f"DELETE FROM {T}.lab_setup_status WHERE key = 'genie_space_id'")
spark.sql(f"INSERT INTO {T}.lab_setup_status VALUES ('genie_space_id', :v)", args={"v": space_id})
host = w.config.host.rstrip("/")
displayHTML(f'<a href="{host}/genie/rooms/{space_id}">Open the lab Genie Agent</a>')
