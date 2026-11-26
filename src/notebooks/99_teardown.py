# Databricks notebook source
# MAGIC %md
# MAGIC # 99 · Teardown
# MAGIC Deletes **only what the lab recorded in `lab_manifest`**: the Genie Agent, vector index, experiment,
# MAGIC connections it proved it created, functions, policies, tables, a governed tag the lab created, the `USE CATALOG` and
# MAGIC `USE CONNECTION` grants it added, and the ACL entries it added to existing endpoints (only those entries; the
# MAGIC rest of each ACL is kept as it is now). Probe objects from B-01 … B-05 and B-04b are deleted by their recorded,
# MAGIC pattern-checked names.
# MAGIC
# MAGIC * `dry_run=true` (default for `scripts/teardown.sh --dry-run`) lists what would be deleted and changes nothing.
# MAGIC * If any deletion fails the task **fails**, so `scripts/teardown.sh` stops before `bundle destroy`.
# MAGIC
# MAGIC `databricks bundle destroy` then removes the schemas, jobs, model service and dashboard;
# MAGIC `scripts/teardown.sh` removes the persona identities that bootstrap recorded as created by it.

# COMMAND ----------
# MAGIC %md Pinned dependencies (one place for every notebook: `src/constraints.txt`).

# COMMAND ----------
# MAGIC %pip install -q -c ../constraints.txt databricks-sdk pyyaml

# COMMAND ----------
dbutils.library.restartPython()

# COMMAND ----------
import os, sys, json, re, time
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
from checks import (PERSONAS, name, q, qp, principal, first_line, paginate, has_proof, genie_space_owned, lab_names,
                    is_not_found, attempt)
from databricks.sdk import WorkspaceClient

for k, d in [("catalog", "main"), ("schema", "genie_governance_lab"), ("delete_governed_tag", "true"), ("dry_run", "true"),
             ("lab_groups", ""), ("resource_prefix", "lab")]:
    dbutils.widgets.text(k, d)
N = lab_names(dbutils.widgets.get("resource_prefix"))
PROBE_EP = N["probe_endpoint"]
C, S = name(dbutils.widgets.get("catalog")), name(dbutils.widgets.get("schema"))
DRY = dbutils.widgets.get("dry_run").lower() != "false"
T, TOOLS = f"{q(C)}.{q(S)}", f"{C}.{S}_tools"
LAB_GROUPS = [principal(g.strip()) for g in dbutils.widgets.get("lab_groups").split(",") if g.strip()]
manual = []   # things the lab can't prove it owns: listed for manual removal, never deleted
UC = "/api/2.1/unity-catalog"
w = WorkspaceClient()
api = w.api_client.do
plan, failures, done = [], [], []

# "Already gone" only from a STRUCTURED not-found (checks.is_not_found: SDK NotFound / ResourceDoesNotExist, a
# not-found error_code, an HTTP 404 status, a not-found Spark error class), never from message text: the same rule as
# the runner. Anything else is a failure, which keeps the manifest and the results tables (see "Tables last").
def step(what, fn):
    plan.append(what)
    if DRY:
        print("would:", what); return
    outcome, e = attempt(fn)
    if outcome == "ok":
        done.append(what); print("ok    ", what)
    elif outcome == "gone":
        done.append(what + " (already gone)"); print("gone  ", what)
    else:
        failures.append({"step": what, "error": first_line(e, 300)}); print("FAILED", what, "-", first_line(e, 200))

MAN = f"{T}.lab_manifest"
items = [r.asDict() for r in spark.table(MAN).collect()] if spark.catalog.tableExists(f"{C}.{S}.lab_manifest") else []
by = {}
for it in items:
    by.setdefault(it["kind"], []).append(it)
print(f"manifest: {len(items)} item(s):", {k: len(v) for k, v in by.items()})

# COMMAND ----------
# MAGIC %md ## Workspace objects

