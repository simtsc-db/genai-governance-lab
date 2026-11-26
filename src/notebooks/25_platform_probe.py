# Databricks notebook source
# MAGIC %md
# MAGIC # 25 · Platform probe (AUDIT mode, non-destructive)
# MAGIC Calls AI functions on a **synthetic** sentence and reads grants, tags and endpoint configuration:
# MAGIC
# MAGIC * **D-06** `ai_mask` 1.0 (Public Preview, HIPAA compliant) should work and remove the PII.
# MAGIC * **D-07 / P-07** `ai_mask` 2.0 (Beta) isn't offered on Compliance Security Profile workspaces,
# MAGIC   and in some regions (Singapore among them, per the region table) it needs cross-Geo processing.
# MAGIC   A refusal that names Geography / cross-Geo processing proves the enforcement works (P-07 PASS).
# MAGIC * **D-05** Data Classification: needs cross-Geo processing in the Asia Geo (Designated Services). UI check M-03.
# MAGIC * **D-08** every column tagged as a Singapore NRIC / FIN / work permit number has an ABAC mask policy.
# MAGIC * **R-01** Knowledge Assistant: configuration only (state, who can query). Invoking it is opt-in.
# MAGIC * **B-06 / B-07** who may create AI services and connections; default access to `system.ai`.
# MAGIC * **B-08** the workspace setting *Enforce Unity Gateway* (settings v2 key `enforce_ai_gateway_v3_only`:
# MAGIC   undocumented; discovered from workspace settings metadata; best effort; may change without notice; the UI is authoritative).
# MAGIC * **B-09** inventory of serving endpoints that stay queryable with enforcement on, plus recent `databricks-*` traffic.

# COMMAND ----------
# MAGIC %md Pinned dependencies (one place for every notebook: `src/constraints.txt`).

# COMMAND ----------
# MAGIC %pip install -q -c ../constraints.txt databricks-sdk pyyaml

# COMMAND ----------
dbutils.library.restartPython()

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
from checks import Recorder, error_kind, mask_verdict, synthetic_sentence, name, q, tag_key, principal, suite_run_id, first_line, paginate, \
    gateway_enforcement_verdict, ENFORCE_GATEWAY_KEY, ai_endpoint_inventory, valid_lookback_days, usage_tracking_enabled, fetch_full_endpoints

for k, d in [("catalog", "main"), ("schema", "genie_governance_lab"), ("ka_endpoint", ""), ("ka_invoke", "false"),
             ("approved_builders", ""), ("nric_tag_keys", "class.sg_nric,class.sg_fin,class.sg_work_permit_number"),
             ("cleartext_groups", ""), ("usage_lookback_days", "30")]:
    dbutils.widgets.text(k, d)
C, S = name(dbutils.widgets.get("catalog")), name(dbutils.widgets.get("schema"))
T = f"{q(C)}.{q(S)}"
RUN = suite_run_id(dbutils)
rec = Recorder(spark, C, S, persona="admin", run_id=RUN)
status = {r.key: r.value for r in spark.table(f"{T}.lab_setup_status").collect()} \
    if spark.catalog.tableExists(f"{C}.{S}.lab_setup_status") else {}
# Synthetic sentence built at runtime: a fictional patient label, an NRIC with an invalid check letter (never a
# real NRIC) and a phone number on the reserved level 5 (can't be dialled in Singapore).
SAMPLE = synthetic_sentence()
from databricks.sdk import WorkspaceClient
w = WorkspaceClient()

# COMMAND ----------
try:
    out = spark.sql("SELECT ai_mask(:s, array('person name','national identity card number','phone number')) AS m",
                    args={"s": SAMPLE}).first().m
    st, lk = mask_verdict(out)
    rec.record("D-06", st, out, {"leaks": lk})
except Exception as e:
    rec.record("D-06", "FAIL" if error_kind(e) == "other" else "SKIP", f"ai_mask 1.0 failed: {first_line(e, 200)}")

