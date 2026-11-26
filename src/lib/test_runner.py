"""Runner-level tests with fakes (no Databricks needed). Run through: python3 src/lib/test_checks.py

One section per review item. Every fake raises a class named like the SDK's errors (NotFound, PermissionDenied), because
the runner treats only STRUCTURED errors as evidence.
"""
import json
import os
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runner as R  # noqa: E402
from checks import identity_marker  # noqa: E402

ACCOUNT = "0" * 8 + "-0000-0000-0000-" + "0" * 12
ROOT = "/Workspace/Users/someone/.bundle/b/workspace"


class NotFound(Exception):
    error_code = "NOT_FOUND"


class PermissionDenied(Exception):
    error_code = "PERMISSION_DENIED"


class FakeTables:
    """In-memory stand-in for runner.Tables (same methods)."""

    def __init__(self):
        self.t, self.ops, self.n = {}, [], 0

    def exists(self, full):
        return full in self.t

    def create(self, full, columns, comment, props):
        if full in self.t:
            raise RuntimeError("TABLE_ALREADY_EXISTS")
        self.n += 1
        self.t[full] = {"rows": [], "props": dict(props), "id": f"tid-{self.n}", "owner": "someone"}
        self.ops.append(("create", full))

    def props(self, full):
        return dict(self.t[full]["props"])

    def rows(self, full, where=None):
        return [dict(r) for r in self.t[full]["rows"] if all(r.get(k) == v for k, v in (where or {}).items())]

    def insert(self, full, row):
        self.t.setdefault(full, {"rows": [], "props": {}, "id": "ext", "owner": "someone"})["rows"].append(dict(row))
        self.ops.append(("insert", full))

    def update(self, full, where, values):
        for r in self.t[full]["rows"]:
            if all(r.get(k) == v for k, v in where.items()):
                r.update(values)
        self.ops.append(("update", full))

    def drop(self, full):
        self.t.pop(full)
        self.ops.append(("drop", full))