# COMMAND ----------
# Genie spaces: trashed only with independent creation provenance (manifest row with creation proof, not marked
# unproven) AND a live title/description that is a lab pair or the exact title recorded at creation. Anything else,
# including IDs adopted from lab_setup_status by earlier versions, is listed for manual review.
for it in by.get("genie_space", []):
    sid = it["object_id"]
    try:
        unproven = json.loads(it.get("detail") or "{}").get("unproven") is True
    except (TypeError, ValueError, AttributeError):
        unproven = True
    if unproven:
        manual.append(f"Genie space {sid} (marked unproven: not an exact lab title/description pair; not trashed)")
        continue
    try:
        sp = w.genie.get_space(sid)
    except Exception as e:
        if is_not_found(e):
            print("gone   Genie space", sid); continue
        # unresolved (not proven gone): the manifest and results tables are kept so a rerun can retry it
        failures.append({"step": f"read Genie space {sid}", "error": first_line(e, 300)}); continue
    if not has_proof(it.get("detail")):
        manual.append(f"Genie space {sid} {sp.title!r} (recorded without creation proof, e.g. adopted from "
                      "lab_setup_status by an earlier version; not trashed)")
        continue
    if not genie_space_owned(it.get("detail"), sp.title, sp.description):
        manual.append(f"Genie space {sid} {sp.title!r} (not a lab title/description pair or the title recorded at "
                      "creation; not trashed)")
        continue
    step(f"trash Genie Agent {sid} ({sp.title!r}, exact lab title/description)", lambda i=sid: w.genie.trash_space(i))
for it in by.get("vs_index", []):
    [name(x) for x in it["object_id"].split(".")]
    step(f"delete vector search index {it['object_id']}", lambda i=it["object_id"]: w.vector_search_indexes.delete_index(i))
for it in by.get("experiment", []):
    step(f"delete experiment {it['object_id']}", lambda i=it["object_id"]: w.experiments.delete_experiment(i))

# COMMAND ----------
# MAGIC %md ## Probe objects (B-01 … B-05, B-04b)
# MAGIC Only names recorded by the collector (unique per-run names from the persona's own evidence, pattern-checked).
# MAGIC Fixed-name probe objects from earlier lab versions carry no ownership proof: they are **listed for manual
# MAGIC removal**, never deleted.

# COMMAND ----------
PROBE_RE = re.compile(rf"^({re.escape(TOOLS)}\.probe_(clinician|pii_reader|outsider|facilitator)_[0-9a-f]{{8}}_(ms|provider|conn|mcp|mcpb)"
                      rf"|{re.escape(PROBE_EP)}-(clinician|pii-reader|outsider|facilitator)-[0-9a-f]{{8}})$")
probes = set()
for it in by.get("probe_object", []):
    kind, _, nm = it["object_id"].partition("|")
    src = json.loads(it.get("detail") or "{}").get("source", "")
    if src.startswith("probe_log_"):          # proof: recorded from the persona's own intent/creation log
        probes.add((kind, nm))
    else:
        manual.append(f"{kind} {nm} (manifest row without probe-log proof; not deleted)")
def exists(kind, nm):
    try:
        api("GET", f"/api/2.0/serving-endpoints/{nm}" if kind == "serving-endpoints" else f"{UC}/{kind}/{nm}")
        return True
    except Exception as e:
        if is_not_found(e):
            return False
        raise
for kind, nm in sorted(probes):
    if not PROBE_RE.fullmatch(nm):
        print("ignored (not a probe name):", nm); continue
    try:
        if not exists(kind, nm):
            continue
    except Exception as e:
        failures.append({"step": f"check {kind} {nm}", "error": first_line(e)}); continue
    def delete_and_verify(k=kind, n=nm):
        api("DELETE", f"/api/2.0/serving-endpoints/{n}" if k == "serving-endpoints" else f"{UC}/{k}/{n}")
        for _ in range(24):                   # verify absence: GET must return 404
            if not exists(k, n):
                return
            time.sleep(5)
        raise RuntimeError(f"{k} {n} still exists after DELETE")
    step(f"delete probe {kind} {nm} (verify GET -> 404)", delete_and_verify)