# COMMAND ----------
if status.get("csp_enabled") == "True":
    rec.record("D-07", "SKIP", "ai_mask 2.0 is not available on Compliance Security Profile workspaces (use 1.0)")
    rec.record("P-07", "MANUAL", "CSP is on, so ai_mask 2.0 can't serve as the cross-Geo probe; use manual step M-03 (Data Classification)")
else:
    try:
        out = spark.sql("SELECT ai_mask(:s, options => map('version', '2.0')) AS m", args={"s": SAMPLE}).first().m
        st, lk = mask_verdict(out)
        rec.record("D-07", st,
                   f"ai_mask 2.0 available and {'redacted the PII' if not lk else 'LEFT PII in the output'}: {str(out)[:300]}", {"leaks": lk})
        rec.record("P-07", "INFO", "ai_mask 2.0 ran, so it isn't cross-Geo-only in this region or cross-Geo is allowed. Use manual step M-03 as the negative test.")
    except Exception as e:
        msg, low = first_line(e), str(e).lower()
        names_geo = any(k in low for k in ("cross-geo", "cross geo", "geography", "designated services"))
        region = any(k in low for k in ("in this region", "region for", "not available in"))
        if names_geo:
            rec.record("D-07", "PASS", f"refused, naming Geography / cross-Geo processing (documented reason): {msg}")
            rec.record("P-07", "PASS", "cross-Geo-only feature refused with the Geography enforcement reason", {"error": msg})
        elif region:
            rec.record("D-07", "PASS", f"refused as not available in this region (consistent with the region table): {msg}")
            rec.record("P-07", "INFO", "refused, but the message doesn't name Geography enforcement, so it doesn't prove the "
                       "setting by itself. Confirm P-06 / M-01 and use M-03.", {"error": msg})
        elif error_kind(e) == "preview_off":
            rec.record("D-07", "SKIP", f"ai_mask 2.0 preview not enabled on this workspace: {msg}")
            rec.record("P-07", "MANUAL", "ai_mask 2.0 is off here, so it can't serve as the cross-Geo probe; use manual step M-03", {"error": msg})
        else:
            rec.record("D-07", "FAIL" if error_kind(e) == "other" else "SKIP", f"{error_kind(e)}: {msg}")
            rec.record("P-07", "MANUAL", "Inconclusive; use manual step M-03", {"error": msg})

# COMMAND ----------
# MAGIC %md ## R-01 · Knowledge Assistant: configuration only
# MAGIC Records whether the endpoint exists, its state and who can query it. Invoking it is opt-in
# MAGIC (`ka_invoke=true`) and only allowed for an endpoint tagged `lab_synthetic=true` (a synthetic knowledge
# MAGIC base); only the HTTP status and correlation headers are stored, never the response body.

# COMMAND ----------
KA, KA_INVOKE = dbutils.widgets.get("ka_endpoint"), dbutils.widgets.get("ka_invoke").lower() == "true"
if not KA:
    rec.record("R-01", "SKIP", "ka_endpoint not set")
else:
    name(KA, "ka_endpoint")
    try:
        ep = w.serving_endpoints.get(KA)
        perms = w.serving_endpoints.get_permissions(ep.id)
        who = sorted({(a.group_name or a.user_name or a.service_principal_name, p.permission_level.value)
                      for a in (perms.access_control_list or []) for p in (a.all_permissions or [])
                      if p.permission_level and p.permission_level.value in ("CAN_QUERY", "CAN_MANAGE")})
        tags = {t.key: t.value for t in (ep.tags or [])}
        st = ep.state.as_dict() if ep.state else {}
        ev = {"state": st, "task": ep.task, "who_can_query": who[:50], "tags": tags}
        obs = (f"endpoint exists, state={st.get('ready')}/{st.get('config_update')}; {len(who)} principal(s) can query. "
               "Designated Services: Knowledge Assistant needs cross-Geo processing in the Asia Geo.")
        if KA_INVOKE and tags.get("lab_synthetic") == "true":
            import requests
            r = requests.post(f"{w.config.host.rstrip('/')}/serving-endpoints/{KA}/invocations", timeout=180,
                              headers={**w.config.authenticate(), "Content-Type": "application/json"},
                              json={"input": [{"role": "user", "content": "What is this knowledge base about? One sentence."}]})
            ev["invocation"] = {"http_status": r.status_code,
                                "correlation": {k: v for k, v in r.headers.items() if "request-id" in k.lower() or "trace" in k.lower()}}
            obs += (f" Opt-in invocation: HTTP {r.status_code} (endpoint served the request)." if r.status_code == 200
                    else f" Opt-in invocation: request failed with HTTP {r.status_code}.")
        elif KA_INVOKE:
            obs += " Invocation skipped: the endpoint isn't tagged lab_synthetic=true."
        rec.record("R-01", "INFO", obs, ev)
    except Exception as e:
        rec.record("R-01", "SKIP", f"couldn't read the endpoint configuration: {first_line(e, 200)}")