class FakeFS:
    def __init__(self):
        self.files = {}

    def read(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def write(self, path, text):
        self.files[path] = text

    def list(self, folder):
        folder = folder.rstrip("/") + "/"
        return [p for p in self.files if p.startswith(folder) and "/" not in p[len(folder):]]


def _uc_get(tables, full):
    """Unity Catalog tables.get fake: a structured NotFound for a missing table."""
    if full not in tables.t:
        raise NotFound(f"TABLE_DOES_NOT_EXIST: {full}")
    return NS(table_id=tables.t[full]["id"], owner=tables.t[full]["owner"])


def make(action="teardown-apply", mode="demo", api=None, tables=None, fs=None, jobs_offset=0, **kw):
    cfg = R.parse_config({"action": action, "mode": mode, "catalog": "cat", "schema": "lab_s",
                          "resource_prefix": "labtest", "persona_prefix": "labtest", "warehouse_id": "w1",
                          "account_id": ACCOUNT, **kw})
    tables = tables or FakeTables()
    calls = []

    def do(method, path, query=None, body=None):
        calls.append((method, path, query, body))
        if api:
            return api(method, path, query, body)
        return {}
    w = NS(config=NS(host="https://x"), api_client=NS(do=do), get_workspace_id=lambda: 42,
           current_user=NS(me=lambda: NS(user_name="someone", groups=[NS(display="admins")])),
           tables=NS(get=lambda full: _uc_get(tables, full)),
           workspace=NS(get_status=lambda path: NS(object_type="NOTEBOOK")),
           jobs=NS(get=lambda jid: NS(settings=NS(tasks=[NS(task_key="t", notebook_task=NS(
               notebook_path=f"{ROOT}/files/src/notebooks/nb_{jid}", base_parameters={}))]))))
    fs = fs or FakeFS()
    r = R.Runner(w, spark=None, cfg=cfg, log=lambda *a: None, read_text=fs.read, write_text=fs.write,
                 sleep=lambda s: None, tables=tables, list_dir=fs.list)
    r._dep = R.Deployment(root=ROOT, state_dir=ROOT + "/state", target="workspace",
                          jobs={k: str(i + jobs_offset) for k, i in (
                              ("lab_setup", 1), ("lab_audit", 2), ("lab_teardown", 3), ("lab_collect", 4),
                              ("lab_persona_clinician", 5), ("lab_persona_pii_reader", 6),
                              ("lab_persona_outsider", 7))},
                          schemas={"lab": "cat.lab_s", "lab_tools": "cat.lab_s_tools"},
                          model_services={"lab_chat": "cat.lab_s.labtest_chat"}, source_root=ROOT + "/files",
                          audit_mode="demo")
    return r, w, tables, fs, calls


def raises(fn, exc=R.Stop):
    try:
        fn()
    except exc as e:
        return str(e)
    raise AssertionError(f"expected {exc.__name__}")


def receipt(kind, role, name, scim_id, app="", proof=None):
    return {"kind": kind, "role": role, "display_name": name, "scim_id": str(scim_id), "application_id": app,
            "binding": "b", "state": "active", "note": "",
            "proof": json.dumps(proof or {"confirmed_absent_before": True, "marker": identity_marker(name)})}


# ---------------------------------------------------------------------------------------------- 1. no CASCADE
def test_r1_schema_dropped_only_when_empty_and_never_cascade():
    r, w, *_ = make()
    sqls = []
    r.spark = NS(sql=lambda s, args=None: sqls.append(s))
    w.schemas = NS(get=lambda full: (_ for _ in ()).throw(NotFound("gone")))
    r.schema_inventory = lambda full: {"table": ["someone_elses_table"], "function": ["f"]}
    msg = raises(lambda: r._destroy("schema", "cat.lab_s", {}, None))
    assert "not empty" in msg and "table someone_elses_table" in msg and not sqls
    r.schema_inventory = lambda full: {}
    r._destroy("schema", "cat.lab_s", {}, None)
    assert sqls == ["DROP SCHEMA `cat`.`lab_s`"] and not any("CASCADE" in s for s in sqls)
    assert "not this lab" in raises(lambda: r._destroy("schema", "cat.other", {}, None))


def test_r1_runner_tables_dropped_only_with_marker():
    r, w, tables, *_ = make()
    tables.t["cat.lab_s.lab_runner_steps"] = {"rows": [], "props": {}, "id": "x", "owner": "someone"}
    assert "no runner marker" in raises(lambda: r._destroy("runner_tables", "cat.lab_s", {}, None))
    tables.t["cat.lab_s.lab_runner_steps"]["props"] = {R.KIND_PROP: "steps"}
    tables.t["cat.lab_s.labtest_chat_payload"] = {"rows": [], "props": {}, "id": "y", "owner": "someone"}
    r._destroy("runner_tables", "cat.lab_s", {"inference_tables": ["cat.lab_s.labtest_chat_payload"]}, None)
    assert not tables.t


class InvalidParameterValue(Exception):
    error_code = "INVALID_PARAMETER_VALUE"


def test_r1_delete_error_needs_structured_proof_of_absence():
    r, w, *_ = make()
    gone = {"state": False}

    def get(job_id):
        if gone["state"]:
            raise type("ResourceDoesNotExist", (Exception,), {})("no job")
        return NS(job_id=job_id)
    w.jobs = NS(delete=lambda job_id: (_ for _ in ()).throw(InvalidParameterValue(f"Job {job_id} does not exist.")),
                get=get)
    msg = raises(lambda: r._destroy("job", "5", {}, None))          # delete failed AND the job is still there
    assert "couldn't delete job 5" in msg
    gone["state"] = True                                             # deleted earlier: GET proves it (structured)
    r._destroy("job", "5", {}, None)


# ---------------------------------------------------------------------------------------------- 2. identity proof
def _store(r):
    return r.store(create_generation=True)


def test_r2_binding_includes_account_workspace_target_root():
    r, *_ = make()
    b = r.binding()
    assert b.split("|")[:4] == [ACCOUNT, "42", "workspace", ROOT] and "cat.lab_s" in b
    assert "unknown" in raises(lambda: R.make_binding("", "42", "t", ROOT, r.cfg))


def test_r2_store_must_be_created_by_this_runner():
    r, w, tables, fs, _ = make()
    tables.t["cat.lab_s.lab_identity_receipts"] = {"rows": [], "props": {}, "id": "x", "owner": "someone"}
    assert "no creation record" in raises(lambda: _store(r).open(create=True))     # pre-existing, no record
    tables.t.clear()
    assert _store(r).open(create=True) is True                                     # created now, record sealed
    rec = json.loads(fs.files[ROOT + "/runner/identity_store.json"])
    assert set(rec["tables"]) == {"receipts", "bindings"} and rec["sha256"]
    assert _store(r).open(create=False) is True                                    # verified again
    tables.t["cat.lab_s.lab_persona_bindings"]["id"] = "replaced"
    assert "not the table this runner created" in raises(lambda: _store(r).open(create=False))
    tables.t["cat.lab_s.lab_persona_bindings"]["id"] = rec["tables"]["bindings"]
    fs.files[ROOT + "/runner/identity_store.json"] = fs.files[ROOT + "/runner/identity_store.json"].replace(
        rec["store_id"], "f" * 32)
    assert "checksum" in raises(lambda: _store(r).open(create=False))              # tampered record


def test_r2_store_rejects_inherited_write_privileges():
    def api(method, path, query, body):
        if "effective-permissions" in path:
            return {"privilege_assignments": [{"principal": "account users", "privileges": [
                {"privilege": "MODIFY", "inherited_from_type": "CATALOG", "inherited_from_name": "cat"}]}]}
        return {}
    r, *_ = make(api=api)
    msg = raises(lambda: _store(r).open(create=True))
    assert "writable by others" in msg and "account users: MODIFY (inherited from catalog cat)" in msg
    r2, w2, tables2, *_ = make()
    _store(r2).open(create=True)
    tables2.t["cat.lab_s.lab_identity_receipts"]["owner"] = "another_admin"
    assert "owned by another_admin" in raises(lambda: _store(r2).open(create=False))


def test_r2_teardown_deletes_only_exact_matches():
    sp = receipt("service_principal", "outsider", "labtest-outsider", 9, app="a1")
    live = {"id": "9", "displayName": "labtest-outsider", "applicationId": "a1"}
    assert R.identity_teardown_action(sp, live)[0] == "delete"
    assert R.identity_teardown_action(sp, {k: v for k, v in live.items() if k != "applicationId"})[0] == "unresolved"
    assert R.identity_teardown_action(sp, {**live, "displayName": "x"})[0] == "unresolved"
    assert R.identity_teardown_action(sp, {**live, "id": "10"})[0] == "unresolved"
    assert R.identity_teardown_action({**sp, "proof": "{}"}, live)[0] == "unresolved"
    assert R.identity_teardown_action(sp, None)[0] == "gone"


def test_r2_only_structured_not_found_counts_as_gone():
    def api(method, path, query, body):
        if path.endswith("/1"):
            raise NotFound("SCIM_404")
        if path.endswith("/2"):
            raise RuntimeError("HTTP 500: the service principal was not found in the cache (404?)")   # text only
        return {"id": "3"}
    r, *_ = make(api=api)
    assert r.account_get("ServicePrincipals", "1") is None
    assert "error" in r.account_get("ServicePrincipals", "2")
    assert R.is_not_found(NotFound()) and not R.is_not_found(RuntimeError("404 not found does not exist"))


# ---------------------------------------------------------------------------------------------- 3. manual fallback
def test_r3_fallback_keeps_receipts_and_needs_no_account_read():
    def api(method, path, query, body):
        if path.startswith(R.ACCOUNT_SCIM):
            raise PermissionDenied("account SCIM read denied")
        return {}
    apps = ("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222",
            "33333333-3333-3333-3333-333333333333")
    r, w, tables, *_ = make(action="personas", api=api, manual_app_ids=",".join(apps),
                            manual_groups="g_cl,g_pii,g_all,g_out")
    st = _store(r)
    st.open(create=True)
    st.add_receipt("service_principal", "clinician", "labtest-clinician", 5, "old-app",
                   {"confirmed_absent_before": True, "marker": identity_marker("labtest-clinician")})
    before = json.dumps(st.receipts(), sort_keys=True)
    sps = {a: NS(id=str(i), application_id=a, display_name=f"sp{i}") for i, a in enumerate(apps)}
    w.service_principals = NS(list=lambda filter: [sps[filter.split('"')[1]]])
    w.groups = NS(list=lambda filter: [NS(id=filter.split('"')[1], display_name=filter.split('"')[1],
                                          meta=NS(resource_type="Group"))])
    out = r._manual_personas(st)
    assert out["groups"]["outsiders"] == "g_out"
    assert json.dumps(st.receipts(), sort_keys=True) == before                     # receipt untouched
    b = {(x["kind"], x["role"]): x for x in st.bindings()}
    assert b[("service_principal", "clinician")]["origin"] == "provided" and len(b) == 7


def test_r3_fallback_values_must_be_distinct():
    a = "11111111-1111-1111-1111-111111111111"
    assert "different service principals" in raises(
        lambda: make(action="personas", manual_app_ids=f"{a},{a},{a.replace('1', '3')}",
                     manual_groups="a,b,c,d"), R.ConfigError)
    assert "different groups" in raises(
        lambda: make(action="personas", manual_app_ids="11111111-1111-1111-1111-111111111111,"
                     "22222222-2222-2222-2222-222222222222,33333333-3333-3333-3333-333333333333",
                     manual_groups="a,b,A,d"), R.ConfigError)


def test_r3_workspace_local_group_is_not_accepted():
    r, w, *_ = make(action="personas")
    w.groups = NS(list=lambda filter: [NS(id="1", display_name="g", meta=NS(resource_type="WorkspaceGroup"))])
    g, why = r._workspace_group("g")
    assert g is None and "workspace-local" in why


# ---------------------------------------------------------------------------------------------- 4. ACL cleanup
def _acl(entries):
    return NS(access_control_list=[NS(user_name=u, group_name=g, service_principal_name=s, all_permissions=[
        NS(permission_level=NS(value=lv), inherited=inh)]) for u, g, s, lv, inh in entries])


def test_r4_acl_removed_only_if_still_exactly_the_recorded_entry():
    rec = receipt("acl", "directories|77|outsider", "app-o", "app-o", app="app-o",
                  proof={"confirmed_absent_before": True, "before": [], "after": ["CAN_READ"]})
    r, w, *_ = make()
    state = {"acl": _acl([("owner", None, None, "CAN_MANAGE", False), (None, None, "app-o", "CAN_READ", False),
                          (None, "admins", None, "CAN_MANAGE", True)])}
    sets = []
    w.permissions = NS(get=lambda t, i: state["acl"],
                       set=lambda t, i, access_control_list: (sets.append(access_control_list),
                                                              state.update(acl=_acl([("owner", None, None, "CAN_MANAGE",
                                                                                      False)]))))
    assert r._receipt_decision(rec)[0] == "remove"
    ok, why = r._remove_acl_entry(rec)
    assert ok and len(sets) == 1 and [x.user_name for x in sets[0]] == ["owner"]   # only the other DIRECT entry kept
    # live entry no longer exactly CAN_READ -> unresolved, nothing written
    state["acl"] = _acl([(None, None, "app-o", "CAN_MANAGE", False)])
    sets.clear()
    assert r._receipt_decision(rec)[0] == "unresolved" and not sets
    assert R.acl_teardown_action(rec, None)[0] == "gone" and R.acl_teardown_action(rec, [])[0] == "gone"
    assert R.acl_teardown_action({**rec, "proof": "{}"}, ["CAN_READ"])[0] == "unresolved"


# ---------------------------------------------------------------------------------------------- 5. run / resume
def _run_fixture(results):
    r, w, tables, *_ = make(action="run")
    ran = []

    def run_job(key, params):
        ran.append(key)
        state, error = results.get(key, ("SUCCESS", ""))
        return {"run_id": len(ran), "state": state, "url": "u", "error": error}
    r.run_job = run_job
    r.personas = lambda st=None: {"apps": {p: f"app-{p}" for p in R.PERSONAS},
                                  "groups": {g: f"labtest_{g}" for g in R.GROUP_ROLES}}
    r.store = lambda dep=None: None
    r.grant_source_read = lambda apps, st: []
    r.ensure_run_as = lambda jid, app, p: "bound"
    r.deployed_param_default = lambda key, param: ""
    R.persona_jobs, orig = (lambda w, dep: {p: i for i, p in enumerate(R.PERSONAS)}), R.persona_jobs
    return r, tables, ran, orig


def test_r5_persona_failure_stops_before_collect_and_resume_reruns_dependents():
    r, tables, ran, orig = _run_fixture({"lab_persona_pii_reader": ("FAILED", "boom")})
    try:
        msg = raises(lambda: r.run())
        assert "lab_persona_pii_reader ended FAILED" in msg and "lab_collect" not in ran and "lab_audit" not in ran
        suite = msg.split("run_id=")[1].split(" ")[0]
        r.cfg = R.parse_config({**{k: getattr(r.cfg, k) for k in ("mode", "catalog", "schema", "resource_prefix",
                                                                    "persona_prefix", "warehouse_id", "account_id")},
                                "action": "resume", "run_id": suite})
        ran.clear()
        r.run_job = lambda key, params, _ran=ran: (_ran.append(key), {"run_id": 9, "state": "SUCCESS", "url": "u",
                                                                       "error": ""})[1]
        r.run(resume_id=suite)
        assert ran == ["lab_persona_pii_reader", "lab_persona_outsider", "lab_collect", "lab_audit"]
    finally:
        R.persona_jobs = orig


def test_r5_wrong_principal_fails_and_runner_records_skips():
    r, tables, ran, orig = _run_fixture({"lab_persona_clinician": (
        "FAILED", "RuntimeError: LAB_IDENTITY_GUARD: running as someone else, not the expected clinician principal")})
    try:
        msg = raises(lambda: r.run())
        assert "refused to run as the wrong identity" in msg and ran == ["lab_setup", "lab_persona_clinician"]
        rows = tables.rows("cat.lab_s.test_results")
        assert rows and {x["status"] for x in rows} == {"SKIP"} and {x["persona"] for x in rows} == {"clinician"}
        steps = tables.rows("cat.lab_s.lab_runner_steps")
        assert [s["state"] for s in steps if s["step"] == "persona_clinician"] == ["FAILED"]   # retryable
    finally:
        R.persona_jobs = orig


# ---------------------------------------------------------------------------------------------- 6. teardown resume
def test_r6_teardown_resumes_from_checkpoint_after_tables_are_gone():
    deleted = []

    def api(method, path, query, body):
        if method == "DELETE":
            deleted.append(path)
            return {}
        if path.startswith(R.ACCOUNT_SCIM):
            if any(path.endswith(d.rsplit("/", 1)[1]) for d in deleted):
                raise NotFound("SCIM_404")
            return {"id": "9", "displayName": "labtest-outsider", "applicationId": "a1"}
        return {}
    r, w, tables, fs, _ = make(api=api)
    exp = "/Workspace/Users/someone/genai_governance_lab_exports/results.csv"
    fs.files[exp] = "a,b\r\n1,2\r\n"
    cp = {"version": 2, "binding": r.binding(), "generation": r.generation(r._dep, create=True),
          "deployment": r._dep.as_dict(),
          "phases": {"export": "done", "lab_teardown": "done"}, "bundle_resources": [("job", "2")],
          "export": {"path": exp, "sha256": R.sha256(fs.files[exp])},
          "receipts": [receipt("service_principal", "outsider", "labtest-outsider", 9, app="a1")]}
    fs.files[ROOT + "/runner/teardown_checkpoint.json"] = R.seal(cp)
    r.export = lambda dep: (_ for _ in ()).throw(AssertionError("results tables are gone: must not re-export"))
    r.run_job = lambda key, params: (_ for _ in ()).throw(AssertionError("lab_teardown already done"))
    destroyed = []
    r._destroy = lambda kind, rid, cp, st: destroyed.append((kind, rid))
    out = r.teardown(apply=True)
    assert deleted == [f"{R.ACCOUNT_SCIM}/ServicePrincipals/9"] and destroyed == [("job", "2")]
    assert out["phases"]["identities"] == "done"
    saved = R.unseal(fs.files[ROOT + "/runner/teardown_checkpoint.json"], "cp")
    assert saved["receipts"][0]["state"] == "deleted"
    # a tampered checkpoint is never used
    fs.files[ROOT + "/runner/teardown_checkpoint.json"] = fs.files[ROOT + "/runner/teardown_checkpoint.json"].replace(
        '"done"', '"DONE"', 1)
    assert "checksum" in raises(lambda: r.teardown(apply=True))


def test_r6_unresolved_identity_stops_before_bundle_resources():
    def api(method, path, query, body):
        if path.startswith(R.ACCOUNT_SCIM):
            return {"id": "9", "displayName": "renamed-by-someone", "applicationId": "a1"}
        return {}
    r, w, tables, fs, _ = make(api=api)
    exp = "/Workspace/Users/someone/genai_governance_lab_exports/r.csv"
    fs.files[exp] = "x"
    cp = {"version": 2, "binding": r.binding(), "generation": r.generation(r._dep, create=True),
          "deployment": r._dep.as_dict(),
          "phases": {"export": "done", "lab_teardown": "done"}, "bundle_resources": [("schema", "cat.lab_s")],
          "export": {"path": exp, "sha256": R.sha256("x")},
          "receipts": [receipt("service_principal", "outsider", "labtest-outsider", 9, app="a1")]}
    fs.files[ROOT + "/runner/teardown_checkpoint.json"] = R.seal(cp)
    r._destroy = lambda *a: (_ for _ in ()).throw(AssertionError("must not destroy with unresolved receipts"))
    assert "unresolved" in raises(lambda: r.teardown(apply=True))


# ---------------------------------------------------------------------------------------------- 7. Genie legacy
def test_r7_legacy_genie_ids_without_provenance_are_never_owned():
    from checks import genie_space_owned, LAB_GENIE_PAIRS, LAB_GENIE_TITLE, LAB_GENIE_DESCRIPTION
    old_title, old_desc = LAB_GENIE_PAIRS[0]
    assert not genie_space_owned('{"adopted_from": "lab_setup_status"}', old_title, old_desc)
    assert not genie_space_owned("{}", LAB_GENIE_TITLE, LAB_GENIE_DESCRIPTION)
    proof = '{"confirmed_absent_before": true, "created_title": "%s 2026"}' % LAB_GENIE_TITLE
    assert genie_space_owned(proof, LAB_GENIE_TITLE + " 2026", LAB_GENIE_DESCRIPTION)
    assert genie_space_owned('{"confirmed_absent_before": true}', LAB_GENIE_TITLE, LAB_GENIE_DESCRIPTION)
    assert not genie_space_owned('{"confirmed_absent_before": true, "unproven": true}', LAB_GENIE_TITLE,
                                 LAB_GENIE_DESCRIPTION)


# ---------------------------------------------------------------------------------------------- 8. mode binding
def test_r8_mode_must_match_the_deployment():
    dep = R.Deployment(root=ROOT, state_dir="s", target="t", jobs={"lab_setup": "1", "lab_audit": "2"},
                       audit_mode="demo")
    assert "demo-mode deployment" in raises(lambda: R.check_mode("run", "audit", dep))
    R.check_mode("run", "demo", dep)
    R.check_mode("teardown-apply", "audit", dep)                    # teardown follows the deployment's mode
    audit_only = R.Deployment(root=ROOT, state_dir="s", target="t", jobs={"lab_audit": "2"}, audit_mode="audit")
    assert "audit-mode deployment" in raises(lambda: R.check_mode("results", "demo", audit_only))
    broken = R.Deployment(root=ROOT, state_dir="s", target="t", jobs={"lab_audit": "2"}, audit_mode="demo")
    assert "inconsistent" in raises(lambda: R.check_mode("teardown-plan", "audit", broken))


def test_r8_results_checks_mode_before_reading_anything():
    r, w, tables, *_ = make(action="results", mode="audit")
    r.result_rows = lambda: (_ for _ in ()).throw(AssertionError("read results before the mode check"))
    assert "demo-mode deployment" in raises(lambda: r.results())


# ---------------------------------------------------------------------------------------------- 9. allowlists
def test_r9_blank_widget_keeps_a_deployed_allowlist():
    assert R.list_param((), "approved_admins_group", "labtest_clinicians") is None       # keep the deployed allowlist
    assert R.list_param((), "", "labtest_clinicians") == "labtest_clinicians"            # nothing configured: infer
    assert R.list_param(("a", "b"), "x", "y") == "a,b"                                  # explicit widget wins


# ---------------------------------------------------------------------------------------------- final review 1:
# teardown checkpoints are bound to a deployment generation
HOME = "/Workspace/Users/someone/genai_governance_lab_exports"


def _scim(live):
    """Account SCIM fake: live = {scim_id: record}; DELETE removes, GET of a missing ID is a structured 404."""
    def api(method, path, query, body):
        if path.startswith(R.ACCOUNT_SCIM):
            sid = path.rsplit("/", 1)[1]
            if method == "DELETE":
                live.pop(sid, None)
                return {}
            if sid not in live:
                raise NotFound("SCIM_404")
            return live[sid]
        return {}
    return api


def _generation_lifecycle(r, fs, live, scim_id, ran, destroyed):
    """personas (store + one receipt) -> teardown-apply, with the jobs, export and resource deletion faked."""
    st = r.store(create_generation=True)
    st.open(create=True)
    nm = "labtest-outsider"
    live[str(scim_id)] = {"id": str(scim_id), "displayName": nm, "applicationId": f"app-{scim_id}"}
    st.add_receipt("service_principal", "outsider", nm, scim_id, f"app-{scim_id}",
                   {"confirmed_absent_before": True, "marker": identity_marker(nm)})
    exp = f"{HOME}/export-{scim_id}.csv"
    fs.files[exp] = "a\r\n1\r\n"
    r.export = lambda dep: {"path": exp, "sha256": R.sha256(fs.files[exp])}
    r.run_job = lambda key, params: ran.append(key) or {"state": "SUCCESS", "run_id": 1, "url": "u"}
    r._notebook_result = lambda res: {"state": res["state"]}

    def destroy(kind, rid, cp, st_):
        destroyed.append((kind, rid))
        if kind == "runner_tables" and st_:
            st_.drop()
        if kind == "bundle_folder":                       # the deployment folder (and its generation record) goes
            for p in [p for p in fs.files if p.startswith(ROOT + "/")]:
                del fs.files[p]
    r._destroy = destroy
    return r.teardown(apply=True)


def test_f1_redeploy_is_a_new_generation_and_its_teardown_is_not_skipped():
    fs, live, tables = FakeFS(), {}, FakeTables()
    ran1, destroyed1 = [], []
    r1, *_ = make(api=_scim(live), fs=fs, tables=tables)
    _generation_lifecycle(r1, fs, live, 9, ran1, destroyed1)
    gen1 = [p for p in fs.files if "teardown-checkpoint-" in p]
    assert len(gen1) == 1 and R.unseal(fs.files[gen1[0]], "cp")["complete"] is True and "9" not in live
    # redeploy (same root, new jobs) -> personas -> teardown-apply: the second generation is removed completely
    ran2, destroyed2 = [], []
    r2, *_ = make(api=_scim(live), fs=fs, tables=tables, jobs_offset=10)
    out = _generation_lifecycle(r2, fs, live, 19, ran2, destroyed2)
    assert ran2 == ["lab_teardown"] and "19" not in live and out["phases"]["identities"] == "done"
    assert destroyed2 == [k for k in r2.bundle_resources(r2._dep)] and destroyed2[-1] == ("bundle_folder", ROOT)
    cps = sorted(p for p in fs.files if "teardown-checkpoint-" in p)
    gens = [R.unseal(fs.files[p], "cp")["generation"] for p in cps]
    assert len(cps) == 2 and len(set(gens)) == 2 and all(g in p for g, p in zip(gens, cps))   # named by generation
    assert all(R.unseal(fs.files[p], "cp")["complete"] for p in cps)


def test_f1_checkpoint_of_another_generation_is_never_reused():
    r, w, tables, fs, _ = make()
    gen = r.generation(r._dep, create=True)
    stale = {"version": 2, "binding": r.binding(), "generation": "gen-20260101T000000Z-00000000",
             "deployment": r._dep.as_dict(), "phases": {"export": "done", "lab_teardown": "done",
                                                        "identities": "done"}, "bundle_resources": [], "receipts": []}
    fs.files[ROOT + "/runner/teardown_checkpoint.json"] = R.seal(stale)
    assert "generation" in raises(lambda: r.teardown(apply=True))
    assert "generation" in raises(lambda: r.teardown(apply=False))
    no_gen = {k: v for k, v in stale.items() if k != "generation"}             # written before generations
    fs.files[ROOT + "/runner/teardown_checkpoint.json"] = R.seal(no_gen)
    assert "before generations" in raises(lambda: r.teardown(apply=True))
    done = {**stale, "generation": gen, "complete": True}                      # complete, but this generation is live
    fs.files[ROOT + "/runner/teardown_checkpoint.json"] = R.seal(done)
    assert "marked complete" in raises(lambda: r.teardown(apply=True))
    assert R.checkpoint_reusable({**done, "complete": False}, r.binding(), gen) is None


def test_f1_generation_record_must_match_the_live_deployment():
    r, w, tables, fs, _ = make()
    r.generation(r._dep, create=True)
    r2, *_ = make(fs=fs, jobs_offset=10)            # same folder, but a different audit job: not this deployment
    assert "audit job" in raises(lambda: r2.generation(r2._dep))
    r3, *_ = make()
    r3.store(create_generation=True).open(create=True)
    rec = ROOT + "/runner/identity_store.json"
    doc = R.unseal(r3.read_text(rec), "x")
    r3.write_text(rec, R.seal({**doc, "generation": "gen-20260101T000000Z-00000000"}))
    assert "generation" in raises(lambda: r3.store().open(create=False))
    st = r3.store()
    r3.write_text(rec, R.seal(doc))
    st.open(create=False)
    r3.tables.t["cat.lab_s.lab_identity_receipts"]["rows"].append({"binding": st.binding, "generation": "old"})
    assert "another deployment generation" in raises(lambda: st.receipts())


def _not_discoverable(r):
    def gone():
        raise R.NoDeployment("No bundle deployment of the lab found")
    r.deployment = gone


def test_f1_home_checkpoint_resume_only_for_the_unfinished_generation_without_newer_evidence():
    fs, live, tables = FakeFS(), {}, FakeTables()
    r, *_ = make(api=_scim(live), fs=fs, tables=tables)
    gen = r.generation(r._dep, create=True)
    exp = f"{HOME}/e.csv"
    fs.files[exp] = "x"
    cp = {"version": 2, "binding": r.binding(), "generation": gen, "deployment": r._dep.as_dict(),
          "phases": {"export": "done", "lab_teardown": "done", "identities": "done",
                     "destroyed": ["model_service:cat.lab_s.labtest_chat"]},
          "export": {"path": exp, "sha256": R.sha256("x")}, "receipts": [],
          "bundle_resources": [("job", "2"), ("bundle_folder", ROOT)]}
    home = f"{HOME}/{R.checkpoint_file(r._slug(), gen)}"
    fs.files[home] = R.seal(cp)
    # 1. a newer generation's record in the deployment folder: not resumed
    fs.files[ROOT + "/runner/generation.json"] = R.seal({"generation": "gen-20990101T000000Z-11111111"})
    r2, *_ = make(api=_scim(live), fs=fs, tables=tables)
    _not_discoverable(r2)
    assert "is not resumed" in raises(lambda: r2.teardown(apply=True))
    del fs.files[ROOT + "/runner/generation.json"]
    # 2. receipts of a newer generation in the identity store: not resumed
    tables.t["cat.lab_s.lab_identity_receipts"] = {"rows": [{"binding": cp["binding"], "generation": "gen-new"}],
                                                   "props": {}, "id": "t", "owner": "someone"}
    r3, *_ = make(api=_scim(live), fs=fs, tables=tables)
    _not_discoverable(r3)
    assert "receipts of generation" in raises(lambda: r3.teardown(apply=True))
    del tables.t["cat.lab_s.lab_identity_receipts"]
    # 3. no newer evidence: resumed, finishes, marked complete
    r4, *_ = make(api=_scim(live), fs=fs, tables=tables)
    _not_discoverable(r4)
    destroyed = []
    r4._destroy = lambda kind, rid, cp_, st: destroyed.append((kind, rid))
    r4.teardown(apply=True)
    assert destroyed == [("job", "2"), ("bundle_folder", ROOT)] and R.unseal(fs.files[home], "cp")["complete"]
    # 4. only complete checkpoints left (and a pre-generation one): nothing is resumed, the original stop stands
    fs.files[f"{HOME}/teardown-checkpoint-{r._slug()}.json"] = R.seal({**cp, "complete": False})
    r5, *_ = make(api=_scim(live), fs=fs, tables=tables)
    _not_discoverable(r5)
    assert "No bundle deployment" in raises(lambda: r5.teardown(apply=True), R.NoDeployment)
    # 5. a checkpoint whose file name and content disagree on the generation is not trusted
    fs.files[f"{HOME}/{R.checkpoint_file(r._slug(), 'gen-20990101T000000Z-22222222')}"] = R.seal({**cp, "complete": False})
    assert "not trusted" in raises(lambda: r5.teardown(apply=True))


# ---------------------------------------------------------------------------------------------- final review 2:
# 99_teardown: "gone" only from a structured not-found (the runner's rule, checks.is_not_found)
class TemporarilyUnavailable(Exception):
    error_code = "TEMPORARILY_UNAVAILABLE"


class HTTPError(Exception):
    def __init__(self, status):
        super().__init__(f"{status} Client Error")
        self.response = NS(status_code=status)


class AnalysisException(Exception):
    def __init__(self, cls, msg):
        super().__init__(msg)
        self._cls = cls

    def getErrorClass(self):
        return self._cls


def _raise(e):
    def fn():
        raise e
    return fn


def test_f2_teardown_step_gone_only_from_structured_not_found():
    from checks import attempt, is_not_found
    assert attempt(lambda: None) == ("ok", None)
    # permission error whose TEXT says "not found": unresolved
    assert attempt(_raise(PermissionDenied("User is not authorized; resource not found or no access (404)")))[0] == "failed"
    # transient 503: unresolved
    assert attempt(_raise(TemporarilyUnavailable("503 Service Unavailable: endpoint does not exist yet")))[0] == "failed"
    assert attempt(_raise(HTTPError(503)))[0] == "failed"
    assert attempt(_raise(RuntimeError("RESOURCE_DOES_NOT_EXIST: not found")))[0] == "failed"     # text only
    # structured not-found: gone
    assert attempt(_raise(NotFound("x")))[0] == "gone"
    assert attempt(_raise(type("ResourceDoesNotExist", (Exception,), {})("x")))[0] == "gone"
    assert attempt(_raise(HTTPError(404)))[0] == "gone"
    assert attempt(_raise(AnalysisException("TABLE_OR_VIEW_NOT_FOUND", "x")))[0] == "gone"
    assert attempt(_raise(AnalysisException("PERMISSION_DENIED", "table not found")))[0] == "failed"
    assert R.is_not_found is is_not_found                                       # one rule for both paths


def test_f2_teardown_notebook_uses_the_shared_rule_and_keeps_tables_on_failure():
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, "..", "notebooks", "99_teardown.py")).read()
    assert "gone_err" not in src and "attempt(fn)" in src and "is_not_found" in src
    assert '"not found"' not in src and '"does not exist"' not in src and '"404"' not in src
    # every table drop comes after a failures check, and an unreadable Genie space is a failure (not manual review)
    tail = src[src.index("RESULT_TABLES = "):]
    assert tail.index("if not DRY and failures") < tail.index("drop_tables(data_t)")
    assert tail.index("drop_tables(data_t)") < tail.index("drop_tables(result_t)") < tail.index("lab_manifest\", lambda")
    assert 'failures.append({"step": f"read Genie space {sid}"' in src


