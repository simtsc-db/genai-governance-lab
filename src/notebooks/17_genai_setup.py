# Databricks notebook source
# MAGIC %md
# MAGIC # 17 · GenAI surfaces beyond Genie (DEMO mode)
# MAGIC Sets up the objects the persona probes use to show that **every** GenAI surface follows the
# MAGIC same identities and Unity Catalog permissions:
# MAGIC
# MAGIC | Surface | Object | Who gets access |
# MAGIC |---|---|---|
# MAGIC | Agent tools (MCP, T-01) | UC function `<schema>_tools.<tool_fn>` | clinicians only (EXECUTE) |
# MAGIC | External MCP (B-04) | dummy connection `<schema>_tools.<prefix>_mcp_conn` | nobody (no USE CONNECTION) |
# MAGIC | External MCP (B-04b) | dummy connection `<schema>_tools.<prefix>_mcp_conn_builders` | clinicians only (USE CONNECTION) |
# MAGIC | Vector / AI Search | index over `clinical_notes_masked` | clinicians and PII readers (SELECT) |
# MAGIC | Model serving / agents | existing endpoint (`agent_endpoint`) | clinicians only (CAN_QUERY) |
# MAGIC | MLflow traces | lab experiment with one trace | PII readers only (CAN_READ) |
# MAGIC
# MAGIC Names come from `resource_prefix` (`checks.lab_names`; the default `lab` keeps the historical names).
# MAGIC The Unity Gateway model service `<prefix>_chat` is a bundle resource (`resources/demo/lab_chat.model_service.yml`);
# MAGIC this notebook binds its identity-dependent settings (EXECUTE for clinicians and PII readers, the clinicians' rate
# MAGIC limit) at run time if they are missing, so the `workspace` target can deploy before the personas exist.
# MAGIC Everything created here is recorded in `lab_manifest` and removed by `99_teardown`. Existing endpoints only get
# MAGIC lab-group ACL entries added where the group had none; teardown removes exactly those entries again.

# COMMAND ----------
# MAGIC %pip install -q -c ../constraints.txt databricks-sdk "mlflow-skinny[databricks]" pyyaml

# COMMAND ----------
dbutils.library.restartPython()

# COMMAND ----------
import os, sys, time, json
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
from checks import Manifest, name, q, qp, principal, suite_run_id, first_line, lab_names
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.iam import AccessControlRequest, PermissionLevel

for k, d in [("catalog", "main"), ("schema", "genie_governance_lab"), ("vs_endpoint", ""), ("agent_endpoint", ""),
             ("embedding_model", "databricks-qwen3-embedding-0-6b"), ("clinicians_group", ""),
             ("pii_readers_group", ""), ("outsiders_group", ""), ("resource_prefix", "lab")]:
    dbutils.widgets.text(k, d)
N = lab_names(dbutils.widgets.get("resource_prefix"))
TOOL_FN, NRIC_GUARD = N["tool_fn"], N["nric_guard"]
C, S = name(dbutils.widgets.get("catalog")), name(dbutils.widgets.get("schema"))
T, TOOLS = f"{q(C)}.{q(S)}", f"{q(C)}.{q(S + '_tools')}"
VS, AGENT, EMB = (dbutils.widgets.get(k) for k in ("vs_endpoint", "agent_endpoint", "embedding_model"))
for v in (VS, AGENT, EMB):
    if v:
        name(v, "endpoint name")
G_CLIN, G_PII, G_OUT = (principal(dbutils.widgets.get(k), k) for k in ("clinicians_group", "pii_readers_group", "outsiders_group"))
w = WorkspaceClient()
man = Manifest(spark, C, S, "17_genai_setup", suite_run_id(dbutils))
legacy = {r.key: r.value for r in spark.table(f"{T}.lab_setup_status").collect()}
status = {}

def step(key, fn):
    try:
        v = fn(); status[key] = v if isinstance(v, str) else "ok"
    except Exception as e:
        status[key] = f"error: {first_line(e)}"
    print(key, "->", status[key])
    return not str(status[key]).startswith("error")

def direct_entries(acl):
    """{(group, level)} of direct (non-inherited) group entries."""
    return {(a.group_name, p.permission_level.value) for a in (acl.access_control_list or []) if a.group_name
            for p in (a.all_permissions or []) if not p.inherited and p.permission_level}