# COMMAND ----------
# MAGIC %md ## Who may build AI services (B-06, B-07)
# MAGIC Lists every direct grant of `CREATE SERVICE` (model, provider and MCP services), `CREATE CONNECTION`
# MAGIC and `ALL PRIVILEGES` at metastore, catalog and schema level, and flags grants to everyone or to principals
# MAGIC outside `approved_builders`. Only grants visible to the identity running the audit are listed, so the
# MAGIC result is MANUAL (inconclusive) unless it runs as a metastore admin. Owners can always create in what
# MAGIC they own; they aren't listed here.

# COMMAND ----------
BROAD = ("account users", "users")
APPROVED = {principal(x.strip()) for x in dbutils.widgets.get("approved_builders").split(",") if x.strip()}
try:
    builders = spark.sql("""
      SELECT 'metastore' AS level, '' AS object, grantee, privilege_type FROM system.information_schema.metastore_privileges
       WHERE privilege_type IN ('CREATE_SERVICE','CREATE_CONNECTION','ALL_PRIVILEGES')
      UNION ALL
      SELECT 'catalog', catalog_name, grantee, privilege_type FROM system.information_schema.catalog_privileges
       WHERE privilege_type IN ('CREATE_SERVICE','CREATE_CONNECTION','ALL_PRIVILEGES')
      UNION ALL
      SELECT 'schema', concat(catalog_name, '.', schema_name), grantee, privilege_type FROM system.information_schema.schema_privileges
       WHERE privilege_type IN ('CREATE_SERVICE','CREATE_CONNECTION','ALL_PRIVILEGES')""").collect()
except Exception as e:
    builders = None
    rec.record("B-06", "MANUAL", f"couldn't read the information_schema privilege views: {first_line(e, 200)}")
rows = [r.asDict() for r in builders] if builders is not None else []
broad = [r for r in rows if r["grantee"].lower() in BROAD]
outside = [r for r in rows if r["grantee"].lower() not in BROAD and r["grantee"] not in APPROVED]
me = w.current_user.me()
my_groups = {g.display for g in (me.groups or [])}
try:
    ms_owner = w.metastores.summary().owner
except Exception:
    ms_owner = None
is_ms_admin = bool(ms_owner) and (ms_owner == me.user_name or ms_owner in my_groups)
summary = (f"{len(rows)} visible grant(s) let principals create AI services or connections; "
           f"{len(broad)} to everyone, {len(outside)} to principals outside approved_builders")
ev = {"approved_builders": sorted(APPROVED), "to_everyone": broad[:50], "outside_approved": outside[:50],
      "metastore_admin": is_ms_admin, "grants": rows[:100]}
if builders is None:
    pass                                   # already recorded as MANUAL
elif broad or outside:
    rec.record("B-06", "FAIL", summary + ("" if is_ms_admin else " (and the list may be incomplete: not a metastore admin)"), ev)
elif not is_ms_admin:
    rec.record("B-06", "MANUAL", summary + ". Inconclusive: the audit identity isn't a metastore admin, so it can't see every grant.", ev)
else:
    rec.record("B-06", "PASS", summary, ev)

