"""Shared helpers for the GenAI governance lab notebooks.

Results
-------
Admin (deployer) notebooks write to <catalog>.<schema>.test_results. Each persona writes only to its
own staging table test_results_<persona>, on which only that persona's group has MODIFY; the trusted
collector (40_collect, runs as the deployer) merges staging rows into test_results. No persona can
change another persona's results.

Columns: run_id, test_id, layer, title, persona, status, expected, observed, evidence (JSON), doc_url, ts
run_id is the SUITE run ID shared by setup, personas and audit of one demo or audit run.

Statuses: PASS, FAIL, SKIP (prerequisite missing, with reason), MANUAL (UI/account step),
INFO (recorded, no verdict). The dashboard adds MISSING / PENDING for tests that have no row yet.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import time
import uuid

import yaml

# Same patterns the probes, the masked-notes check and the guardrails use.
NRIC_RE = re.compile(r"\b[STFGM]\d{7}[A-Z]\b")
# Phone numbers: SG levels 3/6/8/9 plus the reserved level 5 (fixtures), with an optional 65 / +65 / (+65) prefix and an
# optional space, dot or hyphen between the groups. Boundaries: not inside a longer alphanumeric token or number; a
# hyphen next to a letter is a separator ("<number>-no answer"), a hyphen next to a digit is not. Same pattern as
# scripts/check_no_literal_pii.py and, in Java syntax, PHONE_SQL (tests keep all three in step).
PHONE_PATTERN = r"(?<![0-9A-Za-z+.])(?<![0-9]-)(?:(?:\(\+?65\)|\+?65)[ .-]?)?[35689][0-9]{3}[ .-]?[0-9]{4}(?![0-9A-Za-z]|-[0-9])"
PHONE_RE = re.compile(PHONE_PATTERN)
# The same pattern for Spark SQL regexp_replace (Java regex), written without backslashes so it can be embedded in a SQL
# string literal unchanged.
PHONE_SQL = "(?<![0-9A-Za-z+.])(?<![0-9]-)(?:(?:[(][+]?65[)]|[+]?65)[ .-]?)?[35689][0-9]{3}[ .-]?[0-9]{4}(?![0-9A-Za-z]|-[0-9])"
MASK_PREFIX = "*****"
MASKED_RE = re.compile(r"^\*{5}.$")
PERSONAS = ("clinician", "pii_reader", "outsider")

RESULTS_COLS = ["run_id", "test_id", "layer", "title", "persona", "status", "expected",
                "observed", "evidence", "doc_url", "ts"]
RESULTS_DDL = """CREATE TABLE IF NOT EXISTS {table} (
    run_id STRING, test_id STRING, layer INT, title STRING, persona STRING,
    status STRING, expected STRING, observed STRING, evidence STRING,
    doc_url STRING, ts TIMESTAMP)"""

_HERE = os.path.dirname(os.path.abspath(__file__))
_EXPECT = os.path.join(_HERE, "..", "..", "expectations", "controls.yml")

# --------------------------------------------------------------------------------------------
# Identifier validation (item: every widget that becomes a SQL identifier is validated and quoted)
# --------------------------------------------------------------------------------------------
_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-]{0,254}$")          # catalog / schema / table
_PRINCIPAL_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9 _.@+\-]{0,254}$")  # group / user / SP
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,255}$")


def name(value: str, what: str = "name") -> str:
    """Validate a catalog/schema/table name; raise ValueError if it isn't a plain identifier."""
    if not isinstance(value, str) or not _NAME_RE.fullmatch(value):
        raise ValueError(f"invalid {what}: {value!r} (allowed: letters, digits, _ and -)")
    return value


def q(value: str, what: str = "name") -> str:
    """Validated, backtick-quoted identifier component."""
    return f"`{name(value, what)}`"


def principal(value: str, what: str = "principal") -> str:
    """Validate a group / user / service principal name (no quotes, backticks or control chars)."""
    if not isinstance(value, str) or not _PRINCIPAL_RE.fullmatch(value):
        raise ValueError(f"invalid {what}: {value!r}")
    return value


def qp(value: str, what: str = "principal") -> str:
    return f"`{principal(value, what)}`"


def sql_str(value: str, what: str = "literal") -> str:
    """A string literal for places where Spark can't bind parameters (DDL bodies, GRANT).
    Only validated principal-like values are accepted, so no escaping games are possible."""
    return "'" + principal(value, what) + "'"


def tag_key(value: str) -> str:
    if not isinstance(value, str) or not _TAG_RE.fullmatch(value):
        raise ValueError(f"invalid tag key: {value!r}")
    return value


def app_id(value: str) -> str:
    if not _UUID_RE.fullmatch(value or ""):
        raise ValueError(f"invalid application ID: {value!r}")
    return value


def run_id_ok(value: str) -> str:
    if not _RUN_ID_RE.fullmatch(value or ""):
        raise ValueError(f"invalid run id: {value!r}")
    return value


_TARGET_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_WORKSPACE_ID_RE = re.compile(r"^[0-9]{1,20}$")


def target_ok(value: str) -> str:
    """Bundle target name (CLI argument)."""
    if not isinstance(value, str) or not _TARGET_RE.fullmatch(value):
        raise ValueError(f"invalid target: {value!r}")
    return value


def workspace_id_ok(value: str) -> str:
    if not isinstance(value, str) or not _WORKSPACE_ID_RE.fullmatch(value):
        raise ValueError(f"invalid workspace id (digits only): {value!r}")
    return value


def persona_ok(value: str) -> str:
    if value not in PERSONAS + ("admin", "facilitator"):
        raise ValueError(f"unknown persona: {value!r}")
    return value


# --------------------------------------------------------------------------------------------
# Synthetic fixtures. Mirrors the generator in 00_setup_data.py so probes compare exact values.
# People are obviously fictional labels ("Test Patient 017"), generated at runtime; no realistic names.
# NRIC-like values use check letters that are never valid for the S prefix, so none is a real NRIC.
# Phone numbers start with 5, a level IMDA's National Numbering Plan reserves ("to be planned"): they can't be
# dialled in Singapore, and PHONE_RE covers level 5 as well as the allocated levels 3, 6, 8 and 9.
# --------------------------------------------------------------------------------------------
FACILITIES = ["Polyclinic North", "Polyclinic East", "General Hospital West", "Community Hospital Central"]
N_PATIENT_LABELS = 97
PATIENT_LABEL_RE = re.compile(r"\bTest Patient \d{3}\b")
INVALID_CHECK_LETTERS = "KLMNPQRUVWY"      # S/T check letters are A-J and Z; none of these is valid
FIXTURE_IDS = (4, 8, 12)                   # all in Polyclinic North, visible to every persona
DEFAULT_FACILITY = "Polyclinic North"
N_VISITS = 2000