def add_acl_entries(obj_type, obj_id, wanted, get_acl, patch):
    """Read the ACL, add (group, level) only for groups with no direct entry, read it again, and record in the
    manifest ONLY entries that were absent before and are present now. Pre-existing entries are never claimed."""
    before = direct_entries(get_acl())
    groups_before = {g for g, _ in before}
    add = [(g, lvl) for g, lvl in wanted if g not in groups_before]
    if add:
        patch(add)
    after = direct_entries(get_acl())
    confirmed = [(g, lvl) for g, lvl in add if (g, lvl) in after and (g, lvl) not in before]
    for g, lvl in confirmed:
        man.add("acl_entry", f"{obj_type}|{obj_id}|group|{g}|{lvl}", {"confirmed_absent_before": True})
    return (f"added and confirmed {len(confirmed)} of {len(add)} new entr{'y' if len(add) == 1 else 'ies'}; "
            f"{len(wanted) - len(add)} group(s) already had an entry (not claimed, kept by teardown)")

# Earlier lab versions recorded ACL entries without checking they were new. Those claims can't be proven,
# so they are dropped from the manifest (teardown lists lab-group entries it doesn't own for manual review).
spark.sql(f"""DELETE FROM {T}.lab_manifest WHERE kind = 'acl_entry'
              AND get_json_object(detail, '$.adopted_from') IS NOT NULL""")

# COMMAND ----------
# MAGIC %md ## Agent tool exposed through managed MCP (T-01)

# COMMAND ----------
if step("tool_fn", lambda: spark.sql(f"""CREATE OR REPLACE FUNCTION {TOOLS}.{q(TOOL_FN)}(facility STRING)
  RETURNS INT COMMENT 'GenAI governance lab tool: bed capacity for a facility (synthetic)'
  RETURN CASE facility WHEN 'Polyclinic North' THEN 40 WHEN 'Polyclinic East' THEN 35
                       WHEN 'General Hospital West' THEN 600 ELSE 150 END""")):
    man.add("function", f"{C}.{S}_tools.{TOOL_FN}")
step("tool_grant", lambda: spark.sql(f"GRANT EXECUTE ON FUNCTION {TOOLS}.{q(TOOL_FN)} TO {qp(G_CLIN)}"))

# COMMAND ----------
# MAGIC %md ## Dummy MCP connection (B-03, B-04)
# MAGIC A schema-level HTTP connection to a placeholder host. **Nobody gets USE CONNECTION**, so B-04 shows that
# MAGIC even a group allowed to create services (clinicians, `CREATE SERVICE` on the tools schema) can't register
# MAGIC an external MCP over a connection it hasn't been given. It never sends traffic.

# COMMAND ----------
UCAPI = "/api/2.1/unity-catalog"

def not_found(e):
    return any(k in str(e).lower() for k in ("does not exist", "not found", "404", "resource_does_not_exist"))

def lab_connection(conn_name, comment):
    """(full_name, lab_owned). A connection counts as lab-owned only with creation proof: GET 404 before this run's
    POST, present after (or such a proven row from an earlier run). An existing connection is never adopted."""
    try:
        c = w.api_client.do("GET", f"{UCAPI}/connections/{C}.{S}_tools.{conn_name}")
        full = c["full_name"]
        owned = man.proven("connection", full)
        if owned and c.get("comment") != comment:     # only a connection the lab proved it created
            w.api_client.do("PATCH", f"{UCAPI}/connections/{full}", body={"comment": comment})
        return full, owned
    except Exception as e:
        if not not_found(e):
            raise
    full = w.api_client.do("POST", f"{UCAPI}/connections", body={
        "name": conn_name, "connection_type": "HTTP", "parent": f"schemas/{C}.{S}_tools", "comment": comment,
        "options": {"host": "https://mcp.example.com", "port": "443", "base_path": "/mcp",
                    "bearer_token": "lab-dummy-token"}})["full_name"]
    w.api_client.do("GET", f"{UCAPI}/connections/{full}")        # present now (absent before: 404 above)
    man.record_created("connection", full)
    return full, True

# Earlier lab versions adopted connections from status/comment/owner without creation proof. Keep those rows,
# marked unproven: teardown lists them for manual review and never drops them.
for c_ in man.mark_unproven("connection", "no creation proof (adopted by an earlier lab version)"):
    print("manifest: marked unproven (manual review at teardown):", c_)

def mcp_connection():
    full, owned = lab_connection(N["mcp_conn"],
                                 "GenAI governance lab: dummy MCP connection; nobody has USE CONNECTION. Safe to drop.")
    if not owned:
        # B-04 only needs a connection nobody may use; nothing is granted on it, and teardown won't drop it.
        print(f"{full} exists without creation proof: used for B-04 as is, listed for manual review at teardown")
    return full
step("mcp_connection", mcp_connection)