ai = [r.asDict() for r in spark.sql("SHOW GRANTS ON SCHEMA system.ai").collect()]
everyone = sorted({r["ActionType"] for r in ai if r["Principal"].lower() in BROAD})
rec.record("B-07", "INFO",
           ("All users can use every Databricks-provided model and MCP (system.ai grants to everyone: " + ", ".join(everyone) + ")")
           if "EXECUTE" in everyone else "system.ai EXECUTE is not granted to all users",
           {"grants": ai})

# COMMAND ----------
# MAGIC %md ## B-08 · Enforce Unity Gateway
# MAGIC Read-only: the workspace settings v2 API. When on, legacy pay-per-token endpoints are disabled and new
# MAGIC external-model and provisioned-throughput serving endpoints can't be created outside Unity Gateway (blocks NEW B-05
# MAGIC routes). Existing endpoints stay queryable: B-08 doesn't cover them, inventory them separately.
# MAGIC MANUAL when the setting isn't offered or can't be read; the UI path is in the observation (M-14).

# COMMAND ----------
try:
    keys = {m.name for m in w.workspace_settings_v2.list_workspace_settings_metadata()}
except Exception as e:
    keys = None; print("couldn't list workspace settings:", first_line(e, 200))
try:
    setting = w.workspace_settings_v2.get_public_workspace_setting(ENFORCE_GATEWAY_KEY).as_dict()
except Exception as e:
    setting = {"error": first_line(e, 200)}
st, obs = gateway_enforcement_verdict(keys, setting)
rec.record("B-08", st, obs, {"setting": ENFORCE_GATEWAY_KEY, "value": setting, "offered": None if keys is None else ENFORCE_GATEWAY_KEY in keys})

# COMMAND ----------
# MAGIC %md ## B-09 · Legacy AI endpoint inventory
# MAGIC Read-only. Enforce Unity Gateway blocks **new** external-model and provisioned-throughput endpoints, but existing
# MAGIC ones stay queryable. The verdict answers one question from the endpoint inventory: does an existing legacy route
# MAGIC exist right now? Every listed endpoint is read in full (no cap, bounded retries). FAIL: any external-model or
# MAGIC provisioned-throughput endpoint. MANUAL: the caller isn't a workspace admin (the list API only returns what the
# MAGIC caller can see), or any endpoint couldn't be classified. INFO: other AI endpoints (agents, custom models,
# MAGIC Knowledge Assistant, listed `databricks-*`). PASS: none of these. Usage from `system.serving` over
# MAGIC `usage_lookback_days` is attached as supplementary evidence only: traffic is shown only for endpoints with usage
# MAGIC tracking on during the window, so its absence is not proof of no traffic. Never request or response bodies.

# COMMAND ----------
DAYS = valid_lookback_days(dbutils.widgets.get("usage_lookback_days"))
# The list API is permission-filtered: only a workspace admin's list proves nothing else exists.
try:
    groups = [g.display for g in (w.current_user.me().groups or [])]
    visibility = {"admin": "admins" in groups, "basis": "SCIM Me groups " + ("include" if "admins" in groups else "don't include") + " 'admins'"}
except Exception as e:
    visibility = {"admin": None, "basis": f"couldn't read SCIM Me: {first_line(e, 120)}"}
listed, incomplete = [], []
try:
    for e in w.serving_endpoints.list():                         # the SDK follows any pagination
        listed.append(e.as_dict())
except Exception as e:
    if listed:
        incomplete.append(f"list interrupted after {len(listed)} endpoint(s): {first_line(e, 120)}")
    else:
        listed = None; print("couldn't list serving endpoints:", first_line(e, 200))
endpoints, tracking = None, {}
if listed is not None:
    # Full GET for EVERY endpoint (no cap; bounded retries): the list response can omit served entities and the
    # ai_gateway config. A failed GET makes the classification incomplete (MANUAL unless another endpoint FAILs).
    endpoints, failed = fetch_full_endpoints(listed, lambda n: w.serving_endpoints.get(n).as_dict())
    incomplete += failed
    tracking = {ep.get("name"): usage_tracking_enabled(ep) for ep in endpoints if ep.get("name") not in failed}