def patient_label(i: int) -> str:
    """Fictional patient label for visit i (same formula as the SQL generator in 00_setup_data)."""
    return f"Test Patient {(i % N_PATIENT_LABELS) + 1:03d}"


def nric_like(i: int) -> str:
    """NRIC-shaped value with an invalid check letter (never a real NRIC)."""
    return "S" + str(1000000 + (i * 7919) % 8999999).zfill(7) + INVALID_CHECK_LETTERS[i % 11]


def phone_like(i: int) -> str:
    """8-digit phone-shaped value on the reserved level 5 (can't be dialled in Singapore)."""
    return "5" + str((i * 104729) % 9999999).zfill(7)


def fixture_row(i: int) -> dict:
    return {"visit_id": i, "nric": nric_like(i), "patient_name": patient_label(i), "phone": phone_like(i),
            "facility": FACILITIES[i % 4]}


def synthetic_sentence(i: int = FIXTURE_IDS[0], condition: str = "asthma") -> str:
    """One synthetic sentence with a fictional label, an invalid NRIC and a level-5 phone number (built at runtime)."""
    r = fixture_row(i)
    return f"Patient {r['patient_name']} (NRIC {r['nric']}, tel {r['phone']}) seen at {DEFAULT_FACILITY} for {condition}."


def pii_leaks(text: str) -> list:
    """NRIC-like values, phone numbers and patient labels left in a text."""
    t = str(text or "")
    return NRIC_RE.findall(t) + PHONE_RE.findall(t) + PATIENT_LABEL_RE.findall(t)


def mask_verdict(out) -> tuple[str, list]:
    """D-06 / D-07: PASS only if ai_mask returned non-blank text and no NRIC, phone number or patient label is left in
    it (the same rule as note_issue for the masked notes)."""
    text = str(out or "")
    lk = pii_leaks(text)
    return ("PASS" if text.strip() and not lk else "FAIL"), lk       # whitespace-only output is no output


def note_issue(visit_id, note):
    """Masked-notes validation (15_masked_notes): None for a clean, non-empty note, else what is wrong with it."""
    n = note or ""
    hits = pii_leaks(n)
    if hits or not n.strip():
        return {"visit_id": visit_id, "hits": len(hits), "empty": not n.strip()}
    return None


def masked(v: str) -> str:
    return MASK_PREFIX + (v[-1:] if v else "")


def all_fixture_nrics() -> set:
    return {fixture_row(i)["nric"] for i in range(1, N_VISITS + 1)}


def compare_fixture(rows: list[dict], cols: tuple[str, ...]) -> tuple[str, dict]:
    """Compare returned fixture rows with the seeded values.
    Returns ('masked' | 'cleartext' | 'mismatch' | 'no rows', evidence)."""
    if not rows:
        return "no rows", {"returned": 0}
    got, ok_mask, ok_clear = [], True, True
    for r in rows:
        exp = fixture_row(int(r["visit_id"]))
        for c in cols:
            v = r.get(c)
            got.append({"visit_id": r["visit_id"], "col": c, "value": v})
            ok_mask &= v == masked(exp[c])
            ok_clear &= v == exp[c]
    ids = sorted(int(r["visit_id"]) for r in rows)
    ev = {"fixture_ids": list(FIXTURE_IDS), "returned_ids": ids, "values": got[:9]}
    if ids != sorted(FIXTURE_IDS):
        return "mismatch", {**ev, "reason": "not every fixture row was returned"}
    return ("masked" if ok_mask else "cleartext" if ok_clear else "mismatch"), ev


def facility_label(facilities) -> str:
    s = {f for f in facilities if f}
    if s == {DEFAULT_FACILITY}:
        return "1 facility"
    if s == set(FACILITIES):
        return "4 facilities"
    return f"unexpected facility set ({len(s)})" if s else "no rows"


# --------------------------------------------------------------------------------------------
# Catalogue and suite registration
# --------------------------------------------------------------------------------------------
def load_catalogue() -> dict:
    with open(_EXPECT) as f:
        return yaml.safe_load(f)


def catalogue_rows(cat: dict) -> list[tuple]:
    """(test_id, persona, layer, title, expected, modes, source) for every expected result."""
    out = []
    for tid, meta in cat.items():
        exp = meta.get("expected") or {}
        personas = meta.get("personas") or list(exp)
        modes = meta.get("modes") or (["audit", "demo"] if personas == ["admin"] else ["demo"])
        for p in personas:
            out.append((tid, p, int(meta.get("layer", 0)), meta.get("title", tid), str(exp.get(p, "")),
                        ",".join(modes), meta.get("source", "")))
    return out


def register_suite(spark, catalog: str, schema: str, run_id: str, mode: str):
    """Record the suite (first writer wins) and refresh test_catalogue from expectations/controls.yml."""
    T = f"{q(catalog)}.{q(schema)}"
    spark.sql(f"CREATE TABLE IF NOT EXISTS {T}.suite_runs (run_id STRING, mode STRING, started_at TIMESTAMP)")
    spark.sql(f"""MERGE INTO {T}.suite_runs t USING (SELECT :rid AS run_id, :mode AS mode, current_timestamp() AS started_at) s
                  ON t.run_id = s.run_id WHEN NOT MATCHED THEN INSERT *""", args={"rid": run_id, "mode": mode})
    spark.createDataFrame(catalogue_rows(load_catalogue()),
                          "test_id STRING, persona STRING, layer INT, title STRING, expected STRING, modes STRING, source STRING") \
         .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{T}.test_catalogue")