# COMMAND ----------
# MAGIC %md ## Builders' MCP connection (B-04b)
# MAGIC A **second** dummy connection in the tools schema, with `USE CONNECTION` for clinicians (the lab's approved
# MAGIC builders) only. B-04b shows the allowed path: `CREATE SERVICE` on the schema plus `USE CONNECTION` on the
# MAGIC connection lets a builder register an MCP service. It never sends traffic (the probe doesn't list tools).
# MAGIC The connection is recorded only if this run created it; the grant only if it was absent before and present after.
# MAGIC If a connection with this name already exists without the lab's creation proof, it is neither reused nor
# MAGIC granted on, and B-04b records SKIP (connection name collision, not lab-owned).

# COMMAND ----------
def conn_privs(full):
    """{(principal, privilege)} granted directly on a connection."""
    r = w.api_client.do("GET", f"{UCAPI}/permissions/connection/{full}") or {}
    return {(a.get("principal"), p) for a in r.get("privilege_assignments", []) or [] for p in a.get("privileges", []) or []}

def builders_connection():
    full, owned = lab_connection(N["mcp_conn_builders"], "GenAI governance lab: dummy MCP connection for approved "
                                 "builders; never sends traffic. Safe to drop.")
    if not owned:
        # Never reuse or grant on a connection the lab can't prove it created: B-04b records SKIP.
        return f"skip: connection name collision, not lab-owned ({full})"
    return full

def builders_grant():
    full = status.get("mcp_connection_builders", "")
    if not full or full.startswith(("skip", "error")):
        return f"skip: builders connection not set up ({full or 'missing'})"
    key = f"{full}|{G_CLIN}|USE_CONNECTION"
    if (G_CLIN, "USE_CONNECTION") in conn_privs(full):
        return ("ok (added by an earlier run of this lab version, recorded)" if man.has("connection_grant", key)
                else "already held; not recorded (teardown keeps it)")
    w.api_client.do("PATCH", f"{UCAPI}/permissions/connection/{full}",
                    body={"changes": [{"principal": G_CLIN, "add": ["USE_CONNECTION"]}]})
    if (G_CLIN, "USE_CONNECTION") not in conn_privs(full):
        return "error: USE CONNECTION not visible after the grant"
    man.add("connection_grant", key, {"confirmed_absent_before": True})   # absent before, present after
    return "granted and confirmed"

step("mcp_connection_builders", builders_connection)
step("mcp_connection_builders_grant", builders_grant)
# B-01/B-02: clinicians are the lab's "approved builders"; nobody else may create services here.
step("create_service_grant", lambda: spark.sql(f"GRANT CREATE SERVICE ON SCHEMA {TOOLS} TO {qp(G_CLIN)}"))

# COMMAND ----------
# MAGIC %md ## Custom NRIC guardrail function (for manual step M-04)
# MAGIC Built-in sensitive-data detection in service policies has no Singapore NRIC/FIN class. This Python policy
# MAGIC function adds one. Python service policies don't run where the serverless network policy restricts internet
# MAGIC access; there, use the LLM-as-a-judge custom guardrail described in M-04 instead.

# COMMAND ----------
if step("nric_guard_py", lambda: spark.sql(r"""CREATE OR REPLACE FUNCTION %s.%s(event VARIANT, config VARIANT)
RETURNS VARIANT LANGUAGE PYTHON ENVIRONMENT (environment_version = '6')
COMMENT 'GenAI governance lab: Python service policy that denies messages containing a Singapore NRIC/FIN'
AS $$
import json, re
e = event.toPython() if event is not None else {}
msg = (e.get("context") or {}).get("message") or ""
text = msg if isinstance(msg, str) else json.dumps(msg)
if re.search(r"\b[STFGM]\d{7}[A-Z]\b", text):
    out = {"decision": {"result": "DENY", "reason": "Singapore NRIC/FIN detected"}}
else:
    out = {"decision": {"result": "ALLOW"}}
return type(event).parseJson(json.dumps(out))
$$""" % (T, q(NRIC_GUARD)))):
    man.add("function", f"{C}.{S}.{NRIC_GUARD}")

# COMMAND ----------
# MAGIC %md ## Vector Search index over the masked notes (V-01, V-02)
# MAGIC Built only if `15_masked_notes` confirmed the masked notes contain no PII.

# COMMAND ----------
IDX = f"{C}.{S}.{N['vs_index']}"
if VS and legacy.get("notes_masking_check") != "ok":
    status["vs_index"] = f"skip: masked notes failed the leak check ({legacy.get('notes_masking_check')})"
