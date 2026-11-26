# Databricks notebook source
# MAGIC %md
# MAGIC # 30 · Persona probe (DEMO mode)
# MAGIC This notebook runs **as a persona service principal** (the job's `run_as`). Every query,
# MAGIC Genie question and model call below is evaluated with that identity's permissions, so the
# MAGIC same code gives different, expected results per persona.
# MAGIC
# MAGIC | Persona | Groups | Expect |
# MAGIC |---|---|---|
# MAGIC | clinician | clinicians | masked PII, one facility |
# MAGIC | pii_reader | PII readers + all facilities | cleartext, four facilities |
# MAGIC | outsider | outsiders (no Genie permission) | masked, one facility, Genie denied |
# MAGIC
# MAGIC **Evidence rules** (no false PASS):
# MAGIC * values are compared with the exact seeded fixtures (visits 4, 8, 12 in Polyclinic North);
# MAGIC * a denial only counts if the error names the object or privilege under test (or is the 403/404 for that
# MAGIC   exact securable); any other permission error is SKIP with the reason;
# MAGIC * results go to this persona's own staging table; the collector merges them into `test_results`.

# COMMAND ----------
# MAGIC %md Pinned dependencies (one place for every notebook: `src/constraints.txt`).

# COMMAND ----------
# MAGIC %pip install -q -c ../constraints.txt databricks-sdk pyyaml databricks-mcp

# COMMAND ----------
dbutils.library.restartPython()

# COMMAND ----------
import os, sys, json, time, uuid
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
import re
from checks import (Recorder, classify, error_kind, first_line, compare_fixture, facility_label, fixture_row, masked,
                    all_fixture_nrics, FIXTURE_IDS, MASKED_RE, NRIC_RE, PROBE_LOG_DDL, name, q, pii_leaks,
                    suite_run_id, lab_names, synthetic_sentence, patient_label)
SERVICE_RE = re.compile(r"^[A-Za-z0-9_-]{1,255}\.[A-Za-z0-9_-]{1,255}\.[A-Za-z0-9_-]{1,255}$")
from databricks.sdk import WorkspaceClient

for k, d in [("persona", "clinician"), ("catalog", "main"), ("schema", "genie_governance_lab"),
             ("genie_space_id", ""), ("gateway_endpoint", ""), ("allow_human", "false"), ("warehouse_id", ""),
             ("expected_policy", ""), ("fm_endpoint", "databricks-claude-sonnet-4-6"),
             ("chat_model", "models/system.ai.databricks-claude-sonnet-4-6"), ("resource_prefix", "lab"),
             ("expected_principal", "")]:
    dbutils.widgets.text(k, d)
N = lab_names(dbutils.widgets.get("resource_prefix"))
CHAT_MODEL = dbutils.widgets.get("chat_model").strip()
if not re.fullmatch(r"models/[A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+", CHAT_MODEL):
    raise ValueError(f"chat_model must look like models/system.ai.<model>: {CHAT_MODEL!r}")
EXPECTED_PRINCIPAL = dbutils.widgets.get("expected_principal").strip()
P = dbutils.widgets.get("persona")
C, S = name(dbutils.widgets.get("catalog")), name(dbutils.widgets.get("schema"))
T = f"{q(C)}.{q(S)}"
SPACE, ENDPOINT = dbutils.widgets.get("genie_space_id"), dbutils.widgets.get("gateway_endpoint")
WH = dbutils.widgets.get("warehouse_id")
EXPECTED_POLICY = dbutils.widgets.get("expected_policy").strip()
FM_ENDPOINT = name(dbutils.widgets.get("fm_endpoint"), "fm_endpoint")
for v in (SPACE, WH):
    if v:
        name(v)
RUN = suite_run_id(dbutils)
w = WorkspaceClient()
me = w.current_user.me()
print("Running as:", me.user_name or me.display_name, "| persona:", P, "| suite:", RUN)
human = "@" in (me.user_name or "")
if human and dbutils.widgets.get("allow_human") == "true":
    P = "facilitator"        # dry run: results are INFO and go to test_results_facilitator

# Guard: results only mean something if this runs as the persona's service principal. A human identity (the
# deployer, when run_as isn't bound) is always refused; if the runner passed expected_principal (the persona's
# application ID), the identity must be exactly that one.
wrong = None
if human and P != "facilitator":
    wrong = (f"not running as a persona service principal (running as {me.user_name}); create the personas "
             "(00_run_lab action=personas, or scripts/bootstrap_personas.sh) and bind run_as")
elif EXPECTED_PRINCIPAL and P != "facilitator" and (me.user_name or "") != EXPECTED_PRINCIPAL:
    wrong = f"running as {me.user_name}, not the expected {P} principal {EXPECTED_PRINCIPAL[:8]}…; run_as is bound to another identity"
if wrong:
    # Fail the job (non-success) and write nothing: an identity that isn't the persona must not write the persona's
    # results. The trusted runner (00_run_lab) records the SKIPs and keeps the step retryable; on the CLI path the
    # failed job shows the reason.
    raise RuntimeError(f"LAB_IDENTITY_GUARD: {wrong}")
rec = Recorder(spark, C, S, persona=P, run_id=RUN)   # only after the identity guard

setup = {r.key: r.value for r in spark.table(f"{T}.lab_setup_status").collect()}
SPACE = SPACE or setup.get("genie_space_id", "")
print("Genie Agent:", SPACE or "not set up")
IDS = ", ".join(str(i) for i in FIXTURE_IDS)

# COMMAND ----------
# MAGIC %md ## Layer 2 · Data (direct SQL), compared with the seeded fixtures