def suite_run_id(dbutils) -> str:
    """Suite run ID from the job parameter, else this job run's ID (standalone run)."""
    try:
        dbutils.widgets.text("suite_run_id", "")
        v = dbutils.widgets.get("suite_run_id").strip()
    except Exception:
        v = ""
    if v:
        return run_id_ok(v)
    jr = os.environ.get("DATABRICKS_JOB_RUN_ID")
    if not jr:
        try:
            ctx = json.loads(dbutils.notebook.entry_point.getDbutils().notebook().getContext().safeToJson())
            jr = (ctx.get("attributes") or {}).get("jobRunId") or (ctx.get("attributes") or {}).get("multitaskParentRunId")
        except Exception:
            jr = None
    return f"job-{jr}" if jr else f"adhoc-{uuid.uuid4().hex[:12]}"


def results_table(catalog: str, schema: str, persona: str) -> str:
    persona_ok(persona)
    t = "test_results" if persona == "admin" else f"test_results_{persona}"
    return f"{q(catalog)}.{q(schema)}.{t}"


class Recorder:
    """Collects results and writes them at the end (admin -> test_results, persona -> own staging table)."""

    def __init__(self, spark, catalog: str, schema: str, persona: str, run_id: str):
        self.spark = spark
        self.persona = persona_ok(persona)
        self.table = results_table(catalog, schema, persona)
        self.run_id = run_id_ok(run_id)
        self.job_run_id = os.environ.get("DATABRICKS_JOB_RUN_ID", "")
        self.cat = load_catalogue()
        self.rows: list[dict] = []
        if not spark.catalog.tableExists(self.table.replace("`", "")):
            spark.sql(RESULTS_DDL.format(table=self.table))   # works for the deployer only; personas get theirs from setup

    def expected(self, test_id: str) -> str:
        exp = self.cat.get(test_id, {}).get("expected", {}) or {}
        return str(exp.get(self.persona, exp.get("admin", "")))

    def record(self, test_id: str, status: str, observed: str, evidence: dict | None = None):
        meta = self.cat.get(test_id, {})
        ev = dict(evidence or {})
        if self.job_run_id:
            ev.setdefault("job_run_id", self.job_run_id)
        row = dict(
            run_id=str(self.run_id), test_id=test_id, layer=int(meta.get("layer", 0)),
            title=meta.get("title", test_id), persona=self.persona, status=status,
            expected=self.expected(test_id), observed=str(observed)[:2000],
            evidence=json.dumps(ev, default=str)[:8000],
            doc_url=meta.get("doc", ""), ts=_dt.datetime.now(_dt.timezone.utc),
        )
        self.rows.append(row)
        print(f"[{status:6}] {test_id} ({self.persona}): {observed}")

    def verdict(self, test_id: str, observed: str, evidence: dict | None = None):
        """PASS if observed matches this persona's expected value (exact, case-insensitive)."""
        expected = self.expected(test_id)
        if not expected or self.persona == "facilitator":
            status = "INFO"
        else:
            status = "PASS" if observed.strip().lower() == expected.strip().lower() else "FAIL"
        self.record(test_id, status, observed, evidence)

    def outcome(self, test_id: str, kind: str, evidence: dict | None = None, what: str = ""):
        """Turn a classified probe outcome into a result.
        allowed / denied / blocked / rate_limited / masked / ...  -> verdict against the catalogue
        unrelated   permission or not-found error that doesn't name the target -> SKIP
        preview_off / unavailable / geo -> SKIP (prerequisite or residency), reason in evidence
        other       real error -> FAIL (never counted as an expected denial)"""
        ev = evidence or {}
        err = str(ev.get("error", ""))[:200]
        if kind == "unrelated" and GATEWAY_ENFORCED_RE.search(str(ev.get("error", ""))):
            self.record(test_id, "SKIP", "a legacy serving endpoint this test relies on is disabled by the workspace's "
                        f"Enforce Unity Gateway setting, so the control under test can't be observed: {err}", ev)
        elif kind == "unrelated":
            self.record(test_id, "SKIP", f"error does not name {what or 'the target'}, so it isn't the control under test: {err}", ev)
        elif kind in ("preview_off", "unavailable", "geo"):
            self.record(test_id, "SKIP", f"{kind}: {err}", ev)
        elif kind == "other":
            self.record(test_id, "FAIL", f"error: {err}", ev)
        else:
            self.verdict(test_id, kind, ev)

    def flush(self):
        if not self.rows:
            return
        self.spark.createDataFrame(self.rows).select(*RESULTS_COLS).write.mode("append").saveAsTable(self.table)
        print(f"Wrote {len(self.rows)} results to {self.table} (run_id={self.run_id})")


def is_masked(values) -> bool:
    vals = [v for v in values if v is not None]
    return bool(vals) and all(MASKED_RE.fullmatch(str(v)) for v in vals)


def is_cleartext_nric(values) -> bool:
    vals = [v for v in values if v is not None]
    return bool(vals) and all(NRIC_RE.fullmatch(str(v)) for v in vals)


# --------------------------------------------------------------------------------------------
# Error classification
# --------------------------------------------------------------------------------------------
_GEO = ("cross-geo", "cross geo", "geography", " geo ", "geo.", "in this region", "region for")
_PERM = ("permission", "not authorized", "unauthorized", "forbidden", "403", "does not have", "do not have",
         "insufficient", "access denied", "permission_denied", "no access")
_NOTFOUND = ("does not exist", "not found", "404", "resource_does_not_exist", "not_found")


def error_kind(exc) -> str:
    """Coarse classification of an error (no target check). Prefer classify()."""
    msg = str(exc).lower()
    # Residency first: in-Geo refusals can arrive wrapped in PERMISSION_DENIED
    if any(k in msg for k in _GEO):
        return "geo"
    if any(k in msg for k in _PERM):
        return "denied"
    if "not enabled" in msg or "is disabled" in msg or "preview" in msg:
        return "preview_off"
    if any(k in msg for k in ("not available", "not supported", "unsupported", "compliance security profile")):
        return "unavailable"
    return "other"


_PRIVS = ("USE CATALOG", "USE SCHEMA", "USE CONNECTION", "SELECT", "MODIFY", "EXECUTE", "CREATE SERVICE",
          "CREATE CONNECTION", "CREATE TABLE", "CREATE FUNCTION", "CREATE SCHEMA", "MANAGE", "ALL PRIVILEGES",
          "CAN RUN", "CAN QUERY", "CAN USE", "CAN READ", "CAN VIEW", "CAN MANAGE")