elif VS:
    step("cdf", lambda: spark.sql(f"ALTER TABLE {T}.clinical_notes_masked SET TBLPROPERTIES (delta.enableChangeDataFeed = true)"))
    def make_index():
        from databricks.sdk.service.vectorsearch import (DeltaSyncVectorIndexSpecRequest, EmbeddingSourceColumn,
                                                          PipelineType, VectorIndexType)
        try:
            w.vector_search_indexes.get_index(IDX)
            if legacy.get("vs_index") == IDX and not man.has("vs_index", IDX):
                # ownership evidence: the index is inside the lab's own (bundle-managed) schema
                man.add("vs_index", IDX, {"adopted_from": "lab_setup_status", "evidence": "in the lab schema"})
            try:
                w.vector_search_indexes.sync_index(IDX)
            except Exception as e:
                if "currently running" not in str(e).lower():
                    raise
                return "exists, sync already running"
            return "exists, sync triggered"
        except Exception as e:
            if "does not exist" not in str(e).lower() and "not found" not in str(e).lower():
                raise
        w.vector_search_indexes.create_index(
            name=IDX, endpoint_name=VS, primary_key="visit_id", index_type=VectorIndexType.DELTA_SYNC,
            delta_sync_index_spec=DeltaSyncVectorIndexSpecRequest(
                source_table=f"{C}.{S}.clinical_notes_masked", pipeline_type=PipelineType.TRIGGERED,
                embedding_source_columns=[EmbeddingSourceColumn(name="note", embedding_model_endpoint_name=EMB)],
                columns_to_sync=["visit_id", "facility", "note"]))
        man.add("vs_index", IDX, {"endpoint": VS})
        return "created"
    step("vs_index", make_index)
    def wait_ready():
        for _ in range(60):
            st = w.vector_search_indexes.get_index(IDX).status
            if st and st.ready:
                return f"ready ({st.indexed_row_count} rows)"
            time.sleep(20)
        return "error: index not ready after 20 min"
    step("vs_ready", wait_ready)
    for g in (G_CLIN, G_PII):
        step(f"vs_select_{g}", lambda g=g: spark.sql(f"GRANT SELECT ON TABLE {T}.{q(N['vs_index'])} TO {qp(g)}"))
    def vs_endpoint_acl():
        ep_id = w.vector_search_endpoints.get_endpoint(VS).id
        wanted = [(g, "CAN_USE") for g in (G_CLIN, G_PII, G_OUT)]
        note = add_acl_entries("vector-search-endpoints", ep_id, wanted,
            lambda: w.permissions.get("vector-search-endpoints", ep_id),
            lambda add: w.permissions.update("vector-search-endpoints", ep_id, access_control_list=[
                AccessControlRequest(group_name=g, permission_level=PermissionLevel(lvl)) for g, lvl in add]))
        print("vector search endpoint ACL:", note)
        return ep_id
    step("vs_endpoint_id", vs_endpoint_acl)
    status["vs_index"] = IDX if not str(status.get("vs_index", "")).startswith("error") else status["vs_index"]
else:
    status["vs_index"] = "skip: vs_endpoint not set"

# COMMAND ----------
# MAGIC %md ## Serving / agent endpoint ACL (S-01)

# COMMAND ----------
if AGENT:
    def agent_acl():
        from databricks.sdk.service.serving import ServingEndpointAccessControlRequest, ServingEndpointPermissionLevel
        ep = w.serving_endpoints.get(AGENT)
        wanted = [(G_CLIN, "CAN_QUERY")]
        note = add_acl_entries("serving-endpoints", ep.id, wanted,
            lambda: w.serving_endpoints.get_permissions(ep.id),
            lambda add: w.serving_endpoints.update_permissions(ep.id, access_control_list=[
                ServingEndpointAccessControlRequest(group_name=g, permission_level=ServingEndpointPermissionLevel(lvl))
                for g, lvl in add]))
        print("agent endpoint ACL:", note)
        return ep.id
    step("agent_endpoint_id", agent_acl)
    status["agent_endpoint"] = AGENT
else:
    status["agent_endpoint_id"] = "skip: agent_endpoint not set"

# COMMAND ----------
# MAGIC %md ## MLflow experiment and trace (X-01)
# MAGIC One synthetic trace; its ID is stored so X-01 can check that the allowed persona gets exactly it back.