# COMMAND ----------
VISITS = f"{C}.{S}.patient_visits"
try:
    fx = [r.asDict() for r in spark.sql(
        f"SELECT visit_id, nric, patient_name, phone, facility FROM {T}.patient_visits WHERE visit_id IN ({IDS})").collect()]
    obs, ev = compare_fixture(fx, ("nric", "patient_name"))
    rec.verdict("D-01", obs, ev)
    if setup.get("abac_policy") != "ok":
        rec.record("D-03", "SKIP", f"ABAC policy not created in setup: {setup.get('abac_policy')}")
    else:
        obs3, ev3 = compare_fixture(fx, ("phone",))
        rec.verdict("D-03", obs3, ev3)
    facs = [r.facility for r in spark.sql(f"SELECT DISTINCT facility FROM {T}.patient_visits").collect()]
    rec.verdict("D-02", facility_label(facs), {"facilities": sorted(f for f in facs if f)})
except Exception as e:
    k = classify(e, targets=[VISITS, "patient_visits"], privileges=["SELECT"])
    for t in ("D-01", "D-02", "D-03"):
        rec.outcome(t, k, {"error": first_line(e)}, what="patient_visits")

def can_read(table):
    """allowed (rows returned) / denied (error names the table) / unrelated / other / empty."""
    try:
        n = len(spark.sql(f"SELECT * FROM {T}.{q(table)} LIMIT 1").collect())
        return ("allowed" if n else "empty"), ""
    except Exception as e:
        return classify(e, targets=[f"{C}.{S}.{table}", table], privileges=["SELECT"]), first_line(e)
(raw, raw_err), (msk, msk_err) = can_read("clinical_notes_raw"), can_read("clinical_notes_masked")
ev = {"raw": raw, "masked": msk, "raw_error": raw_err, "masked_error": msk_err}
if raw in ("unrelated", "other") or msk in ("unrelated", "other"):
    rec.record("D-04", "SKIP" if "unrelated" in (raw, msk) else "FAIL",
               f"inconclusive: raw={raw}, masked={msk} (errors don't name the notes tables)", ev)
else:
    rec.verdict("D-04", f"{raw} on raw, {msk} on masked", ev)

# COMMAND ----------
# MAGIC %md ## Layer 4 · Genie Agent (Conversation API, as this persona)
# MAGIC Assertions look at the **data Genie returned**, not the wording, because answers vary.

# COMMAND ----------
GENIE_TARGETS = [SPACE]           # the exact space ID; the word "space" alone is not attribution
GENIE_CONVS = []                   # conversation IDs of this persona's questions (matched by lab_evidence)
def ask_genie(question, retries=1):
    """Return (status, columns, rows, text). status: ok / denied / unrelated / other / no_query."""
    text = ""
    for _ in range(retries + 1):
        try:
            msg = w.genie.start_conversation_and_wait(SPACE, question)
        except Exception as e:
            return classify(e, targets=GENIE_TARGETS, privileges=["CAN_RUN", "CAN RUN"]), [], [], first_line(e)
        GENIE_CONVS.append(msg.conversation_id)
        text = " ".join((a.text.content or "") for a in (msg.attachments or []) if a.text)
        for a in msg.attachments or []:
            if a.query:
                res = w.genie.get_message_attachment_query_result(SPACE, msg.conversation_id, msg.message_id, a.attachment_id)
                sr = res.statement_response
                cols = [c.name for c in sr.manifest.schema.columns]
                return "ok", cols, ((sr.result.data_array if sr.result else None) or []), text
        time.sleep(3)
    return "no_query", [], [], text

def col_values(cols, data, needle):
    idx = next((i for i, c in enumerate(cols) if needle in c.lower()), None)
    return None if idx is None else [r[idx] for r in data]

if not SPACE:
    for t in ("A-01", "A-02", "A-03", "A-04"):
        rec.record(t, "SKIP", "genie_space_id parameter is empty")
else:
    st, cols, data, text = ask_genie("Show the nric and facility columns for 5 rows of patient_visits")
    if st == "denied":
        for t in ("A-01", "A-02", "A-03", "A-04"):
            rec.verdict(t, "denied", {"error": text})
    elif st in ("unrelated", "other"):
        for t in ("A-01", "A-02", "A-03", "A-04"):
            rec.outcome(t, st, {"error": text}, what="the Genie Agent")
    else:
        # Genie answering in text without a query (e.g. it declines a PII request) still means access was granted.
        rec.verdict("A-03", "allowed" if st in ("ok", "no_query") else st, {"status": st, "text": text[:300]})
        vals = col_values(cols, data, "nric") if st == "ok" else None
        if vals:
            known = all_fixture_nrics()
            obs = ("masked" if all(v and MASKED_RE.fullmatch(str(v)) for v in vals)
                   else "cleartext" if all(v in known for v in vals) else "mismatch")
            rec.verdict("A-01", obs, {"columns": cols, "sample": vals[:3], "rows": len(vals)})
        else:
            rec.record("A-01", "INFO" if st == "ok" else "SKIP",
                       "inconclusive: Genie's query returned no NRIC column, so there is nothing to compare" if st == "ok"
                       else "Genie answered without a query (it may decline PII requests); no NRIC values to check",
                       {"columns": cols, "text": text[:300]})

        st2, cols2, data2, text2 = ask_genie("How many visits per facility? One row per facility.")
        facs = col_values(cols2, data2, "facility") if st2 == "ok" else None
        if facs is None:
            rec.record("A-02", "FAIL" if st2 == "ok" else "SKIP", f"no facility column returned ({st2})", {"columns": cols2, "text": text2[:300]})
        else:
            rec.verdict("A-02", facility_label(facs), {"facilities": sorted({f for f in facs if f}), "rows": data2[:5]})

        st3, cols3, data3, text3 = ask_genie("Show 3 clinical notes with their full note text")
        notes = col_values(cols3, data3, "note") if st3 == "ok" else None
        notes = [n for n in (notes or []) if n is not None and str(n).strip()]   # empty text doesn't count
        if not notes:
            rec.record("A-04", "FAIL" if st3 == "ok" else "SKIP",
                       f"no note text returned ({st3}); A-04 needs a successful, non-empty answer", {"columns": cols3, "text": text3[:300]})
        else:
            blob = json.dumps(notes) + " " + text3
            leaked = NRIC_RE.findall(blob)
            rec.verdict("A-04", "no cleartext NRIC in answer" if not leaked else "cleartext NRIC in answer",
                        {"leaked_count": len(leaked), "rows": len(notes)})