_PRIV_ALT = "|".join(p.replace(" ", r"[ _]") for p in sorted(_PRIVS, key=len, reverse=True))
_NAME = r"[`'\"\[]?([A-Za-z0-9_`.-]*[A-Za-z0-9_`])[`'\"\]]?"
# Denial clauses that name BOTH what is missing and on which securable. Each yields (privilege or None, name).
_CLAUSES = [
    # "Principal 'x' does not have CREATE SERVICE on Schema 'a.b'", "User does not have SELECT on Table `a`.`b`.`c`"
    re.compile(rf"does not have (?:the )?(?P<priv>{_PRIV_ALT})(?: privilege)? on (?:\w+ )?{_NAME}(?=[\s.,;:)]|$)", re.I),
    # Genie: "Unable to get space [<space id>]. Caused by User ... does not have read permission"
    re.compile(rf"unable to get space \[(?P<name>[0-9a-f]+)\]", re.I),
    # UC securable hidden: "'a.b.lab_chat' does not exist.", "Function 'a.b.f' not found"
    re.compile(rf"'(?P<name>[A-Za-z0-9_.-]+)' does not exist", re.I),
    re.compile(rf"function '(?P<name>[A-Za-z0-9_.-]+)' not found", re.I),
    # Vector search: "Insufficient permissions for UC entity a.b.idx"
    re.compile(rf"insufficient permissions for UC entity {_NAME}(?=[\s\\\"',.]|$)", re.I),
    # Serving: "You do not have permission to query the endpoint. name: ep"
    re.compile(rf"permission to (?P<priv>query) the endpoint\. name: (?P<name>[A-Za-z0-9_.-]+)", re.I),
    # MLflow: "does not have permission to 'View' experiment with id 123"
    re.compile(rf"permission to '(?P<priv>\w+)' experiment with id (?P<name>\d+)", re.I),
    # Enforce Unity Gateway (B-05): "Creating external model endpoint '<name>' is disabled for this workspace.
    # Please use Unity Gateway." Names the exact endpoint and the enforcement; no privilege is involved.
    re.compile(r"creating external model endpoint '(?P<name>[A-Za-z0-9_.-]+)' is disabled for this workspace\.?"
               r"\s*please use unity gateway", re.I),
]

# Any legacy route the Enforce Unity Gateway setting switched off (pay-per-token endpoints, external-model creation)
GATEWAY_ENFORCED_RE = re.compile(r"is disabled for this workspace\.?\s*please use unity gateway", re.I)


def _norm(p: str) -> str:
    return re.sub(r"[_\s]+", " ", p.upper()).strip()


def _norm_name(n: str) -> str:
    return n.replace("`", "").strip("'\"[]").lower()


def message_only(msg) -> str:
    """The error message without JVM / Python stack traces (frames like '.execute(' aren't privileges)."""
    m = str(msg).split("JVM stacktrace")[0].split("Traceback (most recent call last)")[0]
    return re.split(r"\n\s+at [\w.$]+\(", m)[0]


def denial_clause(msg) -> tuple | None:
    """(privilege or None, securable name) from the first recognised denial clause, else None."""
    m = message_only(msg)
    for rx in _CLAUSES:
        hit = rx.search(m)
        if hit:
            d = hit.groupdict()
            name = d.get("name") or next((g for g in hit.groups()[::-1] if g), "")
            return (_norm(d["priv"]) if d.get("priv") else None), _norm_name(name)
    return None


def named_privileges(msg: str) -> set:
    """Privileges named anywhere in the message (evidence only; classify() uses the denial clause)."""
    up = " " + _norm(message_only(msg)) + " "
    return {p for p in _PRIVS if re.search(r"(?<![A-Z])" + re.escape(p) + r"(?![A-Z])", up)}


def classify(err, targets=(), privileges=(), status_code: int | None = None) -> str:
    """Classify an error against the securable and privilege under test.

    denied      a recognised denial clause names a securable equal to one of `targets` AND, if the clause
                names a privilege, it is one of `privileges` (e.g. 'does not have SELECT on Table a.b.t')
    unrelated   any other permission / not-found error: missing USE CATALOG / USE SCHEMA, SELECT on another
                table, 'Workspace access denied', a generic 403 (missing privilege not identifiable)
    geo / preview_off / unavailable / other  as error_kind()
    """
    msg = message_only(err)
    low = msg.lower()
    if any(k in low for k in _GEO):
        return "geo"
    clause = denial_clause(msg)
    perm = any(k in low for k in _PERM) or status_code == 403
    nf = any(k in low for k in _NOTFOUND) or status_code == 404
    if clause:
        priv, nm = clause
        want = {_norm_name(t) for t in targets if t}
        expected = {_norm(p) for p in privileges if p}
        if nm in want and (priv is None or priv in expected or not expected):
            return "denied"
        return "unrelated"
    if perm or nf:
        return "unrelated"
    k = error_kind(msg)
    return k if k != "denied" else "unrelated"


def first_line(e, n: int = 300) -> str:
    return (str(e).strip().splitlines() or [""])[0][:n]


# --------------------------------------------------------------------------------------------
# "Gone": one rule for the runner (00_run_lab) and the teardown notebook (99_teardown)
# --------------------------------------------------------------------------------------------
NOT_FOUND_CODES = {"SCIM_404", "NOT_FOUND", "RESOURCE_DOES_NOT_EXIST", "SCHEMA_DOES_NOT_EXIST", "TABLE_DOES_NOT_EXIST",
                   "TABLE_OR_VIEW_NOT_FOUND", "RESOURCE_NOT_FOUND"}
# Spark SQL error classes (a structured field of the exception, not its message) that mean "doesn't exist".
SPARK_NOT_FOUND_CLASSES = {"TABLE_OR_VIEW_NOT_FOUND", "SCHEMA_NOT_FOUND", "ROUTINE_NOT_FOUND", "POLICY_NOT_FOUND",
                           "UC_POLICY_NOT_FOUND", "NO_SUCH_POLICY", "RESOURCE_DOES_NOT_EXIST"}


def _spark_error_class(e) -> str:
    for attr in ("getCondition", "getErrorClass"):
        fn = getattr(e, attr, None)
        if callable(fn):
            try:
                v = fn()
            except Exception:  # noqa: BLE001
                v = None
            if v:
                return str(v).upper()
    return ""


