# Databricks notebook source
# MAGIC %md
# MAGIC # 00 · Run the GenAI governance lab (workspace-native, no CLI)
# MAGIC One notebook drives the lab after the bundle is deployed from this Git folder (**Deploy**, target `workspace`;
# MAGIC see `docs/workspace-setup.md`). Pick an **action**, fill the widgets, run all cells.
# MAGIC
# MAGIC | action | what it does |
# MAGIC |---|---|
# MAGIC | `preflight` | checks you are a workspace admin, the catalog, warehouse and deployment, the identity plan; prints what to confirm manually |
# MAGIC | `personas` | creates the persona service principals and account groups (or records the ones you give in `manual_*`) |
# MAGIC | `run` | lab_setup -> 3 persona jobs (run_as bound to the personas first) -> collect -> audit, one suite run ID |
# MAGIC | `resume` | continues the suite `run_id` from the first step that didn't succeed |
# MAGIC | `results` | latest suite with MISSING / PENDING spelled out, and the dashboard link |
# MAGIC | `evidence` | next day: correlates the suite with audit logs, gateway usage and the inference table |
# MAGIC | `teardown-plan` | what teardown would remove (changes nothing) |
# MAGIC | `teardown-apply` | export -> lab_teardown -> lab identities -> this deployment's jobs, dashboard, model service, schemas |
# MAGIC
# MAGIC `mode=audit` runs only the read-only audit (no personas). Every value is validated before use; nothing here
# MAGIC defaults to a specific workspace, catalog, warehouse or identity.

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
from runner import ACTIONS, MODES, WIDGETS, ConfigError, Runner, Stop, parse_config

for k, d, label in WIDGETS:
    if k == "action":
        dbutils.widgets.dropdown(k, d, list(ACTIONS), label)
    elif k == "mode":
        dbutils.widgets.dropdown(k, d, list(MODES), label)
    else:
        dbutils.widgets.text(k, d, label)

# COMMAND ----------
from databricks.sdk import WorkspaceClient
import databricks.sdk
try:
    cfg = parse_config({k: dbutils.widgets.get(k) for k, _, _ in WIDGETS})
except ConfigError as e:
    raise RuntimeError(f"STOP: {e}") from None
w = WorkspaceClient()
lab = Runner(w, spark, cfg, display=display)
print(f"action={cfg.action} mode={cfg.mode} deployment={cfg.full_schema} resource_prefix={cfg.resource_prefix} "
      f"persona_prefix={cfg.persona_prefix} sdk={databricks.sdk.version.__version__}")

# COMMAND ----------
import json
try:
    if cfg.action == "preflight":
        out = lab.preflight()
    elif cfg.action == "personas":
        out = lab.setup_personas()
    elif cfg.action == "run":
        out = {"suite_run_id": lab.run()}
    elif cfg.action == "resume":
        out = {"suite_run_id": lab.run(resume_id=cfg.run_id)}
    elif cfg.action == "results":
        out = lab.results()
    elif cfg.action == "evidence":
        out = lab.evidence()
    elif cfg.action == "teardown-plan":
        out = lab.teardown(apply=False)
    elif cfg.action == "teardown-apply":
        out = lab.teardown(apply=True)
except Stop as e:
    # An expected stop condition with a clear reason: the cell (and a job run of this notebook) fails with it.
    raise RuntimeError(f"STOP ({cfg.action}): {e}") from None
dbutils.notebook.exit(json.dumps({"action": cfg.action, "ok": True, "result": out}, default=str)[:30000])