# COMMAND ----------
def make_experiment():
    import mlflow
    me = w.current_user.me().user_name
    path = f"/Users/{me}/{N['experiment']}"
    mlflow.set_tracking_uri("databricks")
    existed = mlflow.get_experiment_by_name(path)
    exp = mlflow.set_experiment(path)
    if existed is None:
        man.add("experiment", exp.experiment_id, {"path": path})
    elif legacy.get("experiment_id") == exp.experiment_id and not man.has("experiment", exp.experiment_id):
        # ownership evidence: the lab's fixed name in the deployer's own folder, recorded by an earlier lab run
        man.add("experiment", exp.experiment_id, {"path": path, "adopted_from": "lab_setup_status",
                                                  "evidence": "lab path in the deployer's folder"})
    with mlflow.start_span(name="lab_trace") as span:
        span.set_inputs({"question": "synthetic: visits per facility"}); span.set_outputs({"answer": "500"})
        tid = getattr(span, "trace_id", None) or getattr(span, "request_id", None)
    status["trace_id"] = str(tid or "")
    from databricks.sdk.service.ml import ExperimentAccessControlRequest, ExperimentPermissionLevel
    w.experiments.update_permissions(exp.experiment_id, access_control_list=[
        ExperimentAccessControlRequest(group_name=G_PII, permission_level=ExperimentPermissionLevel.CAN_READ)])
    return exp.experiment_id
step("experiment_id", make_experiment)

# COMMAND ----------
# MAGIC %md ## Unity Gateway model service: identity bindings (G-01, G-03)
# MAGIC EXECUTE for clinicians and PII readers and the clinicians' 2-per-minute rate limit. The demo targets set both
# MAGIC in the bundle (nothing to do here); the `workspace` target deploys the service without identities, and this
# MAGIC step adds what is missing (and verifies it). Outsiders never get EXECUTE. Nothing else in the service changes:
# MAGIC the update sends only the rate limits (or, if the API needs the whole config, the config read just before).

# COMMAND ----------
CHAT = f"{C}.{S}.{N['chat']}"

def chat_grants():
    path = f"{UCAPI}/permissions/model_service/{CHAT}"
    def privs():
        r = w.api_client.do("GET", path) or {}
        return {(a.get("principal"), p) for a in r.get("privilege_assignments", []) or [] for p in a.get("privileges", []) or []}
    held = privs()
    add = [g for g in (G_CLIN, G_PII) if (g, "EXECUTE") not in held]
    if add:
        w.api_client.do("PATCH", path, body={"changes": [{"principal": g, "add": ["EXECUTE"]} for g in add]})
    after = privs()
    if any((g, "EXECUTE") not in after for g in (G_CLIN, G_PII)):
        return "error: EXECUTE not visible after the grant"
    if (G_OUT, "EXECUTE") in after:
        return "error: outsiders hold EXECUTE on the model service (G-01 would fail); not changed by the lab"
    return f"ok (added {len(add)}, {2 - len(add)} already held)"

def chat_rate_limit():
    svc = w.api_client.do("GET", f"{UCAPI}/model-services/{CHAT}")
    cfg = svc.get("config") or {}
    limits = cfg.get("rate_limits") or []
    def has_limit(ls):
        return any(l.get("key") == "RATE_LIMIT_KEY_USER_GROUP" and l.get("principal") == G_CLIN for l in ls or [])
    if has_limit(limits):
        return "ok (already set)"
    want = limits + [{"key": "RATE_LIMIT_KEY_USER_GROUP", "principal": G_CLIN, "requests": 2,
                      "renewal_period": "RATE_LIMIT_RENEWAL_PERIOD_MINUTE"}]
    errors = []
    for mask, body in (("config.rate_limits", {"config": {"rate_limits": want}}),
                       ("config", {"config": {**cfg, "rate_limits": want}})):
        try:
            w.api_client.do("PATCH", f"{UCAPI}/model-services/{CHAT}", query={"update_mask": mask}, body=body)
        except Exception as e:
            errors.append(f"{mask}: {first_line(e, 150)}"); continue
        after = (w.api_client.do("GET", f"{UCAPI}/model-services/{CHAT}").get("config") or {})
        if has_limit(after.get("rate_limits")) and len(after.get("service_policies") or []) == len(cfg.get("service_policies") or []):
            return f"ok (added, update_mask={mask})"
        errors.append(f"{mask}: not visible after the update")
    return "error: " + "; ".join(errors)

step("chat_grants", chat_grants)
step("chat_rate_limit", chat_rate_limit)

# COMMAND ----------
merged = {r.key: r.value for r in spark.table(f"{T}.lab_setup_status").collect()}
merged.update({k: str(v) for k, v in status.items()})
spark.createDataFrame(list(merged.items()), "key STRING, value STRING") \
     .write.mode("overwrite").saveAsTable(f"{T}.lab_setup_status")
display(spark.table(f"{T}.lab_setup_status"))