def is_not_found(e) -> bool:
    """True only for a STRUCTURED not-found: the SDK's NotFound / ResourceDoesNotExist, a not-found error_code, an HTTP
    404 status on the response object, or a not-found Spark error class. Message text is never evidence that
    something is gone (a permission error or a 503 may well say 'not found')."""
    if type(e).__name__ in ("NotFound", "ResourceDoesNotExist"):
        return True
    if str(getattr(e, "error_code", "") or "").upper() in NOT_FOUND_CODES:
        return True
    if getattr(getattr(e, "response", None), "status_code", None) == 404:
        return True
    return _spark_error_class(e) in SPARK_NOT_FOUND_CLASSES


def attempt(fn) -> tuple:
    """Run a teardown step: ('ok', None), ('gone', e) only for a structured not-found, else ('failed', e). A failed
    step is unresolved: the teardown keeps the manifest and results tables."""
    try:
        fn()
        return "ok", None
    except Exception as e:  # noqa: BLE001 - classified, never swallowed
        return ("gone" if is_not_found(e) else "failed"), e


# --------------------------------------------------------------------------------------------
# Manifest: exactly what the lab created. Teardown deletes only manifest items.
# --------------------------------------------------------------------------------------------
PROBE_LOG_DDL = """CREATE TABLE IF NOT EXISTS {table} (
    run_id STRING, rid STRING, test_id STRING, kind STRING, name STRING, state STRING, detail STRING, ts TIMESTAMP)"""
PROBE_KINDS = {"model-services": "ms", "model-provider-services": "provider", "connections": "conn",
               "mcp-services": "mcp", "serving-endpoints": None}


# Tests that probe a kind another test already uses get their own suffix (B-04b: second MCP service per run).
PROBE_TEST_SUFFIX = {("B-04b", "mcp-services"): "mcpb"}


def expected_probe_name(kind: str, persona: str, rid: str, tools_schema: str, test_id: str | None = None,
                        probe_prefix: str = "glab-probe") -> str | None:
    """The only name a persona's probe of `kind` (for `test_id`) may have in run `rid` (None if the inputs are invalid).
    probe_prefix: lab_names(resource_prefix)['probe_endpoint'] (serving endpoints are workspace-wide)."""
    if kind not in PROBE_KINDS or persona not in PERSONAS + ("facilitator",) or not re.fullmatch(r"[0-9a-f]{8}", rid or ""):
        return None
    if kind == "serving-endpoints":
        if not re.fullmatch(r"[a-z][a-z0-9]{0,15}-probe", probe_prefix or ""):
            return None
        return f"{probe_prefix}-{persona.replace('_', '-')}-{rid}"
    suffix = PROBE_TEST_SUFFIX.get((test_id, kind), PROBE_KINDS[kind])
    return f"{tools_schema}.probe_{persona}_{rid}_{suffix}"


# --------------------------------------------------------------------------------------------
# Configurable names. resource_prefix names every lab object that isn't scoped by the lab schema alone (jobs, the
# model service, connections, policies, functions, index, experiment, probe endpoints); persona_prefix names the
# service principals and account groups. The default prefix "lab" reproduces the names earlier versions used, so
# existing deployments keep working. Tables inside the lab schema keep fixed names (the schema scopes them).
# --------------------------------------------------------------------------------------------
DEFAULT_PREFIX = "lab"
_PREFIX_RE = re.compile(r"^[a-z][a-z0-9]{1,15}$")
JOB_KEYS = ("lab_setup", "lab_audit", "lab_persona_clinician", "lab_persona_pii_reader", "lab_persona_outsider",
            "lab_collect", "lab_evidence", "lab_teardown")
_LEGACY_NAMES = {"tool_fn": "facility_capacity", "nric_guard": "nric_guard_py", "vs_index": "clinical_notes_idx",
                 "experiment": "genie_governance_lab_traces", "mask_fn": "mask_pii", "filter_fn": "facility_filter",
                 "abac_mask_fn": "abac_mask", "probe_endpoint": "glab-probe"}


def prefix_ok(value: str, what: str = "resource_prefix") -> str:
    if not isinstance(value, str) or not _PREFIX_RE.fullmatch(value):
        raise ValueError(f"invalid {what}: {value!r} (2-16 lowercase letters/digits, starting with a letter)")
    return value


def lab_names(prefix: str = DEFAULT_PREFIX) -> dict:
    """Every configurable lab object name for one resource_prefix. prefix 'lab' = the historical names."""
    p = prefix_ok(prefix)
    n = {"chat": f"{p}_chat", "chat_inference_prefix": f"{p}_chat", "mcp_conn": f"{p}_mcp_conn",
         "mcp_conn_builders": f"{p}_mcp_conn_builders", "abac_policy": f"{p}_mask_tagged_pii",
         "sg_policy": f"{p}_mask_sg_identifiers",
         "jobs": {k: p + k[len("lab"):] for k in JOB_KEYS}}
    if p == DEFAULT_PREFIX:
        n.update(_LEGACY_NAMES)
    else:
        n.update({k: f"{p}_{v}" for k, v in _LEGACY_NAMES.items() if k not in ("experiment", "probe_endpoint")})
        n.update({"experiment": f"{p}_traces", "probe_endpoint": f"{p}-probe"})
    return n


# Persona identities. The marker is a technical constant: keep it even though the display name is "GenAI governance lab".
IDENTITY_MARKER = "genie-governance-lab:"
PERSONA_GROUPS = {"clinician": ("clinicians",), "pii_reader": ("pii_readers", "all_facilities"), "outsider": ("outsiders",)}
GROUP_ROLES = ("clinicians", "pii_readers", "all_facilities", "outsiders")


def persona_identities(persona_prefix: str = DEFAULT_PREFIX) -> dict:
    """{'service_principals': {persona: display name}, 'groups': {role: display name}} for one persona_prefix."""
    p = prefix_ok(persona_prefix, "persona_prefix")
    return {"service_principals": {k: f"{p}-{k.replace('_', '-')}" for k in PERSONAS},
            "groups": {r: f"{p}_{r}" for r in GROUP_ROLES}}


def identity_marker(display_name: str) -> str:
    return IDENTITY_MARKER + display_name


def is_marked(display_name, external_id) -> bool:
    """Ownership evidence for an identity: externalId is exactly the lab marker for its display name."""
    return bool(display_name) and external_id == IDENTITY_MARKER + str(display_name)