rec.record("A-05", "MANUAL", "Agent mode: ask the same questions in the UI (docs/runbook.md, step 4.3)",
           {"genie_conversation_ids": sorted({c for c in GENIE_CONVS if c}), "space_id": SPACE})

# COMMAND ----------
# MAGIC %md ## Layer 3 · Unity Gateway model service (G-01 … G-03)
# MAGIC The lab's own model service `<prefix>_chat`: EXECUTE for clinicians and PII readers only, and a rate
# MAGIC limit of 2 requests per minute for the clinicians group. Calls go through the Unity Gateway API.

# COMMAND ----------
import requests
HOST = w.config.host.rstrip("/")
SERVICE = ENDPOINT or f"{C}.{S}.{N['chat']}"
if not SERVICE_RE.fullmatch(SERVICE):
    raise ValueError(f"gateway_endpoint must be a catalog.schema.service name: {SERVICE!r}")
SERVICE_TARGETS = [SERVICE]        # full 3-part name only
GATEWAY_REQUESTS = []              # x-request-id of every gateway call (= request_id in usage / inference tables)
RATE_LIMITED = P == "clinician"          # only the clinicians group has the 2/min limit

def find_policy(obj, path=""):
    """Service-policy decision records in a structured response: [{path, record}] where record is the dict
    under a key like databricks_service_policy (one record or a list of records)."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if "service_policy" in k.lower():
                for rec_ in (v if isinstance(v, list) else [v]):
                    if isinstance(rec_, dict):
                        out.append({"path": f"{path}.{k}", "record": rec_})
            else:
                out += find_policy(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, x in enumerate(obj):
            out += find_policy(x, f"{path}[{i}]")
    return out

def is_deny(record):
    """Explicit ENFORCED deny: action/decision/result is deny or block, and the record isn't in a log / monitor /
    dry-run mode."""
    vals = [str(record.get(k, "")).lower() for k in ("action", "decision", "result")]
    mode = " ".join(str(record.get(k, "")).lower() for k in ("mode", "enforcement", "enforcement_mode"))
    if any(m in mode for m in ("log", "monitor", "dry", "audit")) or any(v in ("log", "monitor") for v in vals):
        return False
    return any(v in ("deny", "denied", "block", "blocked") for v in vals)

def gateway(prompt, max_tokens=20):
    """Returns (kind, evidence). kind: allowed / blocked / blocked_other_policy / denied / rate_limited /
    unrelated / other."""
    r = requests.post(f"{HOST}/ai-gateway/mlflow/v1/chat/completions", timeout=120,
                      headers={**w.config.authenticate(), "Content-Type": "application/json"},
                      json={"model": SERVICE, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens})
    body = r.text[:800]
    try:
        j = r.json()
    except Exception:
        j = None
    pol = find_policy(j) if j is not None else []
    rid = r.headers.get("x-request-id")
    GATEWAY_REQUESTS.append({"request_id": rid, "prompt": prompt, "http_status": r.status_code})
    ev = {"http_status": r.status_code, "body": body[:400], "request_id": rid}
    if pol:
        ev["policy_decisions"] = [p["record"] for p in pol[:5]]
        completed = any((c or {}).get("finish_reason") not in (None, "content_filter") for c in (j or {}).get("choices") or [])
        ev["successful_completion_in_response"] = completed
        # PASS only for ONE record naming the expected policy with an enforced deny, and no completion returned.
        if EXPECTED_POLICY and not completed and any(p["record"].get("name") == EXPECTED_POLICY and is_deny(p["record"])
                                                     for p in pol):
            return "blocked", ev
        if any(is_deny(p["record"]) for p in pol):
            return "blocked_unconfirmed", ev
    if r.status_code == 200 and j and j.get("choices"):
        return "allowed", ev
    if r.status_code == 429:
        return "rate_limited", ev
    if r.status_code in (403, 404):  # UC hides securables you have no privilege on: 404 "does not exist"
        return classify(body, targets=SERVICE_TARGETS, privileges=["EXECUTE"], status_code=r.status_code), ev
    return "other", ev

def pause_for_rate_limit():
    if RATE_LIMITED:
        print("waiting 65 s so the clinicians' 2-per-minute limit resets"); time.sleep(65)

g1, ev1 = gateway("Reply with the single word OK.")
rec.outcome("G-01", g1, {"service": SERVICE, **ev1, **({} if g1 == "allowed" else {"error": ev1.get("body", "")})}, what=SERVICE)

# COMMAND ----------
# MAGIC %md ## AI Functions (F-01 … F-03)
# MAGIC * F-01: AI Functions are governed by EXECUTE on `system.ai` (all users by default; restricting it
# MAGIC   needs the AI Functions UC permissions feature enabled by the account team), so this is recorded.
# MAGIC * F-02: `ai_query` over the masked table, on the seeded fixture rows. The model only receives what Unity
# MAGIC   Catalog lets the caller read, so a clinician can't extract NRICs through an LLM.
# MAGIC * F-03: `ai_query` through the lab's **custom** model service. The Unity Gateway docs say `ai_query` supports
# MAGIC   only Databricks-provided models; model services you create aren't supported yet. Recorded as INFO.

# COMMAND ----------
try:
    out = spark.sql("SELECT ai_summarize('Patient visited the polyclinic for an asthma review and was advised to return in 3 months.') AS s").first().s
    rec.record("F-01", "INFO", f"ai_summarize allowed: {out[:120]}", {"note": "restrict via EXECUTE on system.ai once AI Functions UC permissions are enabled"})
except Exception as e:
    rec.record("F-01", "INFO", f"ai_summarize {error_kind(e)}: {first_line(e, 150)}")

try:
    rows = spark.sql(f"""SELECT visit_id, nric, ai_query(:ep, concat('Repeat exactly this value and nothing else: ', nric)) AS out
                         FROM {T}.patient_visits WHERE visit_id IN ({IDS})""", args={"ep": FM_ENDPOINT}).collect()
    outs = {int(r.visit_id): (r.out or "").strip() for r in rows}
    seen_nric = {int(r.visit_id): r.nric for r in rows}          # what UC let this persona read
    leaked = [i for i, o in outs.items() if fixture_row(i)["nric"] in o]
    ev = {"outputs": outs, "endpoint": FM_ENDPOINT, "leaked_fixture_ids": leaked}
    missing = sorted(set(FIXTURE_IDS) - {i for i, o in outs.items() if o})
    sent_clear = [i for i, v in seen_nric.items() if v == fixture_row(i)["nric"]]
    sent_masked = all(seen_nric.get(i) == masked(fixture_row(i)["nric"]) for i in FIXTURE_IDS)
    ev.update({"values_sent": seen_nric, "cleartext_sent_ids": sent_clear})
    if P != "pii_reader" and (sent_clear or leaked):
        rec.verdict("F-02", "cleartext NRIC reached the model", ev)        # cleartext sent or echoed: conclusive
    elif P != "pii_reader" and not sent_masked:
        rec.record("F-02", "INFO", "inconclusive: the values sent to the model aren't the expected masked values", ev)
    elif missing:
        rec.record("F-02", "INFO", f"inconclusive: no model output for fixture rows {missing}", ev)
    elif all(outs[i] == masked(fixture_row(i)["nric"]) for i in FIXTURE_IDS):
        rec.verdict("F-02", "no cleartext NRIC reached the model", ev)
    elif all(outs[i] == fixture_row(i)["nric"] for i in FIXTURE_IDS):
        rec.verdict("F-02", "cleartext NRIC reached the model", ev)
    else:
        rec.record("F-02", "INFO", "inconclusive: the model didn't echo exactly the value it was given "
                   "(refusal or rewording)", {**ev, "values_sent": seen_nric})
except Exception as e:
    rec.outcome("F-02", classify(e, targets=[FM_ENDPOINT, VISITS, "patient_visits"], privileges=["EXECUTE", "SELECT"]),
                {"error": first_line(e)}, what=f"{FM_ENDPOINT} / patient_visits")

try:
    out = spark.sql("SELECT ai_query(:svc, 'Reply with the single word OK') AS r", args={"svc": SERVICE}).first().r
    rec.record("F-03", "INFO", "allowed: ai_query reached the custom model service (docs say this isn't supported yet; recheck the docs)",
               {"response": str(out)[:200]})
except Exception as e:
    msg = first_line(e)
    low = msg.lower()
    # The docs say ai_query doesn't support model services you create yet; the workspace reports this as
    # "ai_query through UC Model Services is not enabled". Both mean: not supported for this route.
    if any(k in low for k in ("not supported", "unsupported", "not yet supported", "model services is not enabled",
                              "uc model services")) and classify(e, targets=SERVICE_TARGETS, privileges=["EXECUTE"]) != "denied":
        obs = "not supported for this route"
    else:
        obs = classify(e, targets=SERVICE_TARGETS, privileges=["EXECUTE"])
    rec.record("F-03", "INFO", f"{obs}: {msg[:200]}", {"error": msg,
               "doc": "ai_query support for Unity Gateway is only available for Databricks-provided models"})

pause_for_rate_limit()

# COMMAND ----------
# MAGIC %md ## Guardrail and rate limit (G-02, G-03)

# COMMAND ----------
if g1 == "allowed":
    g2, ev2 = gateway("Summarise: " + synthetic_sentence(), 60)   # fictional label, invalid NRIC, built at runtime
    if g2 == "blocked":
        rec.verdict("G-02", "blocked", ev2)
    elif g2 == "blocked_unconfirmed":
        rec.record("G-02", "INFO", "blocked, but no decision record names the expected policy "
                   f"({EXPECTED_POLICY or 'expected_policy not set'}) with an explicit deny; see policy_decisions", ev2)
    elif g2 == "allowed":
        rec.record("G-02", "MANUAL", f"Prompt with an NRIC was not blocked. Attach the NRIC service policy to {N['chat']} in Enforce mode (docs/manual-steps.md M-04) and rerun.", ev2)
    else:
        rec.outcome("G-02", g2, {**ev2, "error": ev2.get("body", "")}, what=SERVICE)
elif g1 == "denied":
    rec.verdict("G-02", "denied", {"note": "model call denied before any policy check", **ev1})
else:
    rec.outcome("G-02", g1 if g1 in ("unrelated", "other") else "other", {**ev1, "error": ev1.get("body", "")}, what=SERVICE)

pause_for_rate_limit()
calls = [gateway("Reply with OK.", 5) for _ in range(3)]
kinds = [k for k, _ in calls]
ev3 = {"calls": kinds, "http": [e["http_status"] for _, e in calls], "gateway_requests": GATEWAY_REQUESTS}
if P == "clinician":
    g3 = "2 allowed then rate_limited" if kinds == ["allowed", "allowed", "rate_limited"] else ", ".join(kinds)
    rec.verdict("G-03", g3, ev3)
elif all(k == "denied" for k in kinds):
    rec.verdict("G-03", "denied", ev3)
elif all(k == "allowed" for k in kinds):
    rec.verdict("G-03", "allowed", ev3)
elif "rate_limited" in kinds:
    rec.record("G-03", "INFO" if P != "outsider" else "FAIL",
               f"HTTP 429 that isn't the lab's clinicians limit ({', '.join(kinds)})", ev3)
else:
    rec.record("G-03", "FAIL", f"unrecognised outcome sequence: {', '.join(kinds)}", ev3)

# COMMAND ----------
# MAGIC %md ## Vector / AI Search (V-01, V-02)

# COMMAND ----------
IDX = setup.get("vs_index", "")
if not IDX or IDX.startswith(("skip", "error")):
    rec.record("V-01", "SKIP", f"vector search index not set up: {IDX or 'missing'}")
    rec.record("V-02", "SKIP", f"vector search index not set up: {IDX or 'missing'}")
else:
    try:
        rows = spark.sql("""SELECT visit_id, note FROM vector_search(index => :idx,
                             query_text => :qt, num_results => 5)""",
                         args={"idx": IDX, "qt": f"{patient_label(FIXTURE_IDS[0])} asthma follow-up"}).collect()
        rec.verdict("V-01", "allowed", {"hits": len(rows)})
        texts = [r.note for r in rows if r.note and r.note.strip()]       # null/empty text doesn't count
        if not texts:
            rec.record("V-02", "FAIL", f"query succeeded but returned no hits with text ({len(rows)} rows)", {"hits": len(rows)})
        else:
            blob = " ".join(texts)
            leaked = pii_leaks(blob)
            rec.verdict("V-02", "no cleartext PII in results" if not leaked else "cleartext PII in results",
                        {"hits": len(rows), "hits_with_text": len(texts), "sample": [(r.note or "")[:120] for r in rows[:2]], "leaks": leaked[:5]})
    except Exception as e:
        from checks import named_privileges
        k = classify(e, targets=[IDX, IDX.split(".")[-1]], privileges=["SELECT"])
        ev = {"error": str(e)[:1500], "privileges_named": sorted(named_privileges(e))}
        rec.outcome("V-01", k, ev, what=IDX)
        rec.outcome("V-02", k, ev, what=IDX)

# COMMAND ----------
# MAGIC %md ## Agent tools over MCP (T-01, T-02)
# MAGIC * T-01: the managed UC-functions MCP server exposes `<schema>_tools`. Only clinicians have EXECUTE on
# MAGIC   the function, so only they can call the tool. The exact tool is selected by name and its result checked.
# MAGIC * T-02: the managed DBSQL MCP server runs SQL as the calling identity on the selected warehouse, so masks
# MAGIC   apply. Pending statements are polled; the returned rows are compared with the fixtures.

# COMMAND ----------
from concurrent.futures import ThreadPoolExecutor
def flat(e):
    """Unwrap ExceptionGroups from the MCP client so the real cause is recorded."""
    subs = getattr(e, "exceptions", None)
    return " <- ".join(flat(x) for x in subs) if subs else f"{type(e).__name__}: {str(e)[:300]}"
if WH and not str(w.warehouses.get(WH).state).endswith("RUNNING"):
    w.warehouses.start_and_wait(WH)

def mcp(url, fn):
    """Run an MCP client call in a worker thread (the notebook already has an event loop); one retry for timeouts."""
    from databricks_mcp import DatabricksMCPClient
    last = None
    for i in range(3):                       # managed MCP servers can be slow on a cold start
        try:
            c = DatabricksMCPClient(server_url=url, workspace_client=w)
            return ThreadPoolExecutor(1).submit(fn, c).result(timeout=300)
        except Exception as e:
            last = e
            if "timeout" not in flat(e).lower():
                raise
            time.sleep(20 * (i + 1))
    raise last

def result_text(res):
    parts = []
    for c in getattr(res, "content", None) or []:
        parts.append(getattr(c, "text", None) or str(c))
    return "\n".join(parts) if parts else str(res)

FN = f"{C}.{S}_tools.{N['tool_fn']}"
TOOL_NAMES = {f"{C}__{S}_tools__{N['tool_fn']}", FN, N["tool_fn"]}
def call_tool(c):
    names = [t.name for t in c.list_tools()]
    tool = next((n for n in names if n in TOOL_NAMES), None)
    listed = tool is not None
    tool = tool or f"{C}__{S}_tools__{N['tool_fn']}"       # not listed: call the exact name anyway
    try:
        res = c.call_tool(tool, {"facility": "Polyclinic North"})
    except Exception as e:
        if "timeout" in flat(e).lower():
            raise                                   # let mcp() retry
        k = classify(flat(e), targets=[FN], privileges=["EXECUTE"])
        return (k if k == "denied" else "unclear"), {"listed_tools": names, "tool": tool, "error": flat(e)[:300],
                "note": "not listed and the direct call failed without naming EXECUTE on the function" if k != "denied" else ""}
    text = result_text(res)
    if getattr(res, "isError", False):
        k = classify(text, targets=[FN], privileges=["EXECUTE"])
        return (k if k == "denied" else "unclear"), {"tool": tool, "listed": listed, "error": text[:300]}
    try:
        value = (json.loads(text).get("rows") or [[None]])[0][0]
    except Exception:
        value = None
    if value == 40:
        return "allowed", {"tool": tool, "result": text[:300], "value": value}
    return "unclear", {"tool": tool, "result": text[:300], "value": value, "expected_value": 40}
try:
    t1, ev = mcp(f"{HOST}/api/2.0/mcp/functions/{C}/{S}_tools", call_tool)
    if t1 == "unclear":
        rec.record("T-01", "INFO", "inconclusive: no exact structured value 40 and no denial naming EXECUTE on the function", ev)
    else:
        rec.outcome("T-01", t1, ev, what=FN)
except Exception as e:
    msg = flat(e)[:400]
    rec.outcome("T-01", classify(msg, targets=[FN, f"{C}/{S}_tools", N["tool_fn"]], privileges=["EXECUTE"]),
                {"error": msg}, what=FN)

def rows_from(obj):
    """Find a list of rows in a JSON result: data_array [[...]], data_typed_array [{values: [...]}], rows."""
    if isinstance(obj, dict) and isinstance(obj.get("data_typed_array"), list):
        return [[next(iter(v.values()), None) if isinstance(v, dict) else v for v in (r.get("values") or [])]
                for r in obj["data_typed_array"]]
    if isinstance(obj, dict):
        for k in ("data_array", "data_typed_array", "rows", "data", "result"):
            if k in obj:
                r = rows_from(obj[k])
                if r is not None:
                    return r
        for v in obj.values():
            r = rows_from(v)
            if r is not None:
                return r
    if isinstance(obj, list) and obj and all(isinstance(x, (list, dict)) for x in obj):
        return obj
    return None

def sql_tool(c):
    tools = {t.name: t for t in c.list_tools()}
    tname = next((n for n in ("execute_sql_read_only", "execute_sql") if n in tools), None)
    if not tname:
        return "other", {"listed_tools": list(tools), "note": "no execute_sql tool"}
    schema = getattr(tools[tname], "inputSchema", None) or {}
    props = list((schema.get("properties") if isinstance(schema, dict) else {}) or {})
    arg = next((p for p in props if p.lower() in ("query", "statement", "sql")), "query")
    args = {arg: f"SELECT visit_id, nric FROM {q(C)}.{q(S)}.patient_visits WHERE visit_id IN ({IDS}) ORDER BY visit_id"}
    wh_arg = next((p for p in props if "warehouse" in p.lower()), None)
    if wh_arg and WH:
        args[wh_arg] = WH
    res0 = c.call_tool(tname, args)
    if getattr(res0, "isError", False):
        return "info", {"tool": tname, "note": "MCP tool returned isError", "result": result_text(res0)[:400]}
    text = result_text(res0)
    polls = 0
    for _ in range(20):                      # poll pending statements
        try:
            j = json.loads(text)
        except Exception:
            j = None
        state, sid = "", None
        if isinstance(j, dict):
            st_ = j.get("status")
            state = str(j.get("state") or (st_.get("state") if isinstance(st_, dict) else st_) or "").upper()
            sid = j.get("statement_id")
        if sid and state in ("PENDING", "RUNNING") and "poll_sql_result" in tools:
            time.sleep(3); polls += 1
            text = result_text(c.call_tool("poll_sql_result", {"statement_id": sid}))
            continue
        break
    ev = {"tool": tname, "warehouse_arg": wh_arg, "polls": polls, "result": text[:400], "state": state}
    if state and state not in ("SUCCEEDED",):
        return "info", {**ev, "note": f"statement not in a terminal success state ({state})"}
    rws = rows_from(j) if j is not None else None
    if rws is None:
        k = classify(text, targets=[VISITS, "patient_visits"], privileges=["SELECT"])
        return (k if k in ("denied", "unrelated") else "unclear"), ev
    fx = []
    for r in rws:
        vals = list(r.values()) if isinstance(r, dict) else list(r)
        if len(vals) >= 2:
            fx.append({"visit_id": int(vals[0]), "nric": vals[1]})
    obs, fev = compare_fixture(fx, ("nric",))
    ev["warehouse_used"] = warehouse_of(sid) if sid else None
    return obs, {**ev, **fev}

def warehouse_of(statement_id):
    """Warehouse that ran a statement, from this identity's query history (None if not visible)."""
    try:
        from databricks.sdk.service.sql import QueryFilter
        for qi in w.query_history.list(filter_by=QueryFilter(statement_ids=[statement_id])).res or []:
            return qi.warehouse_id
    except Exception as e:
        print("query history not readable:", str(e)[:150])
    return None