# Legacy fixed names (earlier lab versions): report only.
KIND_SUFFIX = {"model-services": "ms", "model-provider-services": "provider", "connections": "conn", "mcp-services": "mcp"}
for p in PERSONAS:
    legacy_names = [(k, f"{TOOLS}.probe_{p}_{suf}") for k, suf in KIND_SUFFIX.items()] + \
                   [("serving-endpoints", f"{PROBE_EP}-{p.replace('_', '-')}")]
    for kind, nm in legacy_names:
        try:
            if exists(kind, nm):
                manual.append(f"{kind} {nm} (fixed-name probe object from an earlier lab version; no ownership record)")
        except Exception as e:
            manual.append(f"{kind} {nm} (couldn't check: {first_line(e, 100)})")

# Report (don't delete) anything probe-like left in the tools schema that isn't recorded.
for kind in ("model-services", "model-provider-services", "mcp-services"):
    try:
        listed = paginate(api, f"{UC}/{kind}", kind.replace("-", "_"), {"parent": f"schemas/{TOOLS}"})
    except Exception as e:
        listed = []; print("could not list", kind, "-", first_line(e, 150))
    for x in listed:
        n = x.get("full_name") or x.get("name", "")
        if (kind, n) not in probes:
            print(f"NOT deleted (not recorded by the lab): {kind} {n}")

# COMMAND ----------
# MAGIC %md ## Connections and the grants the lab added on them
# MAGIC After the probe objects, so no lab MCP service still depends on a connection.

# COMMAND ----------
# Connections: first the USE CONNECTION grants the lab added (proven rows only; verified absent), then the
# connections the lab proved it created (verified: GET -> 404). Unproven connection rows (e.g. adopted by an
# earlier lab version) are listed for manual review and never dropped. Lab-group grants on lab connections that the manifest doesn't
# own are listed for manual review.
def proven(it):
    """Only rows with the proof marker (absent before, present after; not marked unproven) are acted on."""
    return has_proof(it.get("detail"))

def conn_privs(full):
    r = api("GET", f"{UC}/permissions/connection/{full}") or {}
    return {(a.get("principal"), p) for a in r.get("privilege_assignments", []) or [] for p in a.get("privileges", []) or []}

def conn_exists(full):
    try:
        api("GET", f"{UC}/connections/{full}"); return True
    except Exception as e:
        if is_not_found(e):
            return False
        raise

owned_conn_grants = set()
for it in by.get("connection_grant", []):
    conn, who, priv = it["object_id"].split("|")
    [name(x) for x in conn.split(".")]
    if not proven(it):
        manual.append(f"connection {conn}: {priv} for {who} (recorded without before/after proof; not revoked)")
        continue
    owned_conn_grants.add((conn, who, priv))
    def revoke_conn_grant(c=conn, g=principal(who), pv=priv):
        api("PATCH", f"{UC}/permissions/connection/{c}", body={"changes": [{"principal": g, "remove": [pv]}]})
        if (g, pv) in conn_privs(c):
            raise RuntimeError(f"{pv} for {g} still granted on {c} after REVOKE")
    step(f"revoke {priv} on connection {conn} from {who} (verify absent)", revoke_conn_grant)
for it in by.get("connection", []):
    if not LAB_GROUPS:
        break
    try:
        for who, pv in conn_privs(it["object_id"]):
            if who in LAB_GROUPS and (it["object_id"], who, pv) not in owned_conn_grants:
                manual.append(f"connection {it['object_id']}: {pv} for {who} (not recorded as added by the lab)")
    except Exception as e:
        if not is_not_found(e):
            manual.append(f"connection {it['object_id']}: couldn't read grants ({first_line(e, 100)})")
