# Databricks notebook source
# MAGIC %md
# MAGIC # 50 · Evidence from system tables (run the next day)
# MAGIC Audit logs and gateway usage arrive with a delay of **hours**, so run this job the day
# MAGIC after the persona jobs. The deployer needs `USE CATALOG system`, `USE SCHEMA` and `SELECT` on
# MAGIC `system.access.audit` and `system.ai_gateway.usage` (a metastore admin grants these).
# MAGIC
# MAGIC Every check matches the **exact IDs the personas recorded** in this suite run: Genie conversation IDs
# MAGIC (A-05 evidence) and gateway `x-request-id`s (G-03 evidence, = `request_id` in usage and inference tables),
# MAGIC plus this workspace, the lab's Genie Agent / full 3-part model service name and each persona's application
# MAGIC ID. All three persona IDs must be configured. A missing row is **PENDING** until `log_latency_hours` has
# MAGIC passed since that persona's last result, FAIL after that.

# COMMAND ----------
# MAGIC %md Pinned dependencies (one place for every notebook: `src/constraints.txt`).

# COMMAND ----------
# MAGIC %pip install -q -c ../constraints.txt databricks-sdk pyyaml

# COMMAND ----------
dbutils.library.restartPython()

# COMMAND ----------
import os, sys, json, datetime as dt
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
from checks import Recorder, app_id, name, q, suite_run_id, first_line, table_not_available, PERSONAS, lab_names

for k, d in [("catalog", "main"), ("schema", "genie_governance_lab"), ("genie_space_id", ""),
             ("persona_apps", ""), ("evidence_run_id", ""), ("gateway_endpoint", ""), ("log_latency_hours", "24"),
             ("resource_prefix", "lab")]:
    dbutils.widgets.text(k, d)
C, S = name(dbutils.widgets.get("catalog")), name(dbutils.widgets.get("schema"))
T = f"{q(C)}.{q(S)}"
RUN = suite_run_id(dbutils)
APPS = dict(zip(PERSONAS, [app_id(a.strip()) for a in dbutils.widgets.get("persona_apps").split(",") if a.strip()]))
LATENCY = dt.timedelta(hours=float(dbutils.widgets.get("log_latency_hours") or 24))
START_ACTION = "genieStartConversationMessage"       # the request the persona probe makes
G01_PROMPT = "Reply with the single word OK."        # the request G-01 sends to the lab model service
SERVICE = dbutils.widgets.get("gateway_endpoint") or f"{C}.{S}.{lab_names(dbutils.widgets.get('resource_prefix'))['chat']}"
from databricks.sdk import WorkspaceClient
w = WorkspaceClient()
WS_ID = str(w.get_workspace_id())
setup = {r.key: r.value for r in spark.table(f"{T}.lab_setup_status").collect()} \
    if spark.catalog.tableExists(f"{C}.{S}.lab_setup_status") else {}
SPACE = dbutils.widgets.get("genie_space_id") or setup.get("genie_space_id", "")
if SPACE:
    name(SPACE, "genie_space_id")

# The suite to correlate: evidence_run_id, else the latest demo suite with persona results.
target = dbutils.widgets.get("evidence_run_id").strip() or (spark.sql(f"""
    SELECT s.run_id FROM {T}.suite_runs s WHERE s.mode = 'demo'
      AND EXISTS (SELECT 1 FROM {T}.test_results r WHERE r.run_id = s.run_id AND r.persona <> 'admin')
    ORDER BY s.started_at DESC LIMIT 1""").collect() or [[None]])[0][0]
rec = Recorder(spark, C, S, persona="admin", run_id=target or RUN)
EVID = ("O-01", "O-02", "O-03", "G-04")
if not target or len(APPS) != 3:
    for t in EVID:
        rec.record(t, "SKIP", "no demo suite with persona results found" if not target
                   else f"all three persona application IDs are needed (got {len(APPS)})")
    rec.flush(); dbutils.notebook.exit("not runnable")
win = spark.sql(f"""SELECT (SELECT started_at FROM {T}.suite_runs WHERE run_id = :r) AS start,
                           (SELECT max(ts) FROM {T}.test_results WHERE run_id = :r AND persona <> 'admin') AS stop""",
                args={"r": target}).first()
START = win.start - dt.timedelta(minutes=5)
STOP = (win.stop or win.start) + dt.timedelta(minutes=15)
base = {"suite_run_id": target, "window": [str(START), str(STOP)], "workspace_id": WS_ID, "personas": APPS}
print(json.dumps(base, default=str, indent=1))
args = {"ws": WS_ID, "start": START, "stop": STOP}
NOW = spark.sql("SELECT current_timestamp() AS n").first().n