try:
    t2, ev = mcp(f"{HOST}/api/2.0/mcp/sql", sql_tool)
    if t2 == "unclear":
        rec.record("T-02", "MANUAL", "DBSQL MCP response had no structured rows", ev)
    elif t2 == "info":
        rec.record("T-02", "INFO", ev.get("note", "inconclusive"), ev)
    elif not WH:
        rec.record("T-02", "INFO", f"{t2}, but no warehouse_id was given, so the warehouse can't be confirmed", ev)
    elif t2 in ("masked", "cleartext", "mismatch", "no rows") and WH and ev.get("warehouse_used") != WH:
        rec.record("T-02", "INFO", f"{t2}, but the requested warehouse {WH} couldn't be confirmed "
                   f"(query history shows {ev.get('warehouse_used')})", ev)
    else:
        rec.outcome("T-02", t2, ev, what="patient_visits")
except Exception as e:
    msg = flat(e)[:400]
    k = classify(msg, targets=[VISITS, "patient_visits", "/mcp/sql"], privileges=["SELECT"])
    rec.outcome("T-02", k if k != "other" else "unavailable", {"error": msg}, what="the DBSQL MCP server")

# COMMAND ----------
# MAGIC %md ## Model serving / agent endpoint and MLflow traces (S-01, X-01)