def test_f1_teardown_without_any_suite_has_nothing_to_export_but_results_tables_must_export():
    r, w, tables, fs, _ = make()
    r.store(create_generation=True).open(create=True)
    r.run_job = lambda key, params: {"state": "SUCCESS", "run_id": 1, "url": "u"}
    r._notebook_result = lambda res: {"state": res["state"]}
    destroyed = []
    r._destroy = lambda kind, rid, cp, st: destroyed.append(kind)
    r.export = lambda dep: (_ for _ in ()).throw(AssertionError("nothing ran: nothing to export"))
    out = r.teardown(apply=True)
    assert out["export"]["nothing_to_export"] and destroyed[-1] == "bundle_folder"
    r2, w2, tables2, fs2, _ = make()
    tables2.t["cat.lab_s.test_results"] = {"rows": [], "props": {}, "id": "t", "owner": "someone"}
    r2.export = lambda dep: (_ for _ in ()).throw(R.Stop("the results query returned no rows; nothing to export"))
    assert "no rows" in raises(lambda: r2.teardown(apply=True))


def test_f1_jobs_never_start_when_the_deployed_notebooks_are_missing():
    r, w, tables, fs, _ = make()
    r.store(create_generation=True).open(create=True)

    def missing(path):
        raise NotFound("RESOURCE_DOES_NOT_EXIST")
    w.workspace = NS(get_status=missing)
    r.run_job = lambda key, params: (_ for _ in ()).throw(AssertionError("must not start a job without notebooks"))
    assert "notebooks are missing" in raises(lambda: r.teardown(apply=True))
    assert "notebooks are missing" in raises(lambda: r.teardown(apply=False))
    w.workspace = NS(get_status=lambda path: (_ for _ in ()).throw(PermissionDenied("denied")))
    assert "couldn't check" in raises(lambda: r.teardown(apply=True))