lab_owned = set()
if spark.catalog.tableExists(f"{C}.{S}.lab_manifest"):
    lab_owned = {r.object_id.partition("|")[2] for r in spark.sql(
        f"SELECT object_id FROM {T}.lab_manifest WHERE kind = 'probe_object' AND object_id LIKE 'serving-endpoints|%'").collect()}
try:
    usage = [r.asDict() for r in spark.sql("""
        SELECT se.endpoint_name, se.entity_type, count(*) AS requests, CAST(max(u.request_time) AS STRING) AS last_request
        FROM system.serving.endpoint_usage u
        JOIN (SELECT served_entity_id, endpoint_name, entity_type FROM system.serving.served_entities
              WHERE workspace_id = :ws
              QUALIFY row_number() OVER (PARTITION BY served_entity_id ORDER BY change_time DESC) = 1) se
          ON u.served_entity_id = se.served_entity_id
        WHERE u.workspace_id = :ws AND u.request_time >= current_timestamp() - make_dt_interval(:days)
        GROUP BY se.endpoint_name, se.entity_type""", args={"ws": str(w.get_workspace_id()), "days": DAYS}).collect()]
except Exception as e:
    usage = None; print("couldn't read system.serving usage:", first_line(e, 200))
st, obs, ev = ai_endpoint_inventory(endpoints, usage, lab_owned, DAYS, visibility, tracking, incomplete)
rec.record("B-09", st, obs, ev)

# COMMAND ----------
# MAGIC %md ## D-05 / D-08 · Data Classification and Singapore identifiers
# MAGIC D-08 lists every column tagged with a Singapore identifier class (from `system.information_schema.column_tags`,
# MAGIC so only columns visible to the audit identity) and checks that a column-mask ABAC policy on the table, schema
# MAGIC or catalog matches that tag. Tags the lab set by hand (demo mode, table `sg_identifiers_simulated`) are
# MAGIC labelled SIMULATED.

# COMMAND ----------
SG_TAGS = [tag_key(t.strip()) for t in dbutils.widgets.get("nric_tag_keys").split(",") if t.strip()]
CLEARTEXT = {principal(g.strip()) for g in dbutils.widgets.get("cleartext_groups").split(",") if g.strip()}
simulated = set()
if spark.catalog.tableExists(f"{C}.{S}.lab_manifest"):
    simulated = {r.object_id for r in spark.sql(f"SELECT object_id FROM {T}.lab_manifest WHERE kind = 'tag_assignment'").collect()}
tag_sql = """SELECT catalog_name, schema_name, table_name, column_name, tag_name, tag_value
             FROM system.information_schema.column_tags WHERE array_contains(split(:keys, ','), tag_name)"""
tagged = [r.asDict() for r in spark.sql(tag_sql, args={"keys": ",".join(SG_TAGS)}).collect()]   # no row cap

def policies_for(full_table):
    try:
        return paginate(w.api_client.do, f"/api/2.1/unity-catalog/policies/TABLE/{full_table}", "policies",
                        {"include_inherited": "true"})
    except Exception as e:
        return {"error": first_line(e, 200)}

TERM_TAG = re.compile(r"^has_tag\(\s*'([^']+)'\s*\)$")
TERM_VAL = re.compile(r"^has_tag_value\(\s*'([^']+)'\s*,\s*'([^']*)'\s*\)$")