# COMMAND ----------
ep_name, ep_id = setup.get("agent_endpoint", ""), setup.get("agent_endpoint_id", "")
if not ep_id or ep_id.startswith(("skip", "error")):
    rec.record("S-01", "SKIP", f"agent endpoint not set up: {ep_id or 'missing'}")
else:
    try:
        r = requests.post(f"{HOST}/serving-endpoints/{name(ep_name)}/invocations", timeout=180,
                          headers={**w.config.authenticate(), "Content-Type": "application/json"},
                          json={"input": [{"role": "user", "content": "Reply with OK"}]})
        ev = {"http_status": r.status_code, "body": r.text[:600]}
        k = "allowed" if r.status_code == 200 else classify(r.text[:400], targets=[ep_name, ep_id],
                                                             privileges=["CAN_QUERY", "CAN QUERY", "query"], status_code=r.status_code)
        rec.outcome("S-01", k, {**ev, "error": r.text[:600]}, what=ep_name)
    except Exception as e:
        rec.outcome("S-01", classify(e, targets=[ep_name, ep_id], privileges=["CAN_QUERY"]), {"error": first_line(e)}, what=ep_name)

exp_id, trace_id = setup.get("experiment_id", ""), setup.get("trace_id", "")
if not exp_id or exp_id.startswith(("skip", "error")):
    rec.record("X-01", "SKIP", f"lab experiment not set up: {exp_id or 'missing'}")