for it in by.get("connection", []):
    [name(x) for x in it["object_id"].split(".")]
    if not proven(it):
        manual.append(f"connection {it['object_id']} (recorded without creation proof; not dropped)")
        continue
    def drop_conn(i=it["object_id"]):
        api("DELETE", f"{UC}/connections/{i}")
        for _ in range(12):                   # verify absence: GET must return 404
            if not conn_exists(i):
                return
            time.sleep(5)
        raise RuntimeError(f"connection {i} still exists after DELETE")
    step(f"drop connection {it['object_id']} (verify GET -> 404)", drop_conn)

# COMMAND ----------
# MAGIC %md ## ACL entries and grants the lab added

# COMMAND ----------
def remove_acl_entries(obj_type, obj_id, entries):
    """Remove exactly the recorded (group, level) entries; every other direct entry stays as it is now."""
    from databricks.sdk.service.iam import AccessControlRequest, PermissionLevel
    perms = w.permissions.get(obj_type, obj_id)
    keep = []
    for a in perms.access_control_list or []:
        for p in a.all_permissions or []:
            if p.inherited:
                continue
            lvl = p.permission_level.value if p.permission_level else None
            if a.group_name and (a.group_name, lvl) in entries:
                continue
            keep.append(AccessControlRequest(user_name=a.user_name, group_name=a.group_name,
                                             service_principal_name=a.service_principal_name,
                                             permission_level=p.permission_level))
    w.permissions.set(obj_type, obj_id, access_control_list=keep)

acl = {}
for it in by.get("acl_entry", []):
    obj_type, obj_id, ptype, who, lvl = it["object_id"].split("|")
    if proven(it):
        acl.setdefault((obj_type, obj_id), set()).add((who, lvl))
    else:
        manual.append(f"{obj_type} {obj_id}: {who} {lvl} (recorded without before/after proof; not revoked)")
for (obj_type, obj_id), entries in acl.items():
    step(f"remove lab ACL entries {sorted(entries)} from {obj_type} {obj_id}",
         lambda t=obj_type, i=obj_id, e=entries: remove_acl_entries(t, i, e))

# Lab-group entries on endpoints the setup used but that the manifest doesn't own: list for manual review.
status = {r.key: r.value for r in spark.table(f"{T}.lab_setup_status").collect()} \
    if spark.catalog.tableExists(f"{C}.{S}.lab_setup_status") else {}
for obj_type, key in (("vector-search-endpoints", "vs_endpoint_id"), ("serving-endpoints", "agent_endpoint_id")):
    oid = status.get(key, "")
    if not oid or oid.startswith(("skip", "error")) or not LAB_GROUPS:
        continue
    try:
        for a in w.permissions.get(obj_type, oid).access_control_list or []:
            for pm in a.all_permissions or []:
                lvl = pm.permission_level.value if pm.permission_level else None
                if a.group_name in LAB_GROUPS and not pm.inherited and (a.group_name, lvl) not in acl.get((obj_type, oid), set()):
                    manual.append(f"{obj_type} {oid}: {a.group_name} {lvl} (not recorded as added by the lab)")
    except Exception as e:
        manual.append(f"{obj_type} {oid}: couldn't read ACL ({first_line(e, 100)})")

owned_cat = {it["object_id"] for it in by.get("catalog_grant", []) if proven(it)}
for it in by.get("catalog_grant", []):
    if not proven(it):
        manual.append(f"catalog grant {it['object_id']} (recorded without before/after proof; not revoked)")
try:
    for r in spark.sql(f"SHOW GRANTS ON CATALOG {q(C)}").collect():
        if r.Principal in LAB_GROUPS and r.ActionType.replace("_", " ").upper() == "USE CATALOG" \
                and f"{C}|{r.Principal}|USE CATALOG" not in owned_cat:
            manual.append(f"catalog {C}: USE CATALOG for {r.Principal} (not recorded as added by the lab)")