# Exact IDs each persona recorded in this suite (latest row per test and persona)
rows = {(r.test_id, r.persona): r for r in spark.sql(f"""SELECT * FROM {T}.test_results WHERE run_id = :r
          AND test_id IN ('A-05', 'G-03') QUALIFY row_number() OVER (PARTITION BY test_id, persona ORDER BY ts DESC) = 1""",
          args={"r": target}).collect()}
def ev_of(test, p):
    r = rows.get((test, p))
    try:
        return json.loads(r.evidence) if r else {}
    except Exception:
        return {}
CONVS = {p: sorted(set(ev_of("A-05", p).get("genie_conversation_ids") or [])) for p in PERSONAS}
REQS = {p: [g for g in (ev_of("G-03", p).get("gateway_requests") or []) if g.get("request_id")] for p in PERSONAS}
OK_REQS = {p: sorted({g["request_id"] for g in REQS[p] if g.get("http_status") == 200}) for p in PERSONAS}
G01_REQ = {p: next((g["request_id"] for g in REQS[p] if g.get("prompt") == G01_PROMPT and g.get("http_status") == 200), None)
           for p in PERSONAS}
LAST = {p: (spark.sql(f"SELECT max(ts) AS m FROM {T}.test_results WHERE run_id = :r AND persona = :p",
                      args={"r": target, "p": p}).first().m) for p in PERSONAS}
base.update({"conversation_ids": CONVS, "gateway_request_ids_200": OK_REQS})

def missing(test, what, ev, personas):
    """Absence is PENDING until log_latency_hours has passed for every persona concerned, FAIL after."""
    young = [p for p in personas if LAST.get(p) is None or NOW - LAST[p] < LATENCY]
    if young:
        rec.record(test, "PENDING", f"{what}; within the {LATENCY} log latency window for {young}. Rerun later.", ev)
    else:
        rec.record(test, "FAIL", f"{what}; the {LATENCY} log latency window has passed for every persona", ev)

def in_list(col, key):
    return f"array_contains(split(:{key}, ','), {col})"

# COMMAND ----------
# O-01 / O-03: Genie audit events matched on the recorded conversation IDs (allowed personas), and on the lab
# space for the outsider (a denied start has no conversation)
try:
    if not SPACE:
        raise ValueError("Genie Agent ID unknown")
    AUD = """FROM system.access.audit WHERE service_name = 'aibiGenie' AND workspace_id = :ws
             AND event_date BETWEEN to_date(:start) AND to_date(:stop) AND event_time BETWEEN :start AND :stop"""
    found = {}
    for p in ("clinician", "pii_reader"):
        if not CONVS[p]:
            continue
        hit = spark.sql(f"""SELECT DISTINCT request_params.conversation_id AS c {AUD}
                  AND user_identity.email = :app AND response.status_code = 200 AND request_params.space_id = :space
                  AND {in_list('request_params.conversation_id', 'convs')}""",
                  args={**args, "app": APPS[p], "space": SPACE, "convs": ",".join(CONVS[p])}).collect()
        found[p] = sorted({r.c for r in hit})
    out_ok = spark.sql(f"""SELECT count(*) AS n {AUD} AND user_identity.email = :app AND request_params.space_id = :space
                           AND response.status_code = 200""", args={**args, "app": APPS["outsider"], "space": SPACE}).first().n
    out_den = spark.sql(f"""SELECT count(*) AS n {AUD} AND user_identity.email = :app AND request_params.space_id = :space
                            AND action_name = :action AND response.status_code IN (403, 404)""",
                        args={**args, "app": APPS["outsider"], "space": SPACE, "action": START_ACTION}).first().n
    ev = {**base, "space_id": SPACE, "found_conversations": found, "outsider_successes": out_ok, "outsider_denied_starts": out_den}
    no_rec = [p for p in ("clinician", "pii_reader") if not CONVS[p]]
    lacking = [p for p in ("clinician", "pii_reader") if CONVS[p] and set(found.get(p, [])) != set(CONVS[p])]
    if out_ok:
        rec.record("O-01", "FAIL", f"{out_ok} successful Genie event(s) from the outsider on the lab agent", ev)
    elif no_rec:
        rec.record("O-01", "SKIP", f"no Genie conversation IDs recorded for {no_rec} in this suite", ev)
    elif lacking:
        missing("O-01", f"recorded conversation IDs not yet in the audit log for {lacking}", ev, lacking)
    else:
        rec.record("O-01", "PASS", "every recorded Genie conversation of clinician and PII reader is in the audit log "
                   "(status 200, lab agent); no outsider success", ev)
    if out_ok:
        rec.record("O-03", "FAIL", f"the outsider has {out_ok} successful Genie event(s) on the lab agent", ev)
    elif out_den:
        rec.record("O-03", "PASS", f"{out_den} denied {START_ACTION} event(s) (403/404) from the outsider on the lab agent; no success", ev)
    else:
        missing("O-03", f"no denied {START_ACTION} event from the outsider", ev, ["outsider"])