else:
    try:
        w.experiments.get_experiment(exp_id)
        traces = w.api_client.do("GET", "/api/2.0/mlflow/traces", query={"experiment_ids": exp_id, "max_results": 100})
        ids = {t.get("request_id") or t.get("trace_id") for t in traces.get("traces", [])}
        found = bool(trace_id) and trace_id in ids
        n = len(traces.get("traces", []))
        if found:
            rec.verdict("X-01", "allowed", {"traces": n, "seeded_trace": trace_id})
        else:
            rec.record("X-01", "FAIL",
                       f"experiment readable but the seeded trace {trace_id or '(not recorded)'} wasn't returned ({n} traces)",
                       {"traces": n, "seeded_trace": trace_id})
    except Exception as e:
        rec.outcome("X-01", classify(e, targets=[exp_id, "experiment"], privileges=["VIEW", "CAN_READ"]), {"error": first_line(e)},
                    what=f"experiment {exp_id}")

# COMMAND ----------
# MAGIC %md ## Who can build AI services (B-01 … B-05, B-04b)
# MAGIC Each persona tries to **create** the objects that open new AI routes: a model service, an external
# MAGIC model provider service, a connection, an MCP service and a classic serving endpoint with an external model.
# MAGIC Only clinicians hold `CREATE SERVICE` (on the tools schema); nobody holds `CREATE CONNECTION`, and only
# MAGIC clinicians hold `USE CONNECTION`, on the builders' connection only (B-04b; B-04 uses one nobody may use). Every probe object gets a unique per-run name, recorded in the evidence (teardown uses it);
# MAGIC anything created is deleted straight away and the deletion is **verified**. If cleanup fails the result is
# MAGIC FAIL "cleanup pending", never PASS.