# The exact (title, description) pairs lab versions have set on their Genie space: the earlier version's and the
# current one (LAB_GENIE_TITLE / LAB_GENIE_DESCRIPTION, used by 16_genie_agent to create and update the space).
LAB_GENIE_TITLE = "Clinic visits (GenAI governance lab)"
LAB_GENIE_DESCRIPTION = "Synthetic visit data. Unity Catalog masks and a row filter apply per user. GenAI governance lab."
LAB_GENIE_PAIRS = (
    ("[lab] Clinic visits (synthetic)",
     "Synthetic visit data. Unity Catalog masks and a row filter apply per user. Genie governance lab."),
    (LAB_GENIE_TITLE, LAB_GENIE_DESCRIPTION),
)


def is_lab_genie_space(title: str | None, description: str | None, created_title: str | None = None) -> bool:
    """Ownership evidence for a Genie space: its title AND description are exactly a pair a lab version set.
    created_title: the title the workspace reported right after THIS lab created the space (from a manifest row with
    creation proof). When the title is already taken (a second deployment in the same workspace), the workspace
    appends a timestamp; that exact recorded title, with the exact lab description, is also accepted.
    Anything else (renamed, edited, similar wording) is not adopted, updated or trashed: manual review."""
    if (title, description) in LAB_GENIE_PAIRS:
        return True
    return (bool(created_title) and title == created_title and str(title).startswith(LAB_GENIE_TITLE)
            and description == LAB_GENIE_DESCRIPTION)


_NOT_READY = ("table_or_view_not_found", "does not exist", "not found", "cannot be found", "provisioning",
              "is being created", "not yet available", "not ready")
_NOT_THIS_TABLE = ("schema_not_found", "no_such_schema", "catalog_not_found", "no_such_catalog", "permission",
                   "does not have", "not authorized", "unauthorized", "forbidden", "403", "insufficient",
                   "access denied", "invalid", "unsupported")


def _norm_ident(text: str) -> str:
    return str(text).replace("`", "").lower()


def table_not_available(err, table: str) -> bool:
    """True only when the error says the exact table `table` (catalog.schema.table; case-insensitive, backticks
    ignored) is not found or not ready yet. A missing catalog or schema, a permission error, another table or any
    other resource is False (SKIP with the reason)."""
    full = _norm_ident(table or "")
    if len(full.split(".")) != 3 or not all(full.split(".")):
        return False
    m = _norm_ident(message_only(str(err)))
    if any(k in m for k in _NOT_THIS_TABLE):
        return False
    names_table = re.search(rf"(?<![\w.]){re.escape(full)}(?!\w|\.\w)", m) is not None
    return names_table and any(k in m for k in _NOT_READY)


# Settings v2 key behind Enforce Unity Gateway: undocumented; discovered from workspace settings metadata; best effort;
# may change without notice; the UI is authoritative.
ENFORCE_GATEWAY_KEY = "enforce_ai_gateway_v3_only"
ENFORCE_GATEWAY_UI = "Settings > Advanced > Enforce Unity Gateway (workspace admin)"


def gateway_enforcement_verdict(listed_keys, setting) -> tuple[str, str]:
    """B-08 from the workspace settings v2 API. `listed_keys`: setting keys from list-workspace-settings-metadata
    (None if the list couldn't be read); `setting`: the get-public-workspace-setting response as a dict, or
    {'error': ...}. PASS only when the effective value is true; FAIL when readable and false; otherwise MANUAL."""
    if listed_keys is not None and ENFORCE_GATEWAY_KEY not in listed_keys:
        return ("MANUAL", "the Enforce Unity Gateway setting isn't offered in this workspace (the docs say it appears "
                f"in workspaces that have legacy AI Gateway endpoints); check {ENFORCE_GATEWAY_UI} or ask the account team")
    if not isinstance(setting, dict) or "error" in setting:
        err = setting.get("error") if isinstance(setting, dict) else setting
        return "MANUAL", f"couldn't read {ENFORCE_GATEWAY_KEY} ({err}); check {ENFORCE_GATEWAY_UI}"
    val = (setting.get("effective_boolean_val") or setting.get("boolean_val") or {}).get("value")
    if val is True:
        return "PASS", (f"{ENFORCE_GATEWAY_KEY} is on: databricks-* pay-per-token endpoints are disabled and new "
                        "external-model and provisioned-throughput serving endpoints can't be created outside Unity "
                        "Gateway. Existing ones stay queryable: inventory them (system.serving.served_entities or the "
                        "serving endpoint list)")
    if val is False:
        return "FAIL", f"{ENFORCE_GATEWAY_KEY} is off: classic serving endpoints with external models can still be created outside Unity Gateway (B-05)"
    return "MANUAL", f"{ENFORCE_GATEWAY_KEY} returned no boolean value; check {ENFORCE_GATEWAY_UI}"


# B-09 legacy AI endpoint inventory (pure; the notebook passes in what it read)
B09_CAP = 20
_PT_KEYS = ("min_provisioned_throughput", "max_provisioned_throughput", "provisioned_model_units")


def served_entity_kind(ep_name: str, ent: dict) -> str:
    """external_model | pt | databricks_ppt | other, for one served entity (or served model) of a serving endpoint."""
    ent = ent or {}
    if ent.get("external_model"):
        return "external_model"
    if any(ent.get(k) is not None for k in _PT_KEYS):     # provisioned throughput (0 is still PT)
        return "pt"
    if ent.get("foundation_model") or str(ep_name or "").startswith("databricks-"):
        return "databricks_ppt" if str(ep_name or "").startswith("databricks-") else "other"
    return "other"


def endpoint_kind(ep: dict) -> tuple[str, list]:
    """(endpoint class, entity kinds). Class is the riskiest entity: external_model > pt > databricks_ppt > other.
    An endpoint whose list entry has no served entities is 'other' (recorded, e.g. Knowledge Assistant)."""
    cfg = (ep or {}).get("config") or {}
    ents = cfg.get("served_entities") or cfg.get("served_models") or []
    kinds = [served_entity_kind(ep.get("name", ""), e) for e in ents] or ["other"]
    for k in ("external_model", "pt", "databricks_ppt"):
        if k in kinds:
            return k, kinds
    return "other", kinds