def coverage(policy, full_table, tag, value):
    """True / False if the policy's MATCH COLUMNS predicate provably does / doesn't positively match this column's
    tag and the policy applies to this table's scope; None if the predicate or scope can't be resolved."""
    if "COLUMN_MASK" not in str(policy.get("policy_type", "")).upper():
        return False
    cat_, sch_, _ = full_table.split(".")
    if policy.get("on_securable_fullname") not in (full_table, f"{cat_}.{sch_}", cat_):
        return None
    if str(policy.get("when_condition") or "").strip():
        return None                                   # FOR TABLES WHEN ...: table condition not resolved here
    # Audience: the mask must apply to everyone ('account users'), exempting at most the configured cleartext groups.
    to = {str(x).lower() for x in (policy.get("to_principals") or [])}
    exc = set(policy.get("except_principals") or [])
    if to != {"account users"} or not exc <= CLEARTEXT:
        return None
    alias = (policy.get("column_mask") or {}).get("on_column")
    conds = [m.get("condition", "") for m in (policy.get("match_columns") or []) if m.get("alias") == alias]
    if len(conds) != 1:
        return None
    terms = [t.strip() for t in re.split(r"\s+OR\s+", conds[0].strip(), flags=re.I)]
    hit = False
    for t in terms:            # only plain disjunctions of has_tag / has_tag_value are resolved
        m1, m2 = TERM_TAG.fullmatch(t), TERM_VAL.fullmatch(t)
        if m1:
            hit |= m1.group(1) == tag
        elif m2:
            hit |= m2.group(1) == tag and m2.group(2) == (value or "")
        else:
            return None
    return hit

cache, report = {}, []
for r in tagged:
    full = f"{r['catalog_name']}.{r['schema_name']}.{r['table_name']}"
    if full not in cache:
        cache[full] = policies_for(full)
    pols = cache[full]
    col = f"{full}.{r['column_name']}"
    if isinstance(pols, dict):
        state, cov, unres = "unreadable", [], []
    else:
        res = [(p.get("name"), coverage(p, full, r["tag_name"], r.get("tag_value"))) for p in pols]
        cov = [n for n, v in res if v is True]
        unres = [n for n, v in res if v is None]
        state = "covered" if cov else "unresolved" if unres else "uncovered"
    report.append({"column": col, "tag": r["tag_name"], "state": state, "covering_policies": cov,
                   "unresolved_policies": unres, "simulated": col in simulated})
real = [x for x in report if not x["simulated"]]
sim_note = (f"; {len(report) - len(real)} SIMULATED: manual tag set by the lab (the documented fallback when automatic "
            "classification is unavailable), not detected by Data Classification") if len(real) < len(report) else ""
by_state = {k: [x["column"] for x in report if x["state"] == k] for k in ("covered", "uncovered", "unresolved", "unreadable")}
ev = {"columns": report[:200], "tag_keys": SG_TAGS, "counts": {k: len(v) for k, v in by_state.items()}}
if not tagged:
    rec.record("D-08", "INFO", "No column carries a Singapore identifier tag. Automatic Data Classification isn't enabled, hasn't "
               "run, or found nothing. In the Asia Geo it needs cross-Geo processing, so with in-Geo enforcement on it is "
               "unavailable; the documented fallback is to apply the same class.sg_* tags by hand (M-03).", ev)
elif by_state["uncovered"]:
    rec.record("D-08", "FAIL", f"{len(by_state['uncovered'])} of {len(tagged)} tagged column(s) have no ABAC mask policy"
               f"{sim_note}: {by_state['uncovered'][:5]}", ev)
elif by_state["unresolved"] or by_state["unreadable"]:
    rec.record("D-08", "MANUAL", f"{len(tagged)} tagged column(s){sim_note}; coverage of "
               f"{len(by_state['unresolved']) + len(by_state['unreadable'])} can't be resolved (policy predicate or scope "
               "not a plain has_tag match, audience not 'account users' EXCEPT only cleartext_groups, or policies "
               "unreadable); check them in Catalog Explorer", {**ev, "cleartext_groups": sorted(CLEARTEXT)})
else:
    rec.record("D-08", "PASS", f"all {len(tagged)} tagged column(s) are covered by an ABAC mask policy whose MATCH COLUMNS "
               f"predicate names the tag{sim_note}", ev)

rec.record("D-05", "MANUAL",
           "Catalog > a catalog > Details > Data Classification (M-03). Designated Services: in the Asia Geo it needs cross-Geo "
           f"processing, so expect 'not available' with in-Geo enforcement. Columns with SG identifier tags now: {len(real)}"
           + (f" (+{len(report) - len(real)} simulated)" if len(real) < len(report) else ""))
rec.flush()