# ---------------------------------------------------------------------------------------------- confirmation review
AUDIT_JOBS = {"lab_audit": "2", "lab_evidence": "8", "lab_collect": "4"}


def _audit(**kw):
    r, w, tables, fs, calls = make(mode="audit", account_id="", **kw)
    r._dep = R.Deployment(root=ROOT, state_dir=ROOT + "/state", target="customer_audit", jobs=dict(AUDIT_JOBS),
                          schemas={"lab": "cat.lab_s"}, source_root=ROOT + "/files", audit_mode="audit")
    destroyed = []
    r._destroy = lambda kind, rid, cp, st: destroyed.append(kind)
    return r, w, tables, fs, destroyed


def test_c1_audit_teardown_needs_no_account_id_but_demo_does():
    r, w, tables, fs, destroyed = _audit()
    assert r.binding().startswith("audit-only|42|customer_audit|" + ROOT)
    r.run_job = lambda key, params: (_ for _ in ()).throw(AssertionError("audit teardown runs no lab_teardown job"))
    out = r.teardown(apply=True)
    assert out["export"]["nothing_to_export"] and destroyed[-1] == "bundle_folder" and out["phases"]["export"] == "done"
    cp = [R.unseal(t, "cp") for p, t in fs.files.items() if "teardown-checkpoint-" in p][0]
    assert cp["complete"] and cp["binding"].startswith("audit-only|")
    # demo: account_id is still required (config) and a demo binding without it is refused
    assert "account_id" in raises(lambda: make(account_id=""), R.ConfigError)
    r2, *_ = make()
    assert "account_id is unknown" in raises(lambda: R.make_binding("", "42", "t", ROOT, r2.cfg, "demo"))
    assert R.make_binding("", "42", "t", ROOT, r2.cfg, "audit") == R.make_binding(ACCOUNT, "42", "t", ROOT, r2.cfg,
                                                                                  "audit")