def _other_label(ep: dict) -> str:
    """Subtype of an 'other AI' endpoint, from the modern served_entities or the legacy served_models list.
    Knowledge Assistant endpoints use an agent task and can't be told apart from other agents."""
    cfg = (ep or {}).get("config") or {}
    ents = cfg.get("served_entities") or cfg.get("served_models") or []
    if "agent" in str(ep.get("task") or ""):
        return "Knowledge Assistant/agent"
    return "custom model" if ents else "no served entities listed"


def usage_tracking_enabled(ep: dict):
    """True / False from the endpoint's ai_gateway.usage_tracking_config.enabled; None if not configured or unknown."""
    cfg = ((ep or {}).get("ai_gateway") or {}).get("usage_tracking_config") or {}
    v = cfg.get("enabled")
    return v if isinstance(v, bool) else None


USAGE_CAVEAT = ("traffic shown only for endpoints with usage tracking on during the window; absence is not proof of "
                "no traffic")


def fetch_full_endpoints(listed, get_fn, retries: int = 3, sleep_s: float = 2.0, sleep=time.sleep):
    """Full config for EVERY listed endpoint (no cap): `get_fn(name)` -> dict, retried up to `retries` times.
    Returns (endpoints, failed_names). A failed GET keeps the list entry (still usable as positive evidence)."""
    full, failed = [], []
    for ep in listed:
        name = (ep or {}).get("name")
        for attempt in range(retries):
            try:
                full.append(get_fn(name)); break
            except Exception:
                if attempt == retries - 1:
                    full.append(ep); failed.append(name)
                else:
                    sleep(sleep_s)
    return full, failed


def ai_endpoint_inventory(endpoints, usage=None, lab_owned=(), lookback_days: int = 30, visibility=None,
                          tracking=None, incomplete=()) -> tuple[str, str, dict]:
    """B-09 verdict: does any existing legacy AI route exist right now? Decided from the endpoint inventory only.
    endpoints   serving endpoint dicts (full GET config where it could be read), or None if the list couldn't be read
    usage       rows {endpoint_name, entity_type, requests, last_request} from system.serving over lookback_days, or
                None. Supplementary evidence only, never part of the verdict (USAGE_CAVEAT).
    lab_owned   endpoint names the lab manifest recorded (labelled, never excluded)
    visibility  {"admin": True/False/None, "basis": "..."}: the list API is permission-filtered
    tracking    {endpoint name: True/False/None} current usage-tracking state per endpoint (evidence only)
    incomplete  endpoint names whose full config couldn't be read, or reasons the list was cut short
    FAIL     any external-model or provisioned-throughput endpoint (any caller)
    MANUAL   list unreadable, caller not proven workspace admin, or classification incomplete
    INFO     other AI endpoints exist (agents, custom models, Knowledge Assistant, listed databricks-* endpoints)
    PASS     workspace admin, complete classification, no AI serving endpoints at all."""
    vis = visibility or {}
    incomplete = list(incomplete or [])
    if endpoints is None:
        return ("MANUAL", "couldn't list serving endpoints; check Serving > endpoints (workspace admin)",
                {"visibility": vis, "usage_caveat": USAGE_CAVEAT})
    lab_owned = set(lab_owned or ())
    tracking = tracking or {}
    rows = []
    for ep in endpoints:
        cls, kinds = endpoint_kind(ep)
        st = ep.get("state") or {}
        rows.append({"name": ep.get("name"), "class": cls, "kinds": sorted(set(kinds)),
                     "other_type": _other_label(ep) if cls == "other" else None,
                     "creator": ep.get("creator"), "ready": st.get("ready"), "lab_owned": ep.get("name") in lab_owned,
                     "usage_tracking": tracking.get(ep.get("name"))})
    risky = [r for r in rows if r["class"] in ("external_model", "pt")]
    other = [r for r in rows if r["class"] in ("other", "databricks_ppt")]
    names = {r["name"] for r in rows}
    ppt_traffic = sorted([u for u in (usage or []) if str(u.get("endpoint_name", "")).startswith("databricks-")],
                         key=lambda u: -int(u.get("requests") or 0))
    ev = {"endpoints_listed": len(rows), "classification_complete": not incomplete,
          "incomplete": incomplete[:B09_CAP], "incomplete_count": len(incomplete),
          "visibility": {"admin": vis.get("admin"), "basis": vis.get("basis", "unknown")},
          "counts": {"external_model": sum(r["class"] == "external_model" for r in rows),
                     "pt": sum(r["class"] == "pt" for r in rows),
                     "other_ai": sum(r["class"] == "other" for r in rows),
                     "databricks_ppt_listed": sum(r["class"] == "databricks_ppt" for r in rows)},
          "external_or_pt": risky[:B09_CAP], "other_ai": other[:B09_CAP], "capped_at": B09_CAP,
          "usage": {"readable": usage is not None, "lookback_days": lookback_days, "caveat": USAGE_CAVEAT,
                    "databricks_ppt_traffic": {"endpoints": len(ppt_traffic),
                                               "requests": sum(int(u.get("requests") or 0) for u in ppt_traffic),
                                               "top": ppt_traffic[:B09_CAP]},
                    "listed_endpoint_traffic": [u for u in (usage or []) if u.get("endpoint_name") in names][:B09_CAP]}}
    if usage is None:
        unote = " Usage history: system.serving couldn't be read (needs grants from an account admin); review it separately."
    elif ppt_traffic:
        unote = (f" Usage history (supplementary): {ev['usage']['databricks_ppt_traffic']['requests']} request(s) to "
                 f"{len(ppt_traffic)} databricks-* pay-per-token endpoint(s) in the last {lookback_days} days "
                 f"({', '.join(u['endpoint_name'] for u in ppt_traffic[:3])}); {USAGE_CAVEAT}.")
    else:
        unote = f" Usage history (supplementary): none found in the last {lookback_days} days; {USAGE_CAVEAT}."
    if risky:
        lab = [r["name"] for r in risky if r["lab_owned"]]
        return ("FAIL", f"{len(risky)} external-model / provisioned-throughput endpoint(s) exist and stay queryable even "
                "with Enforce Unity Gateway: " + ", ".join(f"{r['name']} ({r['class']})" for r in risky[:5])
                + (f"; lab-owned: {lab}" if lab else "") + "." + unote, ev)
    if vis.get("admin") is not True:
        return ("MANUAL", "endpoint list may be incomplete for a non-admin caller (the list API only returns endpoints "
                f"the caller can see; basis: {vis.get('basis', 'unknown')}); rerun the audit as a workspace admin." + unote, ev)
    if incomplete:
        return ("MANUAL", f"endpoint classification incomplete: couldn't read the full config of {len(incomplete)} "
                f"endpoint(s) ({', '.join(str(x) for x in incomplete[:5])}); rerun or check them by hand." + unote, ev)
    if other:
        return ("INFO", f"no external-model or provisioned-throughput endpoints; {len(other)} other AI endpoint(s) that may "
                "break or need migrating once enforcement is on: "
                + ", ".join(f"{r['name']} ({r['other_type'] or 'databricks-* pay-per-token'}; usage tracking "
                            f"{ {True: 'on', False: 'off'}.get(r['usage_tracking'], 'not configured') })" for r in other[:5])
                + "." + unote, ev)
    return ("PASS", f"as a workspace admin, with every endpoint classified: no external-model, provisioned-throughput or "
            "other AI serving endpoints exist right now." + unote, ev)