# COMMAND ----------
BUILD = f"{C}.{S}_tools"
TAG = P.replace("-", "_")
RID = uuid.uuid4().hex[:8]
CONN = setup.get("mcp_connection", "")
UC = "/api/2.1/unity-catalog"
api = w.api_client.do

def gone(get, tries=12):
    for _ in range(tries):
        try:
            get()
        except Exception as e:
            if any(k in str(e).lower() for k in ("does not exist", "not found", "404", "resource_does_not_exist")):
                return True
            raise
        time.sleep(5)
    return False

PROBE_LOG = f"{T}.probe_log_{P}"    # this persona's own append-only log (MODIFY for its group only)
if P == "facilitator":
    spark.sql(PROBE_LOG_DDL.format(table=PROBE_LOG))
def log_probe(test, kind, obj_name, state, detail=""):
    """Append a probe event. 'intent' is written BEFORE the create, so a crash between create and cleanup still
    leaves a record the collector turns into a manifest item for teardown."""
    spark.sql(f"INSERT INTO {PROBE_LOG} VALUES (:r, :rid, :t, :k, :n, :s, :d, current_timestamp())",
              args={"r": RUN, "rid": RID, "t": test, "k": kind, "n": obj_name, "s": state, "d": str(detail)[:500]})

def attempt(test, kind, obj_name, create, delete, get, privileges, extra_targets=(), note=None):
    """Returns 'allowed' (created and deletion verified), 'cleanup_pending', or the denial classification."""
    ev = {"probe_object": {"kind": kind, "name": obj_name}, **({"note": note} if note else {})}
    log_probe(test, kind, obj_name, "intent")
    try:
        create()
    except Exception as e:
        log_probe(test, kind, obj_name, "create_failed", flat(e)[:300])
        msg = flat(e)[:400]
        k = classify(msg, targets=[BUILD, *extra_targets], privileges=privileges)
        if k == "unrelated":
            rec.record(test, "SKIP", "generic permission error; the missing privilege is not identifiable from the "
                       f"response (expected it to name {', '.join(privileges)} or the object)", {**ev, "error": msg})
        else:
            rec.outcome(test, k, {**ev, "error": msg}, what=f"{BUILD} / {', '.join(privileges)}")
        return k
    log_probe(test, kind, obj_name, "created")
    try:
        delete()
        ok = gone(get)
        ev["cleanup"] = "deleted (verified)" if ok else "delete sent but object still exists"
    except Exception as e:
        ok = False
        ev["cleanup"] = f"cleanup failed: {flat(e)[:200]}"
    ev["cleanup_verified"] = ok
    log_probe(test, kind, obj_name, "deleted_verified" if ok else "cleanup_pending", ev["cleanup"])
    if ok:
        rec.verdict(test, "allowed", ev)
    else:
        rec.record(test, "FAIL", f"allowed; cleanup pending for {obj_name} (teardown deletes it)", ev)
    return "allowed" if ok else "cleanup_pending"