def test_c2_never_ran_is_nothing_to_export():
    r, w, tables, fs, _ = make()
    r.store(create_generation=True).open(create=True)
    r.mark_generation(r._dep, "personas")
    r.ensure_steps_table()                                       # personas leaves an empty step log
    r.run_job = lambda key, params: {"state": "SUCCESS", "run_id": 1, "url": "u"}
    r._notebook_result = lambda res: {"state": res["state"]}
    r._destroy = lambda *a: None
    assert r.teardown(apply=True)["export"]["nothing_to_export"]


def test_c2_ran_but_results_tables_gone_stops():
    r, w, tables, fs, destroyed = _audit()
    r.run_job = lambda key, params: {"state": "SUCCESS", "run_id": 1, "url": "u"}
    r.base_params = lambda suite, groups: {}
    r.run()                                                      # audit-mode run: generation + run marker + step row
    rec = r.generation_record(r._dep)
    assert [a["action"] for a in rec["actions"]] == ["run"]
    assert tables.rows("cat.lab_s.lab_runner_steps")[0]["generation"] == rec["generation"]
    msg = raises(lambda: r.teardown(apply=True))                 # results tables don't exist (gone)
    assert "results missing for a generation that ran" in msg and "run started" in msg and not destroyed
    assert not [p for p in fs.files if "teardown-checkpoint-" in p]               # nothing recorded or removed
    # a step row alone (run marker lost) is still evidence of a run
    r2, w2, tables2, fs2, destroyed2 = _audit()
    r2.generation(r2._dep, create=True)
    r2.ensure_steps_table()
    tables2.insert("cat.lab_s.lab_runner_steps", {"run_id": "s", "step": "audit", "state": "SUCCESS"})
    assert "records 1 step" in raises(lambda: r2.teardown(apply=True)) and not destroyed2