def valid_lookback_days(value) -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"usage_lookback_days must be an integer from 1 to 365: {value!r}")
    if not 1 <= n <= 365:
        raise ValueError(f"usage_lookback_days must be an integer from 1 to 365: {value!r}")
    return n


def has_proof(detail) -> bool:
    """True only for manifest rows written with the proof marker (absent before, present after this lab's own
    create/grant) and not later marked unproven. Teardown acts only on these rows."""
    try:
        d = json.loads(detail) if isinstance(detail, str) else (detail or {})
    except (TypeError, ValueError):
        return False
    return isinstance(d, dict) and d.get("confirmed_absent_before") is True and d.get("unproven") is not True


def genie_space_owned(detail, title: str | None, description: str | None) -> bool:
    """A Genie space may be updated or trashed by the lab only with INDEPENDENT creation provenance: its manifest row has
    the creation proof (this lab created it, absent before) and the live title/description are a lab pair or the exact
    title recorded at creation. A space recorded without proof (e.g. an ID adopted from lab_setup_status by an earlier
    version, whose title/description pair anyone can copy) is never adopted or trashed: manual review."""
    if not has_proof(detail):
        return False
    try:
        d = json.loads(detail) if isinstance(detail, str) else (detail or {})
    except (TypeError, ValueError):
        return False
    return is_lab_genie_space(title, description, d.get("created_title"))


MANIFEST_DDL = """CREATE TABLE IF NOT EXISTS {table} (
    kind STRING, object_id STRING, detail STRING, created_by STRING, run_id STRING, created_at TIMESTAMP)"""


class Manifest:
    """kind: genie_space, vs_index, experiment, connection, function, policy, table, governed_tag,
    acl_entry (object_type/object_id/principal/level ADDED by the lab), catalog_grant,
    connection_grant (connection|principal|privilege ADDED by the lab), tag_assignment."""

    def __init__(self, spark, catalog: str, schema: str, step: str, run_id: str):
        self.spark, self.step, self.run_id = spark, step, run_id
        self.table = f"{q(catalog)}.{q(schema)}.lab_manifest"
        spark.sql(MANIFEST_DDL.format(table=self.table))

    def items(self, kind: str | None = None) -> list[dict]:
        rows = [r.asDict() for r in self.spark.table(self.table).collect()]
        return [r for r in rows if kind is None or r["kind"] == kind]

    def has(self, kind: str, object_id: str) -> bool:
        return self.spark.sql(f"SELECT 1 FROM {self.table} WHERE kind = :k AND object_id = :o LIMIT 1",
                              args={"k": kind, "o": str(object_id)}).count() > 0

    def add(self, kind: str, object_id: str, detail: dict | None = None):
        if self.has(kind, object_id):
            return
        self.spark.sql(f"""INSERT INTO {self.table} VALUES (:k, :o, :d, :s, :r, current_timestamp())""",
                       args={"k": kind, "o": str(object_id), "d": json.dumps(detail or {}, default=str),
                             "s": self.step, "r": self.run_id})
        print("manifest +", kind, object_id)

    def remove(self, kind: str, object_id: str):
        self.spark.sql(f"DELETE FROM {self.table} WHERE kind = :k AND object_id = :o", args={"k": kind, "o": str(object_id)})

    def proven(self, kind: str, object_id: str) -> bool:
        return any(r["object_id"] == str(object_id) and has_proof(r["detail"]) for r in self.items(kind))

    def record_created(self, kind: str, object_id: str, detail: dict | None = None):
        """Record an object this run just created (404 before, present after); replaces any stale/unproven row."""
        self.remove(kind, object_id)
        self.add(kind, object_id, {**(detail or {}), "confirmed_absent_before": True})

    def mark_row_unproven(self, kind: str, object_id: str, detail: str | None, reason: str):
        try:
            d = json.loads(detail or "{}")
        except (TypeError, ValueError):
            d = {"raw_detail": detail}
        d = d if isinstance(d, dict) else {"raw_detail": detail}
        d.update({"unproven": True, "reason": reason})
        self.spark.sql(f"UPDATE {self.table} SET detail = :d WHERE kind = :k AND object_id = :o",
                       args={"d": json.dumps(d, default=str), "k": kind, "o": str(object_id)})

    def mark_unproven(self, kind: str, reason: str) -> list[str]:
        """Keep rows without creation proof, but mark them so teardown lists them for manual review only."""
        marked = []
        for r in self.items(kind):
            if has_proof(r["detail"]):
                continue
            try:
                d = json.loads(r["detail"] or "{}")
            except (TypeError, ValueError):
                d = {"raw_detail": r["detail"]}
            if d.get("unproven") is True:
                continue
            d.update({"unproven": True, "reason": reason})
            self.spark.sql(f"UPDATE {self.table} SET detail = :d WHERE kind = :k AND object_id = :o",
                           args={"d": json.dumps(d, default=str), "k": kind, "o": r["object_id"]})
            marked.append(r["object_id"])
        return marked


def paginate(api_do, path: str, key: str, query: dict | None = None, limit: int = 50) -> list:
    """GET a Unity Catalog list API page by page (next_page_token)."""
    out, token, qy = [], None, dict(query or {})
    for _ in range(limit):
        if token:
            qy["page_token"] = token
        r = api_do("GET", path, query=qy) or {}
        out += r.get(key, []) or []
        token = r.get("next_page_token")
        if not token:
            break
    return out
