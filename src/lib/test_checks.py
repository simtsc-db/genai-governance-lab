"""Self-tests for the denial classifier. Run: python3 src/lib/test_checks.py  (or python -m pytest src/lib)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from checks import (ENFORCE_GATEWAY_KEY, GATEWAY_ENFORCED_RE, LAB_GENIE_DESCRIPTION, LAB_GENIE_PAIRS,  # noqa: E402
                    LAB_GENIE_TITLE, ai_endpoint_inventory, classify, endpoint_kind,
                    gateway_enforcement_verdict, usage_tracking_enabled, valid_lookback_days, fetch_full_endpoints, USAGE_CAVEAT, compare_fixture, expected_probe_name,  # noqa: E402
                    fixture_row, has_proof, is_lab_genie_space, masked, table_not_available)

RAW = "genie_governance_lab.genie_governance_lab.clinical_notes_raw"
TOOLS = "genie_governance_lab.genie_governance_lab_tools"


def test_true_positive_select_on_target():
    m = f"[INSUFFICIENT_PERMISSIONS] Insufficient privileges: User does not have SELECT on Table '{RAW}'. SQLSTATE: 42501"
    assert classify(m, [RAW], ["SELECT"]) == "denied"


def test_true_positive_backticked_name():
    m = "PERMISSION_DENIED: User does not have SELECT on table `genie_governance_lab`.`genie_governance_lab`.`clinical_notes_raw`."
    assert classify(m, [RAW], ["SELECT"]) == "denied"


def test_missing_use_catalog_with_echoed_select():
    m = (f"[INSUFFICIENT_PERMISSIONS] User does not have USE CATALOG on Catalog 'genie_governance_lab'. "
         f"== SQL == SELECT * FROM {RAW} LIMIT 1")
    assert classify(m, [RAW], ["SELECT"]) == "unrelated"


def test_select_on_a_different_table():
    m = "User does not have SELECT on Table 'genie_governance_lab.genie_governance_lab.other_table'."
    assert classify(m, [RAW], ["SELECT"]) == "unrelated"


def test_workspace_access_denied():
    assert classify("PERMISSION_DENIED: Workspace access denied", [RAW], ["SELECT"]) == "unrelated"


def test_generic_403():
    m = "PermissionDenied: User does not have permission to access this resource."
    assert classify(m, [TOOLS, f"{TOOLS}.lab_mcp_conn"], ["USE_CONNECTION", "CREATE_SERVICE"]) == "unrelated"


def test_create_service_on_build_schema():
    m = f"PermissionDenied: Principal 'p' does not have CREATE SERVICE on Schema '{TOOLS}'. Config: host=x"
    assert classify(m, [TOOLS], ["CREATE_SERVICE"]) == "denied"
    assert classify(m, [TOOLS], ["USE_CONNECTION"]) == "unrelated"


def test_use_schema_is_not_create_service():
    m = f"Principal 'p' does not have USE SCHEMA on Schema '{TOOLS}'."
    assert classify(m, [TOOLS], ["CREATE_SERVICE"]) == "unrelated"


def test_genie_space_id_must_match():
    m = "Unable to get space [00000000000000000000000000000001]. Caused by User u does not have read permission for node"
    assert classify(m, ["00000000000000000000000000000001"], ["CAN_RUN"]) == "denied"
    assert classify(m, ["00000000000000000000000000000002"], ["CAN_RUN"]) == "unrelated"
    assert classify("You do not have access to this space", ["00000000"], ["CAN_RUN"]) == "unrelated"


def test_hidden_service_full_name():
    m = "'genie_governance_lab.genie_governance_lab.lab_chat' does not exist."
    assert classify(m, ["genie_governance_lab.genie_governance_lab.lab_chat"], ["EXECUTE"], 404) == "denied"
    assert classify(m, ["other.schema.lab_chat"], ["EXECUTE"], 404) == "unrelated"


def test_hidden_function_exact_name():
    fn = "genie_governance_lab.genie_governance_lab_tools.facility_capacity"
    m = f"MCPError: BAD_REQUEST: Function '{fn}' not found"
    assert classify(m, [fn], ["EXECUTE"]) == "denied"
    assert classify("MCPError: BAD_REQUEST: Function 'x.y.other' not found", [fn], ["EXECUTE"]) == "unrelated"


def test_vector_index_and_stacktrace():
    idx = "genie_governance_lab.genie_governance_lab.clinical_notes_idx"
    m = (f'403 {{"error_code":"PERMISSION_DENIED","message":"Insufficient permissions for UC entity {idx}"}}\n\n'
         "JVM stacktrace:\norg.apache.spark.SparkException\n\tat org.apache.spark.sql.execution.SparkPlan.execute(X.scala:1)")
    assert classify(m, [idx], ["SELECT"]) == "denied"


def test_serving_and_experiment():
    assert classify("You do not have permission to query the endpoint. name: ep1", ["ep1"], ["CAN_QUERY", "QUERY"], 403) == "denied"
    assert classify("User 'u' does not have permission to 'View' experiment with id 131", ["131"], ["VIEW"]) == "denied"
    assert classify("User 'u' does not have permission to 'View' experiment with id 999", ["131"], ["VIEW"]) == "unrelated"


def test_use_connection_on_builders_connection():
    conn = f"{TOOLS}.lab_mcp_conn_builders"
    m = f"PermissionDenied: User does not have USE CONNECTION on Connection '{conn}'."
    assert classify(m, [TOOLS, conn], ["USE_CONNECTION", "CREATE_SERVICE"]) == "denied"
    other = f"PermissionDenied: User does not have USE CONNECTION on Connection '{TOOLS}.lab_mcp_conn'."
    assert classify(other, [TOOLS, conn], ["USE_CONNECTION", "CREATE_SERVICE"]) == "unrelated"


def test_probe_names_per_test():
    assert expected_probe_name("mcp-services", "clinician", "0123abcd", TOOLS) == f"{TOOLS}.probe_clinician_0123abcd_mcp"
    assert expected_probe_name("mcp-services", "clinician", "0123abcd", TOOLS, "B-04b") == \
        f"{TOOLS}.probe_clinician_0123abcd_mcpb"
    assert expected_probe_name("mcp-services", "clinician", "0123abcd", TOOLS, "B-04") == \
        f"{TOOLS}.probe_clinician_0123abcd_mcp"


def test_manifest_proof_marker():
    assert has_proof('{"confirmed_absent_before": true}')
    assert has_proof({"confirmed_absent_before": True, "endpoint": "x"})
    # legacy adoption (status/comment/owner) is not creation proof
    assert not has_proof('{"adopted_from": "lab_setup_status", "evidence": "lab comment, owner = deployer"}')
    assert not has_proof('{"confirmed_absent_before": "true"}')
    assert not has_proof('{"confirmed_absent_before": true, "unproven": true}')
    assert not has_proof("") and not has_proof(None) and not has_proof("not json") and not has_proof("[1]")


OLD_TITLE = "[lab] Clinic visits (synthetic)"
OLD_DESC = "Synthetic visit data. Unity Catalog masks and a row filter apply per user. Genie governance lab."


def test_lab_genie_space_exact_pairs():
    assert is_lab_genie_space(OLD_TITLE, OLD_DESC)                          # earlier version's exact pair
    assert is_lab_genie_space(LAB_GENIE_TITLE, LAB_GENIE_DESCRIPTION)       # current version's exact pair
    assert (LAB_GENIE_TITLE, LAB_GENIE_DESCRIPTION) in LAB_GENIE_PAIRS


def test_lab_genie_space_similar_rejected():
    assert not is_lab_genie_space("Clinic visits (GenAI governance lab) copy", LAB_GENIE_DESCRIPTION)
    assert not is_lab_genie_space("My GenAI governance lab space", "About the GenAI governance lab.")
    assert not is_lab_genie_space(LAB_GENIE_TITLE, LAB_GENIE_DESCRIPTION + " Edited.")
    assert not is_lab_genie_space(LAB_GENIE_TITLE, OLD_DESC)               # pairs don't mix
    assert not is_lab_genie_space(OLD_TITLE + " ", OLD_DESC)
    assert not is_lab_genie_space("[lab] Clinic visits", "Genie governance lab")
    assert not is_lab_genie_space(None, None)


INF = "genie_governance_lab.genie_governance_lab.lab_chat_payload"


def test_inference_table_exact_not_found():
    assert table_not_available("[TABLE_OR_VIEW_NOT_FOUND] The table or view `genie_governance_lab`.`genie_governance_lab`."
                               "`lab_chat_payload` cannot be found. Verify the spelling and correctness of the schema "
                               "and catalog. SQLSTATE: 42P01", INF)
    assert table_not_available(f"Table '{INF.upper()}' does not exist.", INF)       # case-insensitive
    assert table_not_available(f"Inference table {INF} is provisioning; try again later", INF)


def test_inference_table_other_errors_rejected():
    assert not table_not_available("[NO_SUCH_CATALOG_EXCEPTION] Catalog 'genie_governance_lab' was not found.", INF)
    assert not table_not_available("[SCHEMA_NOT_FOUND] The schema `genie_governance_lab`.`genie_governance_lab` "
                                   "cannot be found. SQLSTATE: 42704", INF)
    assert not table_not_available(f"[TABLE_OR_VIEW_NOT_FOUND] The table or view `{INF}_v2` cannot be found.", INF)
    assert not table_not_available("[TABLE_OR_VIEW_NOT_FOUND] The table or view "
                                   "`genie_governance_lab`.`genie_governance_lab`.`other_payload` cannot be found.", INF)
    assert not table_not_available(f"[TABLE_OR_VIEW_NOT_FOUND] The table or view `x`.`{INF}` cannot be found.", INF)
    assert not table_not_available(f"PERMISSION_DENIED: User does not have SELECT on Table '{INF}'", INF)
    assert not table_not_available(f"[INSUFFICIENT_PERMISSIONS] Insufficient privileges on {INF}", INF)
    assert not table_not_available("Model service genie_governance_lab.genie_governance_lab.lab_chat not found", INF)
    assert not table_not_available(f"Table '{INF}' does not exist.", "lab_chat_payload")   # not a 3-part name


# Error texts in the shape the platform returns with Enforce Unity Gateway on (illustrative values)
B05_EP = "lab-probe-clinician-00000000"
B05_ENFORCED = (f"PermissionDenied: Creating external model endpoint '{B05_EP}' is disabled for this workspace. "
                "Please use Unity Gateway. Config: host=<workspace-url>, auth_type=runtime")
B05_PRIVS = ["workspace-access", "entitlement", "serving endpoint"]
PPT_DISABLED = ("[INTERNAL_ERROR] Remote session creation failed: PERMISSION_DENIED: PERMISSION_DENIED: Querying "
                "pay-per-token foundation model endpoint 'databricks-claude-sonnet-4-6' is disabled for this workspace. "
                "Please use Unity Gateway. SQLSTATE: XX000")


def test_b05_enforced_exact_endpoint_is_denied():
    assert classify(B05_ENFORCED, [TOOLS, B05_EP], B05_PRIVS) == "denied"
    assert classify(B05_ENFORCED.upper(), [TOOLS, B05_EP], B05_PRIVS) == "denied"     # case-insensitive


def test_b05_other_endpoint_or_generic_403_is_not_denied():
    other = B05_ENFORCED.replace(B05_EP, "lab-probe-outsider-00000001")
    assert classify(other, [TOOLS, B05_EP], B05_PRIVS) == "unrelated"
    assert classify("PermissionDenied: User does not have permission to access this resource.",
                    [TOOLS, B05_EP], B05_PRIVS) == "unrelated"
    # the enforcement text without the endpoint name is not attributable (SKIP, never a PASS)
    assert classify("Creating external model endpoints is disabled for this workspace. Please use Unity Gateway.",
                    [TOOLS, B05_EP], B05_PRIVS) != "denied"


def test_pay_per_token_disabled_is_not_a_denial_of_the_target():
    assert classify(PPT_DISABLED, ["databricks-claude-sonnet-4-6x", "a.b.patient_visits"], ["EXECUTE"]) == "unrelated"
    assert GATEWAY_ENFORCED_RE.search(PPT_DISABLED) and GATEWAY_ENFORCED_RE.search(B05_ENFORCED)
    assert not GATEWAY_ENFORCED_RE.search("PermissionDenied: User does not have permission to access this resource.")


def test_b08_gateway_enforcement_verdict():
    on = {"boolean_val": {"value": True}, "effective_boolean_val": {"value": True}, "name": ENFORCE_GATEWAY_KEY}
    off = {"boolean_val": {"value": False}, "effective_boolean_val": {"value": False}, "name": ENFORCE_GATEWAY_KEY}
    keys = {ENFORCE_GATEWAY_KEY, "example_other_setting"}
    assert gateway_enforcement_verdict(keys, on)[0] == "PASS"
    assert gateway_enforcement_verdict(None, on)[0] == "PASS"            # list unreadable, value readable
    assert gateway_enforcement_verdict(keys, off)[0] == "FAIL"
    assert gateway_enforcement_verdict({"example_other_setting"}, on)[0] == "MANUAL"   # not offered here
    st, obs = gateway_enforcement_verdict(keys, {"error": "PERMISSION_DENIED: only workspace admins"})
    assert st == "MANUAL" and "Settings > Advanced" in obs
    assert gateway_enforcement_verdict(keys, {"name": ENFORCE_GATEWAY_KEY})[0] == "MANUAL"


def _ep(name, ents, task=None, creator="someone@example.com"):
    return {"name": name, "creator": creator, "task": task, "state": {"ready": "READY"},
            "config": {"served_entities": ents} if ents is not None else {}}


EM = _ep("team-gpt", [{"name": "gpt", "external_model": {"name": "gpt-4o", "provider": "openai"}}])
PT = _ep("pt-llama", [{"name": "llama", "entity_name": "system.ai.llama_v3_3_70b_instruct",
                       "min_provisioned_throughput": 0, "max_provisioned_throughput": 9500}])
AGENT = _ep("support-agent", [{"name": "a_1", "entity_name": "c.s.agent", "entity_version": "1", "type": "UC_MODEL"}],
            task="agent/v1/responses")
KA = _ep("ka-1234-endpoint", None, task="agent/v1/responses")
PPT_USAGE = [{"endpoint_name": "databricks-claude-sonnet-4-6", "entity_type": "FOUNDATION_MODEL", "requests": 395,
              "last_request": "2026-11-25T12:13:23Z"}]


def test_b09_endpoint_kinds():
    assert endpoint_kind(EM)[0] == "external_model"
    assert endpoint_kind(PT)[0] == "pt"
    assert endpoint_kind(AGENT)[0] == "other" and endpoint_kind(KA)[0] == "other"
    mixed = _ep("mixed", [{"name": "m", "entity_name": "c.s.m", "type": "UC_MODEL"}, {"name": "x", "external_model": {"name": "y"}}])
    assert endpoint_kind(mixed)[0] == "external_model"          # riskiest entity wins
    legacy = {"name": "old", "config": {"served_models": [{"name": "o", "model_name": "c.s.m", "model_version": "1"}]}}
    assert endpoint_kind(legacy)[0] == "other"


ADMIN = {"admin": True, "basis": "SCIM Me groups include 'admins'"}
NON_ADMIN = {"admin": False, "basis": "SCIM Me groups don't include 'admins'"}


def test_b09_empty_admin_list_passes_with_usage_caveat():
    st, obs, ev = ai_endpoint_inventory([], [], visibility=ADMIN)
    assert st == "PASS" and "absence is not proof" in obs and ev["usage"]["caveat"] == USAGE_CAVEAT
    st, obs, _ = ai_endpoint_inventory([], PPT_USAGE, visibility=ADMIN)          # old traffic: note only, still PASS
    assert st == "PASS" and "databricks-claude-sonnet-4-6" in obs and "supplementary" in obs


def test_b09_usage_never_affects_verdict():
    for eps, want in (([], "PASS"), ([AGENT], "INFO"), ([EM], "FAIL")):
        got = {ai_endpoint_inventory(eps, u, visibility=ADMIN)[0] for u in (None, [], PPT_USAGE)}
        assert got == {want}, (eps, got)


def test_b09_all_endpoints_fetched_beyond_500():
    listed = [{"name": f"ep-{i}"} for i in range(750)]
    calls = []
    def get(name):
        calls.append(name)
        return _ep(name, [{"name": "m", "entity_name": "c.s.m", "type": "UC_MODEL"}])
    full, failed = fetch_full_endpoints(listed, get, sleep=lambda s: None)
    assert len(full) == 750 and not failed and len(calls) == 750
    st, _, ev = ai_endpoint_inventory(full, None, visibility=ADMIN, incomplete=failed)
    assert st == "INFO" and ev["counts"]["other_ai"] == 750 and len(ev["other_ai"]) == 20


def test_b09_one_get_failure_is_manual():
    listed = [{"name": "a"}, {"name": "b"}]
    tries = {"a": 0, "b": 0}
    def get(name):
        tries[name] += 1
        if name == "b":
            raise RuntimeError("503 temporarily unavailable")
        return _ep(name, [{"name": "m", "entity_name": "c.s.m", "type": "UC_MODEL"}])
    full, failed = fetch_full_endpoints(listed, get, retries=3, sleep=lambda s: None)
    assert failed == ["b"] and tries["b"] == 3 and tries["a"] == 1        # bounded retries
    st, obs, ev = ai_endpoint_inventory(full, [], visibility=ADMIN, incomplete=failed)
    assert st == "MANUAL" and "classification incomplete" in obs and ev["incomplete_count"] == 1
    flaky = {"n": 0}
    def get_flaky(name):
        flaky["n"] += 1
        if flaky["n"] < 3:
            raise RuntimeError("timeout")
        return _ep(name, [])
    assert fetch_full_endpoints([{"name": "c"}], get_flaky, sleep=lambda s: None)[1] == []   # recovered on retry


def test_b09_get_failure_but_external_model_fails():
    listed = [{"name": "broken"}, {"name": "team-gpt"}]
    def get(name):
        if name == "broken":
            raise RuntimeError("500")
        return EM
    full, failed = fetch_full_endpoints(listed, get, sleep=lambda s: None)
    assert ai_endpoint_inventory(full, None, visibility=ADMIN, incomplete=failed)[0] == "FAIL"
    assert ai_endpoint_inventory([PT], None, visibility=NON_ADMIN)[0] == "FAIL"            # any caller


def test_b09_non_admin_is_manual():
    st, obs, ev = ai_endpoint_inventory([], [], visibility=NON_ADMIN)
    assert st == "MANUAL" and "non-admin" in obs and ev["visibility"]["admin"] is False
    assert ai_endpoint_inventory([], [])[0] == "MANUAL"                                   # visibility unknown
    assert ai_endpoint_inventory([AGENT], [], visibility=NON_ADMIN)[0] == "MANUAL"


def test_b09_other_ai_endpoints_info_with_labels_and_tracking():
    eps = [AGENT, KA, _ep("echo", [{"name": "e", "entity_name": "c.s.e", "type": "UC_MODEL"}]),
           {"name": "old", "config": {"served_models": [{"name": "o", "model_name": "c.s.m"}]}}, _ep("bare", None),
           _ep("databricks-gte-large-en", [{"name": "g", "foundation_model": {"name": "gte"}}])]
    st, obs, ev = ai_endpoint_inventory(eps, [], visibility=ADMIN, tracking={"support-agent": False, "echo": True})
    assert st == "INFO" and "usage tracking off" in obs and "usage tracking on" in obs
    labels = {r["name"]: r["other_type"] for r in ev["other_ai"]}
    assert labels == {"support-agent": "Knowledge Assistant/agent", "ka-1234-endpoint": "Knowledge Assistant/agent",
                      "echo": "custom model", "old": "custom model", "bare": "no served entities listed",
                      "databricks-gte-large-en": None}
    assert ev["counts"]["databricks_ppt_listed"] == 1


def test_b09_external_model_or_pt_fails_and_is_capped():
    st, obs, ev = ai_endpoint_inventory([EM, AGENT], [], lab_owned={"team-gpt"}, visibility=ADMIN)
    assert st == "FAIL" and "team-gpt" in obs and "lab-owned" in obs and ev["counts"]["external_model"] == 1
    mixed = _ep("mixed", [{"name": "m", "type": "UC_MODEL"}, {"name": "x", "external_model": {"name": "y"}}])
    assert ai_endpoint_inventory([mixed, KA], [], visibility=NON_ADMIN)[0] == "FAIL"
    many = [_ep(f"em-{i}", [{"name": "e", "external_model": {"name": "m"}}]) for i in range(50)]
    st, _, ev = ai_endpoint_inventory(many, [], visibility=ADMIN)
    assert st == "FAIL" and ev["counts"]["external_model"] == 50 and len(ev["external_or_pt"]) == 20
    assert "config" not in ev["external_or_pt"][0]


def test_b09_unreadable_list_is_manual():
    assert ai_endpoint_inventory(None, [], visibility=ADMIN)[0] == "MANUAL"


def test_b09_usage_tracking_flag():
    assert usage_tracking_enabled({"ai_gateway": {"usage_tracking_config": {"enabled": True}}}) is True
    assert usage_tracking_enabled({"ai_gateway": {"usage_tracking_config": {"enabled": False}}}) is False
    assert usage_tracking_enabled({"ai_gateway": None}) is None and usage_tracking_enabled({}) is None


def test_b09_lookback_validation():
    assert valid_lookback_days("30") == 30 and valid_lookback_days(1) == 1
    for bad in ("0", "366", "30; DROP TABLE x", "", None):
        try:
            valid_lookback_days(bad)
            assert False, bad
        except ValueError:
            pass


def test_fixture_compare():
    rows = [{"visit_id": i, "nric": masked(fixture_row(i)["nric"])} for i in (4, 8, 12)]
    assert compare_fixture(rows, ("nric",))[0] == "masked"
    rows[0]["nric"] = fixture_row(4)["nric"]
    assert compare_fixture(rows, ("nric",))[0] == "mismatch"


# --------------------------------------------------------------------------------------------
# Synthetic people, configurable names, persona identities
# --------------------------------------------------------------------------------------------
def test_fixture_people_are_fictional_labels():
    from checks import PATIENT_LABEL_RE, patient_label, nric_like, phone_like, INVALID_CHECK_LETTERS
    for i in range(1, 300):
        r = fixture_row(i)
        assert PATIENT_LABEL_RE.fullmatch(r["patient_name"]) and r["patient_name"] == patient_label(i)
        assert r["nric"] == nric_like(i) and r["nric"][0] == "S" and r["nric"][-1] in INVALID_CHECK_LETTERS
        assert r["phone"] == phone_like(i) and r["phone"].startswith("5") and len(r["phone"]) == 8
    # same formula as the SQL generator in 00_setup_data: 'Test Patient ' || lpad(id % 97 + 1, 3, '0')
    assert patient_label(96) == "Test Patient 097" and patient_label(97) == "Test Patient 001"


def test_synthetic_sentence_is_built_at_runtime_and_detected():
    from checks import synthetic_sentence, pii_leaks
    s = synthetic_sentence()
    r = fixture_row(4)
    assert r["nric"] in s and r["phone"] in s and r["patient_name"] in s
    assert sorted(pii_leaks(s)) == sorted([r["nric"], r["phone"], r["patient_name"]])
    assert pii_leaks("Patient [NAME] (NRIC [NRIC], tel [PHONE])") == []


def test_lab_names_default_prefix_keeps_historical_names():
    from checks import lab_names
    n = lab_names()
    assert (n["chat"], n["chat_inference_prefix"], n["mcp_conn"], n["mcp_conn_builders"]) == \
        ("lab_chat", "lab_chat", "lab_mcp_conn", "lab_mcp_conn_builders")
    assert (n["tool_fn"], n["nric_guard"], n["vs_index"], n["experiment"]) == \
        ("facility_capacity", "nric_guard_py", "clinical_notes_idx", "genie_governance_lab_traces")
    assert (n["abac_policy"], n["sg_policy"], n["mask_fn"], n["filter_fn"], n["abac_mask_fn"]) == \
        ("lab_mask_tagged_pii", "lab_mask_sg_identifiers", "mask_pii", "facility_filter", "abac_mask")
    assert n["probe_endpoint"] == "glab-probe" and n["jobs"]["lab_setup"] == "lab_setup"


def test_lab_names_custom_prefix_renames_everything():
    from checks import lab_names, prefix_ok
    n = lab_names("labtest")
    flat = [v for k, v in n.items() if k != "jobs"] + list(n["jobs"].values())
    assert all(v.startswith("labtest") for v in flat), flat
    assert n["jobs"]["lab_persona_pii_reader"] == "labtest_persona_pii_reader" and n["probe_endpoint"] == "labtest-probe"
    for bad in ("", "Lab", "1lab", "a", "lab-x", "lab_x", "x" * 17, "lab;drop"):
        try:
            prefix_ok(bad); assert False, bad
        except ValueError:
            pass


def test_persona_identities_and_marker():
    from checks import persona_identities, is_marked, identity_marker
    ids = persona_identities("labtest")
    assert ids["service_principals"] == {"clinician": "labtest-clinician", "pii_reader": "labtest-pii-reader",
                                         "outsider": "labtest-outsider"}
    assert ids["groups"]["all_facilities"] == "labtest_all_facilities"
    assert persona_identities()["groups"]["clinicians"] == "lab_clinicians"
    assert identity_marker("x") == "genie-governance-lab:x"
    assert is_marked("x", "genie-governance-lab:x") and not is_marked("x", "genie-governance-lab:y")
    assert not is_marked("x", None) and not is_marked("", "genie-governance-lab:")


def test_probe_endpoint_name_follows_prefix():
    assert expected_probe_name("serving-endpoints", "pii_reader", "0123abcd", TOOLS) == "glab-probe-pii-reader-0123abcd"
    assert expected_probe_name("serving-endpoints", "outsider", "0123abcd", TOOLS, None, "labtest-probe") == \
        "labtest-probe-outsider-0123abcd"
    assert expected_probe_name("serving-endpoints", "outsider", "0123abcd", TOOLS, None, "x.y-probe") is None


# --------------------------------------------------------------------------------------------
# Workspace-native runner (pure parts)
# --------------------------------------------------------------------------------------------
def _cfg(**kw):
    from runner import parse_config
    raw = {"action": "run", "mode": "demo", "catalog": "cat", "schema": "lab_s", "resource_prefix": "labtest",
           "persona_prefix": "labtest", "warehouse_id": "abc123", "account_id": "0" * 8 + "-0000-0000-0000-" + "0" * 12}
    raw.update(kw)
    return parse_config(raw)


def test_runner_config_validates_every_widget():
    from runner import ConfigError
    c = _cfg(approved_builders="a, b", usage_lookback_days="7")
    assert c.full_schema == "cat.lab_s" and c.tools_schema == "cat.lab_s_tools" and c.approved_builders == ("a", "b")
    try:
        _cfg(account_id=""); assert False, "demo run without account_id"
    except ConfigError:
        pass
    assert _cfg(mode="audit", account_id="").account_id == ""
    for bad in ({"action": "destroy"}, {"mode": "both"}, {"catalog": ""}, {"catalog": "a`b"}, {"schema": "x y"},
                {"resource_prefix": "LAB"}, {"persona_prefix": "a"}, {"account_id": "acct-1"},
                {"warehouse_id": ""}, {"warehouse_id": "w;x"}, {"vs_endpoint": "e p"}, {"expected_policy": "p'"},
                {"approved_builders": "a`b"}, {"usage_lookback_days": "0"}, {"run_id": "r;1"},
                {"action": "resume", "run_id": ""}, {"manual_app_ids": "not-a-uuid"},
                {"manual_app_ids": "00000000-0000-0000-0000-000000000000"},
                {"manual_groups": "a,b,c,d"}):
        try:
            _cfg(**bad); assert False, bad
        except ConfigError:
            pass
    assert _cfg(action="preflight", warehouse_id="").warehouse_id == ""


def test_runner_widget_defaults_point_nowhere():
    from runner import WIDGETS
    d = {k: v for k, v, _ in WIDGETS}
    assert d["catalog"] == d["warehouse_id"] == d["account_id"] == d["vs_endpoint"] == ""
    assert d["resource_prefix"] == d["persona_prefix"] == "lab" and d["action"] == "preflight"


def test_runner_parse_bundle_state_direct_and_terraform():
    from runner import parse_bundle_state
    res = {"state": {"resources.jobs.lab_audit": {"__id__": "11"}, "resources.jobs.lab_audit.permissions": {"__id__": "/jobs/11"},
                     "resources.schemas.lab": {"__id__": "cat.lab_s"}, "resources.schemas.lab.grants": {"__id__": "schema/x"},
                     "resources.model_services.lab_chat": {"__id__": "cat.lab_s.labtest_chat"},
                     "resources.dashboards.lab_results": {"__id__": "d1"}}}
    st = parse_bundle_state(res)
    assert st == {"jobs": {"lab_audit": "11"}, "dashboards": {"lab_results": "d1"},
                  "model_services": {"lab_chat": "cat.lab_s.labtest_chat"}, "schemas": {"lab": "cat.lab_s"}}
    tf = {"resources": [{"mode": "managed", "type": "databricks_job", "name": "lab_audit", "instances": [{"attributes": {"id": "11"}}]},
                        {"mode": "data", "type": "databricks_job", "name": "x", "instances": [{"attributes": {"id": "9"}}]},
                        {"mode": "managed", "type": "databricks_schema", "name": "lab", "instances": [{"attributes": {"id": "cat.lab_s"}}]}]}
    assert parse_bundle_state(None, tf)["jobs"] == {"lab_audit": "11"}


def test_runner_match_deployment_needs_its_own_job_and_schema():
    from runner import match_deployment
    c = _cfg()
    good = {"jobs": {"lab_audit": "11"}, "schemas": {"lab": "cat.lab_s"}}
    assert match_deployment(c, 11, good) is None
    assert match_deployment(c, 12, good)                                   # another deployment's job
    assert match_deployment(c, 11, {"jobs": {"lab_audit": "11"}, "schemas": {"lab": "cat.other"}})


def test_runner_source_root():
    from runner import source_root_of
    assert source_root_of("/Workspace/Users/u/.bundle/b/t/files/src/notebooks/20_audit_settings") == "/Workspace/Users/u/.bundle/b/t/files"
    assert source_root_of("/Users/u/repo/src/notebooks/30_persona_probe") == "/Workspace/Users/u/repo"
    assert source_root_of("/Users/u/elsewhere/nb") is None and source_root_of(None) is None


def test_runner_never_adopts_unrecorded_same_name_identities():
    from runner import decide_identity
    from checks import identity_marker
    rec = {"kind": "group", "scim_id": "5", "display_name": "labtest_clinicians",
           "proof": '{"confirmed_absent_before": true, "marker": "%s"}' % identity_marker("labtest_clinicians")}
    assert decide_identity([], None, "labtest_clinicians") == ("create", None)
    assert decide_identity([{"id": "5"}], rec, "labtest_clinicians")[0] == "reuse"
    assert decide_identity([{"id": "6"}], rec, "labtest_clinicians")[0] == "refuse"            # same name, other ID
    assert decide_identity([{"id": "6"}], None, "labtest_clinicians")[0] == "refuse"           # unrecorded, no marker
    assert decide_identity([{"id": "5"}], {**rec, "proof": "{}"}, "labtest_clinicians")[0] == "refuse"
    act, x = decide_identity([{"id": "7", "externalId": identity_marker("labtest_clinicians")}], None, "labtest_clinicians")
    assert act == "reuse" and x["_origin"] == "marked"
    assert decide_identity([{"id": "1"}, {"id": "2"}], rec, "labtest_clinicians")[0] == "refuse"


def test_runner_identity_teardown_deletes_only_proven_records():
    from runner import identity_teardown_action
    from checks import identity_marker
    n = "labtest-outsider"
    rec = {"kind": "service_principal", "scim_id": "9", "display_name": n, "application_id": "a",
           "proof": '{"confirmed_absent_before": true, "marker": "%s"}' % identity_marker(n)}
    live = {"id": "9", "displayName": n, "applicationId": "a"}
    assert identity_teardown_action(rec, live)[0] == "delete"                     # marker not readable: receipt proof
    assert identity_teardown_action(rec, {**live, "externalId": identity_marker(n)})[0] == "delete"
    for bad in ({**live, "externalId": "idp:123"}, {**live, "displayName": "renamed"}, {**live, "id": "10"},
                {**live, "applicationId": "b"}, {"id": "9", "displayName": n}, {"error": "HTTP 500"}):
        assert identity_teardown_action(rec, bad)[0] == "unresolved", bad
    assert identity_teardown_action(rec, None)[0] == "gone"
    assert identity_teardown_action({**rec, "proof": '{"marker": "x"}'}, live)[0] == "unresolved"

def test_runner_plan_resume_and_parameters():
    from runner import run_plan, pending_steps, job_parameters, latest_states
    plan = run_plan("demo")
    assert [s for s, _ in plan] == ["setup", "persona_clinician", "persona_pii_reader", "persona_outsider", "collect", "audit"]
    assert run_plan("audit") == [("audit", "lab_audit")]
    rows = [{"step": "setup", "state": "SUCCESS", "ts": 1}, {"step": "persona_clinician", "state": "FAILED", "ts": 2},
            {"step": "persona_clinician", "state": "SUCCESS", "ts": 3}, {"step": "persona_pii_reader", "state": "FAILED", "ts": 4}]
    done = {s for s, st in latest_states(rows).items() if st == "SUCCESS"}
    assert [s for s, _ in pending_steps(plan, done)] == ["persona_pii_reader", "persona_outsider", "collect", "audit"]
    assert job_parameters({"a", "b", "c"}, {"a": "1", "b": "", "c": None, "d": "x"}) == {"a": "1"}


def test_runner_results_summary_and_notes():
    from runner import summarize_results, restrict_admins_note, manual_steps, results_query
    rows = [{"test_id": "D-01", "persona": "clinician", "status": "PASS", "suite_run_id": "s1", "suite_mode": "demo"},
            {"test_id": "O-01", "persona": "admin", "status": "PENDING", "suite_run_id": "s1"},
            {"test_id": "G-03", "persona": "outsider", "status": "MISSING", "suite_run_id": "s1"}]
    s = summarize_results(rows)
    assert s["suite"] == "s1" and s["counts"] == {"MISSING": 1, "PASS": 1, "PENDING": 1}
    assert s["missing"] == ["G-03/outsider"] and s["pending"] == ["O-01/admin"] and s["mode"] == "demo"
    assert "supported" in restrict_admins_note("RESTRICT_TOKENS_AND_JOB_RUN_AS")
    assert "confirm manually" in restrict_admins_note(None)
    m = manual_steps(_cfg(), "someone")
    assert "labtest-clinician" in m and "labtest_all_facilities" in m and "manual_app_ids" in m
    q_ = results_query({"datasets": [{"queryLines": ["SELECT 1 AS layer, 'x' AS test_id, 'p' AS persona"]}]})
    assert q_.endswith("ORDER BY layer, test_id, persona")


def test_runner_retry_is_bounded():
    from runner import retry
    calls = []
    assert retry(lambda: calls.append(1) or len(calls), tries=5, ok=lambda v: v == 3, sleep=lambda s: None) == 3
    calls.clear()
    assert retry(lambda: calls.append(1) or 0, tries=4, ok=lambda v: v == 1, sleep=lambda s: None) == 0 and len(calls) == 4


# --------------------------------------------------------------------------------------------
# scripts/check_no_literal_pii.py (negative-test fixtures are marked and fictional)
# --------------------------------------------------------------------------------------------
def _pii_scanner():
    import importlib.util
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts", "check_no_literal_pii.py")
    spec = importlib.util.spec_from_file_location("check_no_literal_pii", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_pii_scanner_flags_literals():
    sc = _pii_scanner()
    fake_nric = "T" + "0000001" + "K"           # fictional (invalid check letter); assembled so no literal exists
    fake_phone = "5" + "5550001"            # reserved level 5: can't be dialled in Singapore
    text = f"a {fake_nric} b\ncall {fake_phone[:4]} {fake_phone[4:]}\nhello Zorblax Quux\nTest Patient 017\n"
    kinds = sorted(k for _, _, k, _ in sc.scan({"docs/x.md": text}, ["Zorblax Quux"]))
    assert kinds == ["name", "nric", "phone"], kinds
    assert sc.scan({"docs/x.md": "Test Patient 017, masked *****K, id 00000000"}, ["Zorblax"]) == []


def test_pii_scanner_fixture_exception_is_narrow():
    sc = _pii_scanner()
    fake_nric = "S" + "0000002" + "Q"           # fictional (invalid check letter)
    real_shape = "S" + "0000002" + "D"          # a valid-looking check letter: never allowed, even as a fixture
    ok_line = f'x = "{fake_nric}"  # pii-fixture'
    assert sc.scan({"src/lib/test_checks.py": ok_line}, ["Zorblax"]) == []
    assert sc.scan({"docs/x.md": ok_line}, ["Zorblax"])                                   # only in test_checks.py
    assert sc.scan({"src/lib/test_checks.py": f'x = "{real_shape}"  # pii-fixture'}, ["Zorblax"])
    assert sc.scan({"src/lib/test_checks.py": f'x = "{fake_nric}"'}, ["Zorblax"])          # unmarked
    assert sc.scan({"src/lib/test_checks.py": 'x = "Zorblax"  # pii-fixture'}, ["Zorblax"])  # names never exempt


def test_runner_export_files_keep_line_endings():
    import hashlib, tempfile
    from runner import _read_text_fuse, _write_text_fuse
    text = "a,b\r\n1,2\r\n"                     # csv.writer output
    with tempfile.TemporaryDirectory() as d:
        _write_text_fuse("/x/y.csv", text, root=d)
        back = _read_text_fuse("/x/y.csv", root=d)
    assert hashlib.sha256(back.encode()).hexdigest() == hashlib.sha256(text.encode()).hexdigest()


def test_genie_space_recorded_created_title():
    made_unique = LAB_GENIE_TITLE + " 2026-01-01 00:00:00"
    assert not is_lab_genie_space(made_unique, LAB_GENIE_DESCRIPTION)                       # no record: manual review
    assert is_lab_genie_space(made_unique, LAB_GENIE_DESCRIPTION, made_unique)              # exactly what was recorded
    assert not is_lab_genie_space(made_unique + "x", LAB_GENIE_DESCRIPTION, made_unique)    # renamed since
    assert not is_lab_genie_space(made_unique, LAB_GENIE_DESCRIPTION + " edited", made_unique)
    assert not is_lab_genie_space("Someone else's space", LAB_GENIE_DESCRIPTION, "Someone else's space")


def test_pii_scanner_case_punctuation_and_wrapped_names():
    sc = _pii_scanner()
    low = "t" + "0000003" + "k"                     # lower-case NRIC shape
    tok = ["Zorblax Quux"]
    for text in (f"id {low} here", "call +65 " + "555" + "0 0001", "call (+65) 5550" + "-0002",
                 "call 65-" + "555" + "0.0003", "tel 5" + "555.0004"):
        assert [k for _, _, k, _ in sc.scan({"docs/x.md": text}, tok)], text
    for level in "3689":                            # every allocated SG level is covered (all-zero dummy digits)
        assert sc.PHONE.fullmatch(level + "000" + "0000") and sc.PHONE.fullmatch(level + "000 " + "0000"), level
    assert not sc.PHONE.fullmatch("7" + "000" + "0000")       # levels outside the pattern are not phone numbers
    wrapped = "patient zorblax\n  QUUX was seen"           # case-insensitive, split across a line break
    hits = sc.scan({"docs/x.md": wrapped}, tok)
    assert [(n, k) for _, n, k, _ in hits] == [(1, "name")]
    assert sc.scan({"docs/x.md": "uuid 00000000-0000-0000-0000-000000000001 and 2026-11-26"}, tok) == []
    fixture_low = f'x = "{low}"  # pii-fixture'             # lower case is never an accepted fixture
    assert sc.scan({"src/lib/test_checks.py": fixture_low}, tok)


def test_pii_scanner_staged_mode(tmp=None):
    import subprocess, tempfile, shutil
    sc = _pii_scanner()
    d = tempfile.mkdtemp()
    try:
        subprocess.run(["git", "init", "-q", d], check=True)
        with open(os.path.join(d, "a.md"), "w") as f:
            f.write("clean text\n")
        subprocess.run(["git", "-C", d, "add", "a.md"], check=True)
        with open(os.path.join(d, "a.md"), "w") as f:          # working tree differs from the index
            f.write("S" + "0000004" + "D\n")
        old = sc.ROOT
        sc.ROOT = d
        try:
            staged = sc.staged_files()
        finally:
            sc.ROOT = old
        assert staged == {"a.md": "clean text\n"}                # the STAGED content is what gets scanned
        assert sc.scan(staged, ["Zorblax"]) == []
    finally:
        shutil.rmtree(d)



# --------------------------------------------------------------------------------------------
# scripts/check_release.py. Every ID-shaped or e-mail value is assembled at runtime, so this file holds no literal.
# --------------------------------------------------------------------------------------------
BAD32 = "1234abcd" * 4
BAD_UUID = "-".join(["1234abcd", "1234", "4abc", "8abc", "1234abcd5678"])


class _ReleaseRepo:
    """A throw-away git repo plus helpers to commit and run scripts/check_release.py against it."""
    SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts", "check_release.py")
    OK_EMAIL = "1+simtsc-db@users.noreply.github.com"

    def __init__(self):
        import tempfile
        self.d = tempfile.mkdtemp()
        self.env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        self.git("init", "-q")

    def git(self, *a, name="simtsc-db", email=OK_EMAIL):
        import subprocess
        return subprocess.run(["git", "-C", self.d, "-c", f"user.name={name}", "-c", f"user.email={email}",
                               "-c", "commit.gpgsign=false", *a], check=True, capture_output=True, env=self.env)

    def write(self, path, data):
        full = os.path.join(self.d, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb" if isinstance(data, bytes) else "w") as f:
            f.write(data)

    def commit(self, msg="change", **who):
        msg_file = os.path.join(self.d, ".git", "MSG")              # -F: a message of any size, outside the tree
        with open(msg_file, "w") as f:
            f.write(msg)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "--cleanup=verbatim", "-F", msg_file, **who)

    def run(self, *a):
        import subprocess
        return subprocess.run([sys.executable, "-I", self.SCRIPT, "--root", self.d, *a], capture_output=True,
                              text=True, env=self.env)

    def close(self):
        import shutil
        shutil.rmtree(self.d)


def _release(fn):
    r = _ReleaseRepo()
    try:
        r.write("a.md", "clean\n")
        r.commit("clean")
        fn(r)
    finally:
        r.close()


def test_release_check_scans_the_committed_tree_not_the_working_copy():
    def t(r):
        assert r.run().returncode == 0 and r.run("--range", "HEAD").returncode == 0
        r.write("b.md", f"id {BAD32}\n"); r.commit("add an id")
        r.write("b.md", "removed again, but only in the working copy\n")
        head = r.run()
        assert head.returncode == 1 and BAD32 in head.stdout and "WARNING" in head.stderr, head.stdout + head.stderr
        assert r.run("--range", "HEAD").returncode == 1                 # every commit's tree
        assert r.run("--worktree").returncode == 0                       # pre-commit mode sees the edit
        r.write("b.md", "clean\n"); r.commit("remove it")
        assert r.run().returncode == 0                                   # HEAD is clean now ...
        assert r.run("--range", "HEAD").returncode == 1                  # ... but the history still has it
        assert r.run("--range", "HEAD~1..HEAD").returncode == 0
        assert r.run("--range", "HEAD..HEAD").returncode == 2           # empty range is an error, never a pass
        assert r.run("--range", "no-such-rev").returncode == 2
    _release(t)


def test_release_check_id_patterns_and_mail():
    for bad in (BAD32, BAD_UUID, "x_" + BAD32 + "_y", "1234567" * 2 + "891", "01234567" + "89abcdef",
                "1234567" * 2 + "8", "someone" + "@" + "Databricks.com"):
        def t(r, bad=bad):
            r.write("b.md", f"x {bad} y\n"); r.commit()
            res = r.run()
            assert res.returncode == 1, (bad, res.stdout)
            assert r.run("--worktree").returncode == 1, bad
        _release(t)
    def ok(r):                                                           # short hex and 14 digits are not IDs
        r.write("b.md", "x 0123abcd " + "1234567" * 2 + " 2026-11-26 y\n"); r.commit()
        assert r.run().returncode == 0
    _release(ok)


def test_release_check_allowlist_is_exact_and_path_scoped():
    dummy = "0" * 31 + "1"
    def t(r):
        r.write("scripts/release_allowlist.txt", f"id docs/x.md {dummy}\n")
        r.write("docs/x.md", f"ok {dummy}\n"); r.commit()
        assert r.run().returncode == 0, r.run().stdout                  # listed value, listed path (and the list itself)
        r.write("docs/y.md", f"same value, other file {dummy}\n"); r.commit()
        assert r.run().returncode == 1
        r.write("docs/y.md", "other zero-padded value " + "0" * 31 + "2\n"); r.commit()
        assert r.run().returncode == 1
        os.remove(os.path.join(r.d, "docs", "y.md")); r.commit()
        r.write("scripts/release_allowlist.txt", "nonsense line\n"); r.commit()
        assert r.run().returncode == 2                                   # malformed allowlist is an error
    _release(t)


def test_release_check_paths_names_binaries_and_symlinks():
    def names(r):
        odd = 'we "ird"\n\u540d file.md'                              # quote, newline, non-ASCII: needs -z
        r.write(odd, f"x {BAD32}\n"); r.commit()
        res = r.run()
        assert res.returncode == 1 and "\u540d" in res.stdout, res.stdout
        assert r.run("--worktree").returncode == 1
    _release(names)
    def in_name(r):
        r.write(f"report_{BAD32}.md", "clean\n"); r.commit()
        res = r.run()
        assert res.returncode == 1 and "IN FILE NAME" in res.stdout, res.stdout
    _release(in_name)
    def binary(r):
        r.write("img.bin", b"\x89PNG\x00\x01"); r.commit()
        res = r.run()
        assert res.returncode == 1 and "BINARY" in res.stdout, res.stdout
        assert r.run("--worktree").returncode == 1
        r.write("scripts/release_allowlist.txt", "binary img.bin\n"); r.commit()
        assert r.run().returncode == 0 and r.run("--worktree").returncode == 0
    _release(binary)
    def link(r):
        os.symlink("a.md", os.path.join(r.d, "link.md")); r.commit()
        res = r.run()
        assert res.returncode == 1 and "SYMLINK" in res.stdout, res.stdout
        assert r.run("--worktree").returncode == 1
        r.write("scripts/release_allowlist.txt", "symlink link.md\n"); r.commit()
        assert r.run().returncode == 0 and r.run("--worktree").returncode == 0
    _release(link)


def test_release_check_commit_metadata():
    mail = "a.person" + "@" + "example.com"
    bad_msgs = ["msg\n\nCo-authored-by: X <" + mail + ">", "msg\n\nSigned-off-by: X <" + mail + ">",
                "msg\n\nReviewed-by: Someone", "msg\n\nHelped-by: Someone", "msg\n\ncontact " + mail + " for details",
                "msg\n\nCo-authored-by: X <" + mail + ">\n" + "filler line\n" * 300000]   # SIGPIPE-sized message
    for m in bad_msgs:
        def t(r, m=m):
            r.commit(m)
            res = r.run("--range", "HEAD~1..HEAD")
            assert res.returncode == 1 and "COMMIT" in res.stdout, (m[:60], res.stdout)
        _release(t)
    def who(r):
        r.commit("other name", name="someone")
        res = r.run("--range", "HEAD~1..HEAD")
        assert res.returncode == 1 and "name not allowed: someone" in res.stdout, res.stdout
        assert r.run("--range", "HEAD~1..HEAD", "--allow-name", "someone").returncode == 0
        r.commit("other mail", email=mail)
        res = r.run("--range", "HEAD~1..HEAD")
        assert res.returncode == 1 and "e-mail not allowed" in res.stdout, res.stdout
        assert r.run("--range", "HEAD~2..HEAD~1", "--allow-name", "someone").returncode == 0
    _release(who)
    def clean(r):
        r.commit("Subject only\n\nA plain body without people or addresses.")
        assert r.run("--range", "HEAD").returncode == 0
    _release(clean)


# --------------------------------------------------------------------------------------------
# Phone numbers: one pattern for runtime detection, the masked-notes SQL fallback and the literal-PII scanner
# --------------------------------------------------------------------------------------------
def _phone_variants():
    n = "5555" + "0001"                       # reserved level 5, assembled so no literal exists
    a, b = n[:4], n[4:]
    return [n, f"{a} {b}", f"{a}.{b}", f"{a}-{b}", f"+65 {a} {b}", f"+65{n}", f"65-{a}.{b}", f"(+65) {a}-{b}",
            f"(65){a}{b}", f"65{n}"]


def test_runtime_phone_detection_matches_the_scanner():
    from checks import PHONE_PATTERN, PHONE_RE, pii_leaks
    sc = _pii_scanner()
    assert PHONE_PATTERN == sc.PHONE.pattern                       # same coverage at runtime and in the scanner
    for v in _phone_variants():
        text = f"Patient [NAME] tel {v}, call back."
        assert pii_leaks(text), v
        assert [m.group(0) for m in sc.PHONE.finditer(text)] == [m.group(0) for m in PHONE_RE.finditer(text)], v
    for not_phone in ("7" + "000" + "0000", "5555" + "000", "5555" + "00011", "1-" + "5555" + "0001",
                      "5555" + "0001-2", "x" + "5555" + "0001", "5555" + "0001x"):
        assert not PHONE_RE.search(not_phone), not_phone


def test_d06_d07_mask_verdict_catches_punctuated_phone_numbers():
    from checks import mask_verdict
    for v in _phone_variants():
        st, lk = mask_verdict(f"Patient [NAME] (NRIC [NRIC], tel {v}) seen at Polyclinic North.")
        assert st == "FAIL" and lk, v
    assert mask_verdict("Patient [NAME] (NRIC [NRIC], tel [PHONE]) seen at Polyclinic North.") == ("PASS", [])
    assert mask_verdict("")[0] == "FAIL" and mask_verdict(None)[0] == "FAIL"


def test_masked_notes_check_and_sql_fallback_cover_punctuated_phone_numbers():
    import re
    from checks import note_issue, PHONE_SQL, pii_leaks, synthetic_sentence
    assert "\\" not in PHONE_SQL and "'" not in PHONE_SQL          # embeds unchanged in a SQL string literal
    sql = re.compile(PHONE_SQL)                                     # this subset means the same in Java and Python
    for v in _phone_variants():
        note = f"Seen today; contact next of kin on {v} if needed."
        assert note_issue(7, note) == {"visit_id": 7, "hits": 1, "empty": False}, v
        masked = sql.sub("[PHONE]", note)
        assert "[PHONE]" in masked and not pii_leaks(masked) and note_issue(7, masked) is None, (v, masked)
    assert note_issue(7, "  ") == {"visit_id": 7, "hits": 0, "empty": True}
    from checks import PHONE_RE
    assert PHONE_RE.search(synthetic_sentence()) and not PHONE_RE.search(sql.sub("[PHONE]", synthetic_sentence()))


def test_pii_fixture_country_code_only_when_present():
    sc = _pii_scanner()
    n = "5555" + "0001"
    for v in (n, "+65 " + n[:4] + " " + n[4:], "(+65) " + n[:4] + "-" + n[4:], "65" + n):
        assert sc.fictional_fixture("phone", v), v
    bare = "65" + "51" + "2345"                                    # a bare level-6 number, not 65 + six digits
    assert not sc.fictional_fixture("phone", bare)
    assert sc.scan({"src/lib/test_checks.py": f'x = "{bare}"  # pii-fixture'}, ["Zorblax"])
    assert not sc.fictional_fixture("phone", "6" + "555" + "0001")
    assert sc.scan({"src/lib/test_checks.py": f'x = "{n}"  # pii-fixture'}, ["Zorblax"]) == []


# Boundary cases every phone path must agree on: (text, must be caught)
def _phone_boundary_cases():
    n = "5555" + "0001"
    return [(n + "-no answer", True), ("no-answer-" + n, True), (n + "_x", True), ("tel " + n + ".", True),
            ("(" + n + ")", True), ("+65 " + n[:4] + "-" + n[4:] + "-call back", True),
            ("1-" + n, False), (n + "-2", False), ("x" + n, False), (n + "x", False), ("+1" + n, False),
            ("1." + n, False)]


def _java_find(pattern, texts):
    """[bool] from java.util.regex (Spark's regex engine) via the JDK's jjs, or None if no JVM with jjs is present."""
    import glob, shutil, subprocess, tempfile
    jjs = shutil.which("jjs") or next(iter(glob.glob(
        "/Library/Internet Plug-Ins/JavaAppletPlugin.plugin/Contents/Home/bin/jjs")), None)
    if not jjs:
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write("var p = java.util.regex.Pattern.compile(arguments[0]);\n"
                "for (var i = 1; i < arguments.length; i++) print(p.matcher(arguments[i]).find());\n")
    try:
        out = subprocess.run([jjs, f.name, "--", pattern, *texts], capture_output=True, text=True, timeout=60)
    finally:
        os.remove(f.name)
    if out.returncode != 0:
        return None
    return [line.strip() == "true" for line in out.stdout.splitlines() if line.strip() in ("true", "false")]


def test_phone_boundary_is_consistent_across_runtime_sql_and_scanner():
    import re
    from checks import PHONE_RE, PHONE_SQL, pii_leaks, note_issue
    sc = _pii_scanner()
    sql = re.compile(PHONE_SQL)
    cases = _phone_boundary_cases()
    for text, caught in cases:
        assert bool(PHONE_RE.search(text)) == caught, ("runtime", text)
        assert bool(pii_leaks(text)) == caught and (note_issue(1, text) is not None) == caught, ("checks", text)
        assert bool(sc.PHONE.search(text)) == caught, ("scanner", text)
        assert bool([h for h in sc.scan({"docs/x.md": text}, ["Zorblax"]) if h[2] == "phone"]) == caught, text
        assert bool(sql.search(text)) == caught, ("sql (python)", text)
        if caught:
            assert not PHONE_RE.search(sql.sub("[PHONE]", text)), ("sql fallback leaves it", text)
    java = _java_find(PHONE_SQL, [t for t, _ in cases])
    if java is not None:                                   # Spark's engine, where a JVM is available
        assert java == [c for _, c in cases], list(zip([t for t, _ in cases], java))


def test_mask_verdict_blank_output_fails_like_the_masked_notes_check():
    from checks import mask_verdict, note_issue
    for blank in ("", " ", "\n\t  ", None):
        assert mask_verdict(blank)[0] == "FAIL", repr(blank)
        assert note_issue(1, blank) is not None, repr(blank)
    assert mask_verdict("Patient [NAME] tel [PHONE].") == ("PASS", [])


def test_release_check_range_argument_errors():
    def t(r):
        for args in (("--range", ""), ("--range", "   "), ("--range", "HEAD", "--worktree"),
                     ("--worktree", "--range", "HEAD"), ("--worktree", "--range", ""), ("--range",)):
            res = r.run(*args)
            assert res.returncode == 2, (args, res.stdout, res.stderr)
        assert r.run("--range", "HEAD").returncode == 0 and r.run("--worktree").returncode == 0
    _release(t)


def test_release_check_allowlist_comes_from_the_scanned_commit():
    dummy = "0" * 31 + "1"
    def uncommitted(r):
        r.write("docs/x.md", f"v {dummy}\n"); r.commit("add value, no exception")
        r.write("scripts/release_allowlist.txt", f"id docs/x.md {dummy}\n")         # exception NOT committed
        assert r.run().returncode == 1 and r.run("--range", "HEAD").returncode == 1
        assert r.run("--worktree").returncode == 0                                  # pre-commit: working-copy list
        override = os.path.join(r.d, ".git", "override.txt")                        # explicit override flag only
        with open(override, "w") as f:
            f.write(f"id docs/x.md {dummy}\n")
        assert r.run("--allowlist", override).returncode == 0
        assert r.run("--allowlist", override + ".missing").returncode == 2
    _release(uncommitted)
    def later(r):
        r.write("docs/x.md", f"v {dummy}\n"); r.commit("value without exception")
        r.write("scripts/release_allowlist.txt", f"id docs/x.md {dummy}\n"); r.commit("exception added later")
        assert r.run().returncode == 0                                              # HEAD has both: fine
        assert r.run("--range", "HEAD~1..HEAD").returncode == 0
        res = r.run("--range", "HEAD")                                              # the earlier commit still fails
        assert res.returncode == 1 and dummy in res.stdout, res.stdout
    _release(later)
    def together(r):
        r.write("docs/x.md", f"v {dummy}\n"); r.write("scripts/release_allowlist.txt", f"id docs/x.md {dummy}\n")
        r.commit("value and exception together")
        assert r.run("--range", "HEAD").returncode == 0
    _release(together)


def test_release_check_rejects_every_trailer_unless_permitted():
    for trailer in ("Author: X", "Reviewer: X", "Ticket: 42", "Change-Id: I0", "Reviewed-by: Someone"):
        def t(r, trailer=trailer):
            r.commit(f"msg\n\nBody text.\n\n{trailer}\n")
            res = r.run("--range", "HEAD~1..HEAD")
            assert res.returncode == 1 and "trailer not permitted" in res.stdout, (trailer, res.stdout)
            key = trailer.split(":")[0]
            assert r.run("--range", "HEAD~1..HEAD", "--allow-trailer", key.lower()).returncode == 0, trailer
        _release(t)
    def never(r):                     # a permitted key never lifts the person / e-mail rules
        r.commit("msg\n\nCo-authored-by: X <x" + "@" + "example.com>\n")
        assert r.run("--range", "HEAD~1..HEAD", "--allow-trailer", "Co-authored-by").returncode == 1
    _release(never)
    def prose(r):                     # a colon in ordinary prose is not a trailer
        r.commit("msg\n\nNote: this paragraph explains the change in prose, over a longer sentence.\nMore prose.\n")
        res = r.run("--range", "HEAD~1..HEAD")
        assert res.returncode == 0, res.stdout
    _release(prose)

if __name__ == "__main__":
    from test_runner import *  # noqa: F401,F403,E402 - runner-level tests with fakes (src/lib/test_runner.py)
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t(); print("ok  ", t.__name__)
        except Exception as e:  # noqa: BLE001 - report every failing test, not just assertion errors
            failed += 1; print("FAIL", t.__name__, type(e).__name__, str(e)[:200])
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