def test_c2_unreadable_or_unexplained_step_log_stops():
    r, w, tables, fs, destroyed = _audit()
    r.generation(r._dep, create=True)
    r.mark_generation(r._dep, "personas")
    orig = tables.exists
    tables.exists = lambda full: (_ for _ in ()).throw(PermissionDenied("denied")) if full.endswith(
        "lab_runner_steps") else orig(full)
    assert "unreadable" in raises(lambda: r.teardown(apply=True)) and not destroyed
    tables.exists = orig
    assert "missing although personas ran" in raises(lambda: r.teardown(apply=True)) and not destroyed
    r2, w2, tables2, fs2, destroyed2 = _audit()                  # step log present, generation record says nothing
    r2.generation(r2._dep, create=True)
    r2.ensure_steps_table()
    assert "records no action" in raises(lambda: r2.teardown(apply=True)) and not destroyed2
    r3, w3, tables3, fs3, destroyed3 = _audit()                  # generation record unreadable
    r3.generation(r3._dep, create=True)
    r3._gen.clear()
    fs3.files[ROOT + "/runner/generation.json"] = fs3.files[ROOT + "/runner/generation.json"].replace('"actions"', '"x"')
    assert "checksum" in raises(lambda: r3.teardown(apply=True)) and not destroyed3