except Exception as e:
    for t in ("O-01", "O-03"):
        rec.record(t, "SKIP", f"Can't read system.access.audit: {first_line(e)}")

# COMMAND ----------
# O-02: gateway usage rows matched on the recorded request IDs and the full 3-part service name
try:
    found = {}
    for p in ("clinician", "pii_reader"):
        if OK_REQS[p]:
            found[p] = sorted({r.request_id for r in spark.sql(f"""SELECT request_id FROM system.ai_gateway.usage
                WHERE workspace_id = :ws AND event_time BETWEEN :start AND :stop AND requester = :app
                  AND (service_name = :svc OR endpoint_name = :svc) AND {in_list('request_id', 'ids')}""",
                args={**args, "app": APPS[p], "svc": SERVICE, "ids": ",".join(OK_REQS[p])}).collect()})
    ev = {**base, "service": SERVICE, "found_request_ids": found}
    no_rec = [p for p in ("clinician", "pii_reader") if not OK_REQS[p]]
    lacking = [p for p in ("clinician", "pii_reader") if OK_REQS[p] and set(found.get(p, [])) != set(OK_REQS[p])]
    if no_rec:
        rec.record("O-02", "SKIP", f"no successful gateway request IDs recorded for {no_rec}", ev)
    elif lacking:
        missing("O-02", f"recorded request IDs not yet in system.ai_gateway.usage for {lacking}", ev, lacking)
    else:
        rec.record("O-02", "PASS", f"every recorded successful request of clinician and PII reader is in system.ai_gateway.usage on {SERVICE}", ev)
except Exception as e:
    rec.record("O-02", "SKIP", f"Can't read system.ai_gateway.usage: {first_line(e)}")

# COMMAND ----------
# G-04: the configured service's own inference table (resolved from its config), matched on the G-01 request ID.
# Configuration or permission errors: SKIP with the reason. Only an error that names the exact configured table
# as not found / not ready (and its schema exists) is PENDING within log_latency_hours, FAIL after.
stage, tname = "config", ""
try:
    cfg = w.api_client.do("GET", f"/api/2.1/unity-catalog/model-services/{SERVICE}").get("config", {}).get("inference_table") or {}
    tname = str(cfg.get("table", "")).removeprefix("tables/")
    if not tname or cfg.get("is_deleted"):
        raise ValueError(f"no inference table configured on {SERVICE}")
    tbl = ".".join(q(x) for x in tname.split("."))
    stage = "table"
    pc = [r.col_name for r in spark.sql(f"DESCRIBE TABLE {tbl}").collect() if r.col_name and not r.col_name.startswith("#")]
    tcol = next(c for c in ("event_time", "request_time", "timestamp", "timestamp_ms") if c in pc)
    texpr = f"timestamp_millis({tcol})" if tcol == "timestamp_ms" else tcol
    found = {p: (spark.sql(f"SELECT count(*) AS n FROM {tbl} WHERE request_id = :rid AND requester = :app",
                           args={"rid": G01_REQ[p], "app": APPS[p]}).first().n if G01_REQ[p] else None)
             for p in ("clinician", "pii_reader")}
    out_rows = spark.sql(f"SELECT count(*) AS n FROM {tbl} WHERE {texpr} BETWEEN :start AND :stop AND requester = :o",
                         args={"start": START, "stop": STOP, "o": APPS["outsider"]}).first().n
    ev = {**base, "table": tname, "g01_request_ids": G01_REQ, "found": found, "outsider_rows": out_rows}
    no_rec = [p for p, v in found.items() if v is None]
    lacking = [p for p, v in found.items() if v == 0]
    if out_rows:
        rec.record("G-04", "FAIL", f"{out_rows} inference row(s) from the outsider in the suite window", ev)
    elif no_rec:
        rec.record("G-04", "SKIP", f"no successful G-01 request ID recorded for {no_rec}", ev)
    elif lacking:
        missing("G-04", f"G-01 request ID not yet in {tname} for {lacking}", ev, lacking)
    else:
        rec.record("G-04", "PASS", f"the G-01 request of clinician and PII reader is in {tname} (exact request ID); none from the outsider", ev)
except Exception as e:
    def schema_exists(n):
        try:
            return spark.catalog.databaseExists(n)
        except Exception:
            return False
    if stage == "table" and table_not_available(e, tname) and \
            schema_exists(".".join(tname.split(".")[:2])):   # the schema is there, the table isn't (yet)
        missing("G-04", f"inference table {tname} not available yet ({first_line(e, 150)})",
                {**base, "table": tname, "error": first_line(e, 300)}, ["clinician", "pii_reader"])
    else:
        rec.record("G-04", "SKIP", f"Can't resolve or read the inference table ({stage}): {first_line(e, 200)}")
rec.flush()