MS, PROV, CN, MCPS, MCPB = (f"probe_{TAG}_{RID}_{s}" for s in ("ms", "provider", "conn", "mcp", "mcpb"))
BCONN = setup.get("mcp_connection_builders", "")
attempt("B-01", "model-services", f"{BUILD}.{MS}",
        lambda: api("POST", f"{UC}/model-services", query={"parent": f"schemas/{BUILD}", "model_service_id": MS},
                    body={"config": {"routing": {"destinations": [{"name": "primary",
                          "destination_type": "DESTINATION_TYPE_PAY_PER_TOKEN_FOUNDATION_MODEL",
                          "pay_per_token_config": {"model": CHAT_MODEL}}]}}}),
        lambda: api("DELETE", f"{UC}/model-services/{BUILD}.{MS}"),
        lambda: api("GET", f"{UC}/model-services/{BUILD}.{MS}"), ["CREATE_SERVICE"])
attempt("B-02", "model-provider-services", f"{BUILD}.{PROV}",
        lambda: api("POST", f"{UC}/model-provider-services",
                    query={"parent": f"schemas/{BUILD}", "model_provider_service_id": PROV},
                    body={"comment": "GenAI governance lab probe (dummy key, placeholder URL)", "config": {
                          "provider_type": "EXTERNAL_MODEL_PROVIDER_TYPE_CUSTOM",
                          "targets": [{"model": "gpt-4o", "native_api_types": ["openai/v1/chat/completions"]}],
                          "custom": {"direct": {"base_url": "https://api.example.com/v1", "api_key": {"plaintext": "lab-dummy-key"}}}}}),
        lambda: api("DELETE", f"{UC}/model-provider-services/{BUILD}.{PROV}"),
        lambda: api("GET", f"{UC}/model-provider-services/{BUILD}.{PROV}"), ["CREATE_SERVICE"])
attempt("B-03", "connections", f"{BUILD}.{CN}",
        lambda: api("POST", f"{UC}/connections", body={
                    "name": CN, "connection_type": "HTTP", "parent": f"schemas/{BUILD}",
                    "options": {"host": "https://mcp.example.com", "port": "443", "base_path": "/mcp", "bearer_token": "lab-dummy"}}),
        lambda: api("DELETE", f"{UC}/connections/{BUILD}.{CN}"),
        lambda: api("GET", f"{UC}/connections/{BUILD}.{CN}"), ["CREATE_CONNECTION"])
def mcp_attempt(test, svc, conn, note=None):
    # Creation alone is the evidence: the probe never lists tools, so nothing is sent to the placeholder host.
    return attempt(test, "mcp-services", f"{BUILD}.{svc}",
                   lambda: api("POST", f"{UC}/mcp-services", query={"parent": f"schemas/{BUILD}", "mcp_service_id": svc},
                               body={"config": {"source_connection": {"name": f"connections/{conn}"}}}),
                   lambda: api("DELETE", f"{UC}/mcp-services/{BUILD}.{svc}"),
                   lambda: api("GET", f"{UC}/mcp-services/{BUILD}.{svc}"), ["USE_CONNECTION", "CREATE_SERVICE"], [conn],
                   note=note)

# B-04b (allowed path): the builders' connection, where clinicians hold USE CONNECTION. Run first so B-04 can cite it.
if not BCONN or BCONN.startswith(("skip", "error")):
    b04b = None
    rec.record("B-04b", "SKIP", BCONN[len("skip: "):] if BCONN.startswith("skip: connection name collision")
               else f"builders' MCP connection not set up: {BCONN or 'missing'}", {"connection_status": BCONN})
else:
    b04b = mcp_attempt("B-04b", MCPB, BCONN)
# B-04 (no USE CONNECTION for anyone). If B-04b was allowed, B-04's evidence says so, but B-04's own verdict
# still comes only from its own response (a generic 403 stays SKIP).
B04_NOTE = ("B-04b: this persona registered an MCP service on a connection where it holds USE CONNECTION, so "
            "CREATE SERVICE + USE CONNECTION is sufficient; B-04's verdict is not inferred from that") if b04b == "allowed" else None
if not CONN or CONN.startswith(("skip", "error")):
    rec.record("B-04", "SKIP", f"lab MCP connection not set up: {CONN or 'missing'}", {"note": B04_NOTE} if B04_NOTE else None)
else:
    mcp_attempt("B-04", MCPS, CONN, note=B04_NOTE)
# B-05: a classic serving endpoint with an external model is a route to an outside LLM that bypasses
# Unity Gateway model provider services. Target control: denied. Without the workspace setting
# Enforce Unity Gateway, any user with the workspace-access entitlement can create one (a finding). With it on,
# creation fails with "Creating external model endpoint '<name>' is disabled for this workspace. Please use
# Unity Gateway."; PASS needs that clause naming this probe's exact endpoint (checks.py). A generic 403 is SKIP.
EP = f"{N['probe_endpoint']}-{TAG.replace('_', '-')}-{RID}"
attempt("B-05", "serving-endpoints", EP,
        lambda: api("POST", "/api/2.0/serving-endpoints", body={"name": EP, "config": {"served_entities": [{
                    "name": "ext", "external_model": {"name": "gpt-4o", "provider": "custom", "task": "llm/v1/chat",
                    "custom_provider_config": {"custom_provider_url": "https://api.example.com/v1",
                                               "bearer_token_auth": {"token_plaintext": "lab-dummy"}}}}]}}),
        lambda: api("DELETE", f"/api/2.0/serving-endpoints/{EP}"),
        lambda: api("GET", f"/api/2.0/serving-endpoints/{EP}"), ["workspace-access", "entitlement", "serving endpoint"], [EP])

rec.flush()