def test_c3_every_notebook_of_the_actions_jobs_must_exist():
    r, w, tables, fs, destroyed = _audit()
    seen = []

    def status(path):
        seen.append(path)
        if path.endswith("nb_8"):                                # the evidence job's notebook is missing
            raise NotFound("RESOURCE_DOES_NOT_EXIST")
        return NS(object_type="NOTEBOOK")
    w.workspace = NS(get_status=status)
    r.require_deployed_files(r._dep, ["lab_audit"])              # run (audit mode) only needs the audit job
    assert "nb_8" in raises(lambda: r.require_deployed_files(r._dep))            # preflight: every job
    assert "nb_8" in raises(lambda: r.require_deployed_files(r._dep, ["lab_evidence"]))
    r2, w2, *_ = make(action="run")
    w2.workspace = NS(get_status=lambda p: (_ for _ in ()).throw(NotFound("x")) if p.endswith("nb_4") else None)
    r2.run_job = lambda key, params: (_ for _ in ()).throw(AssertionError("no job may start"))
    assert "nb_4" in raises(lambda: r2.run())                    # collect's notebook missing: nothing starts



# ---------------------------------------------------------------------------------------------- audit-only teardown
def _audit_run(pre_existing=()):
    """An audit deployment whose audit job creates the four results tables; pre_existing exist before the run."""
    r, w, tables, fs, destroyed = _audit()
    for t in pre_existing:
        tables.create(f"cat.lab_s.{t}", "x STRING", "", {})

    def audit_job(key, params):
        for t in R.AUDIT_RESULT_TABLES:
            if f"cat.lab_s.{t}" not in tables.t:
                tables.create(f"cat.lab_s.{t}", "x STRING", "", {})
        return {"state": "SUCCESS", "run_id": 1, "url": "u"}
    r.run_job = audit_job
    r.base_params = lambda suite, groups: {}
    r.run()
    exp = f"{HOME}/audit.csv"
    fs.files[exp] = "a\r\n1\r\n"
    r.export = lambda dep: {"path": exp, "sha256": R.sha256(fs.files[exp])}
    return r, w, tables, fs, destroyed