except Exception as e:
    manual.append(f"catalog {C}: couldn't read grants ({first_line(e, 100)})")

for it in [x for x in by.get("catalog_grant", []) if proven(x)]:
    cat, who, priv = it["object_id"].split("|")
    step(f"revoke {priv} on catalog {cat} from {who}",
         lambda c=cat, g=who: spark.sql(f"REVOKE USE CATALOG ON CATALOG {q(c)} FROM {qp(principal(g))}"))

# COMMAND ----------
# MAGIC %md ## Schema objects (policies, functions, tables) and the governed tag

# COMMAND ----------
for it in by.get("policy", []):
    sch, _, pol = it["object_id"].partition(":")
    c, s = sch.split(".")
    # DROP POLICY has no IF EXISTS; a policy that is already gone is reported by step() as "already gone".
    step(f"drop policy {pol} on {sch}", lambda c=c, s=s, p=name(pol): spark.sql(f"DROP POLICY {q(p)} ON SCHEMA {q(c)}.{q(s)}"))
for it in by.get("function", []):
    c, s, f = it["object_id"].split(".")
    step(f"drop function {it['object_id']}", lambda c=c, s=s, f=f: spark.sql(f"DROP FUNCTION IF EXISTS {q(c)}.{q(s)}.{q(f)}"))
if dbutils.widgets.get("delete_governed_tag") == "true":
    for it in by.get("governed_tag", []):
        step(f"delete governed tag {it['object_id']} (created by the lab)", lambda t=it["object_id"]: w.tag_policies.delete_tag_policy(t))

# COMMAND ----------
# MAGIC %md ## Tables last
# MAGIC Data tables first, then the results / suite tables, then the manifest, and **only if every step above
# MAGIC succeeded**, so a failed teardown keeps the evidence and the record of what is still to delete.

# COMMAND ----------
RESULT_TABLES = ("test_results", "suite_runs", "test_catalogue", "lab_setup_status")
tables = [it["object_id"] for it in by.get("table", []) if it["object_id"] != f"{C}.{S}.lab_manifest"]
data_t = [t for t in tables if t.split(".")[-1] not in RESULT_TABLES and not t.split(".")[-1].startswith("test_results_")]
result_t = [t for t in tables if t not in data_t]
def drop_tables(ts):
    for t in ts:
        c, s_, tb = t.split(".")
        step(f"drop table {t}", lambda c=c, s_=s_, tb=tb: spark.sql(f"DROP TABLE IF EXISTS {q(c)}.{q(s_)}.{q(tb)}"))

summary = lambda: {"dry_run": DRY, "planned": len(plan), "done": len(done), "failures": failures,
                   "manual_review": manual}
if not DRY and failures:
    print(json.dumps(summary(), indent=1))
    raise RuntimeError(f"teardown: {len(failures)} step(s) failed; tables and manifest kept. Fix and rerun: {failures}")
drop_tables(data_t)
if not DRY and failures:
    raise RuntimeError(f"teardown: dropping data tables failed; results and manifest kept: {failures}")
drop_tables(result_t)
if not DRY and failures:
    raise RuntimeError(f"teardown: dropping results tables failed; manifest kept: {failures}")
step(f"drop table {C}.{S}.lab_manifest", lambda: spark.sql(f"DROP TABLE IF EXISTS {MAN}"))

print(json.dumps(summary(), indent=1))
if manual:
    print("\nMANUAL REVIEW (not deleted, no ownership record):\n  " + "\n  ".join(manual))
if DRY:
    dbutils.notebook.exit(json.dumps({"dry_run": True, "plan": plan, "manual_review": manual, "failures": failures}))
if failures:
    raise RuntimeError(f"teardown: dropping the manifest failed: {failures}")
dbutils.notebook.exit(json.dumps(summary()))