def test_a1_proven_audit_results_tables_are_dropped():
    r, w, tables, fs, destroyed = _audit_run()
    proofs = r.generation_record(r._dep)["results_tables"]
    assert set(proofs) == set(R.AUDIT_RESULT_TABLES) and all(v["absent_before"] and v["table_id"] for v in proofs.values())
    plan = r.teardown(apply=False)["audit_results"]
    assert {a for _, a, _ in plan} == {"drop"} and not [o for o in tables.ops if o[0] == "drop"]   # plan changes nothing
    out = r.teardown(apply=True)
    assert out["phases"]["audit_results"] == "done" and destroyed[-1] == "bundle_folder"
    assert sorted(f for op, f in tables.ops if op == "drop") == sorted(f"cat.lab_s.{t}" for t in R.AUDIT_RESULT_TABLES)
    assert not any(f"cat.lab_s.{t}" in tables.t for t in R.AUDIT_RESULT_TABLES)


def test_a2_pre_existing_table_is_manual_and_stops():
    r, w, tables, fs, destroyed = _audit_run(pre_existing=("test_results",))
    assert r.generation_record(r._dep)["results_tables"]["test_results"]["absent_before"] is False
    msg = raises(lambda: r.teardown(apply=True))
    assert "manual review" in msg and "test_results: it existed before" in msg
    assert not [o for o in tables.ops if o[0] == "drop"] and not destroyed          # nothing dropped or destroyed


def test_a3_replaced_table_is_manual_and_stops():
    r, w, tables, fs, destroyed = _audit_run()
    tables.drop("cat.lab_s.suite_runs")
    tables.create("cat.lab_s.suite_runs", "x STRING", "", {})                       # same name, new UC table ID
    tables.ops.clear()
    msg = raises(lambda: r.teardown(apply=True))
    assert "suite_runs: table ID" in msg and "replaced" in msg
    assert not [o for o in tables.ops if o[0] == "drop"] and not destroyed
    r2, w2, tables2, fs2, destroyed2 = _audit()                                      # no creation record at all
    tables2.create("cat.lab_s.test_results", "x STRING", "", {})                    # created outside a recorded run
    r2.generation(r2._dep, create=True)
    fs2.files[f"{HOME}/a.csv"] = "x"
    r2.export = lambda dep: {"path": f"{HOME}/a.csv", "sha256": R.sha256("x")}
    msg = raises(lambda: r2.teardown(apply=True))
    assert "test_results: no creation record" in msg and not destroyed2


def test_a4_resume_after_a_partial_drop():
    r, w, tables, fs, destroyed = _audit_run()
    real_drop, n = tables.drop, []

    def flaky(full):
        if len(n) == 2:
            raise RuntimeError("connection reset")                                  # stops after two drops
        n.append(full)
        real_drop(full)
    tables.drop = flaky
    try:
        r.teardown(apply=True)
        raise AssertionError("expected the drop to fail")
    except RuntimeError:
        pass
    assert len(n) == 2 and not destroyed
    tables.drop = real_drop
    r2, *_ = make(mode="audit", account_id="", fs=fs, tables=tables)                 # a fresh notebook run resumes
    r2._dep, r2._destroy, r2.export = r._dep, (lambda kind, rid, cp, st: destroyed.append(kind)), r.export
    out = r2.teardown(apply=True)
    assert out["phases"]["audit_results"] == "done" and destroyed[-1] == "bundle_folder"
    assert not any(f"cat.lab_s.{t}" in tables.t for t in R.AUDIT_RESULT_TABLES)
