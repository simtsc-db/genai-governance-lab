"""Workspace-native runner for the GenAI governance lab (src/notebooks/00_run_lab.py).

The bundle is deployed once (Git folder > Deploy, or the CLI); this module drives everything else from one notebook:
preflight, personas, run / resume, results, evidence, teardown-plan, teardown-apply. It uses the same validation and
naming as the job notebooks and the CLI scripts (checks.py).

Rule: when ownership or state can't be proven, fail closed (stop, record unresolved, or list for manual review).

* Every input is validated with the strict patterns in checks.py before it reaches SQL, a URL or an API body.
* The deployment is identified from its own bundle state (state/resources.json or terraform.tfstate): the job
  <prefix>_audit, the lab schema catalog.schema and the IDs the bundle recorded. The requested mode must match it.
* Identity store: two tables in the lab schema that THIS runner created (a creation record with the tables' UC IDs
  is kept in the deployment folder; a table that exists without it is never trusted). It is checked for effective,
  including inherited, write privileges before use. Receipts (identities the runner created, with creation proof)
  are kept apart from bindings (which identities the personas use now; created or provided).
  Records are bound to account ID, workspace ID, bundle target, deployment root and the lab names.
* Teardown deletes only what a receipt proves: an identity only if its live ID, displayName and (for service
  principals) applicationId all match; an ACL entry only if it is still exactly the entry recorded. "Gone" means a
  structured not-found (SDK NotFound / SCIM_404 / *_DOES_NOT_EXIST), never error text.
* Deployment generation: each deployment folder gets a generation ID (runner/generation.json, sealed, bound to the
  workspace, target, root, lab names and the deployed audit job) the first time personas, run / resume (both modes)
  or teardown-apply use it; personas and run note in it that they started. Teardown skips the export only with
  positive evidence that no suite ran (generation record and step log), else it stops.
* Audit-only deployments: the runner records the audit results tables' absence before the first run and their UC
  table IDs after it (generation record); teardown drops only exact ID matches, else manual review and stop.
  The identity store record, every receipt and every teardown checkpoint carry it. A redeploy after teardown is a new
  generation, so nothing recorded for an earlier one (in particular a 'complete' checkpoint) is ever reused for it.
* Teardown is resumable: a sealed checkpoint (sha256) with the generation, the verified export, the identity receipts
  and the phase states is kept in the deployment folder and in the caller's home folder (named by generation).
  Schemas are dropped only when empty.
* Persona jobs run as the persona service principals: run_as is set on the deployed jobs after the personas exist and
  re-asserted before every run (a redeploy resets it). A persona job that runs as anyone else fails; the runner
  records the SKIPs and stops, and resume reruns that step and everything after it.

Pure helpers are unit-tested in test_checks.py / test_runner.py; everything that calls Databricks takes the
WorkspaceClient, the table adapter and the file reader/writer as arguments, so it can be faked.
"""
from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import io
import json
import os
import posixpath
import re
import time
import uuid
from dataclasses import dataclass, field

from checks import (DEFAULT_PREFIX, GROUP_ROLES, PERSONA_GROUPS, PERSONAS, RESULTS_COLS, app_id, first_line,
                    identity_marker, is_marked, is_not_found, lab_names, load_catalogue, name, persona_identities,
                    prefix_ok, principal, q, run_id_ok, valid_lookback_days)

ACTIONS = ("preflight", "personas", "run", "resume", "results", "evidence", "teardown-plan", "teardown-apply")
MODES = ("demo", "audit")
_ACCOUNT_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
ACCOUNT_SCIM = "/api/2.0/account/scim/v2"          # account SCIM through the workspace-domain proxy
UC = "/api/2.1/unity-catalog"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
SP_USER_ROLE = "roles/servicePrincipal.user"
EXPORT_DIR = "genai_governance_lab_exports"         # in the caller's workspace home folder
RUNNER_DIR = "runner"                              # under the deployment root: store record + teardown checkpoint
RECEIPTS_T, BINDINGS_T, STEPS_T = "lab_identity_receipts", "lab_persona_bindings", "lab_runner_steps"
STORE_PROP, BINDING_PROP, KIND_PROP = "genai_governance_lab.store_id", "genai_governance_lab.binding_sha", \
    "genai_governance_lab.runner_table"
WRITE_PRIVS = {"MODIFY", "ALL_PRIVILEGES", "MANAGE"}
GUARD_MARK = "LAB_IDENTITY_GUARD"                  # 30_persona_probe raises with this when the identity is wrong
# Tables the audit job (20_audit_settings, 25_platform_probe via checks.register_suite / Recorder) creates in the lab
# schema. In demo mode lab_teardown drops them from lab_manifest; an audit-only deployment has no teardown job, so the
# runner records their absence before its first run and their UC table IDs after it, and drops only exact matches.
AUDIT_RESULT_TABLES = ("suite_runs", "test_catalogue", "test_results", "lab_setup_status")
GEN_FILE = "generation.json"                       # under <root>/runner: the deployment generation record
GEN_RE = re.compile(r"^gen-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")

# (widget, default, label). No default points at a specific workspace, catalog, warehouse or identity.
WIDGETS = [
    ("action", "preflight", "action"),
    ("mode", "demo", "mode (demo | audit)"),
    ("catalog", "", "catalog"),
    ("schema", "genie_governance_lab", "schema"),
    ("resource_prefix", DEFAULT_PREFIX, "resource_prefix"),
    ("persona_prefix", DEFAULT_PREFIX, "persona_prefix"),
    ("account_id", "", "account_id (demo)"),
    ("warehouse_id", "", "warehouse_id"),
    ("vs_endpoint", "", "vs_endpoint (optional)"),
    ("agent_endpoint", "", "agent_endpoint (optional)"),
    ("ka_endpoint", "", "ka_endpoint (optional)"),
    ("expected_policy", "", "expected_policy (G-02)"),
    ("approved_builders", "", "approved_builders"),
    ("cleartext_groups", "", "cleartext_groups"),
    ("usage_lookback_days", "30", "usage_lookback_days"),
    ("run_id", "", "run_id (resume)"),
    ("manual_app_ids", "", "manual: app IDs clinician,pii_reader,outsider"),
    ("manual_groups", "", "manual: groups clinicians,pii_readers,all_facilities,outsiders"),
]

# Bounded waits (seconds) per job; persona jobs include two 65 s rate-limit pauses.
JOB_TIMEOUT = {"lab_setup": 3600, "lab_persona_clinician": 2700, "lab_persona_pii_reader": 2700,
               "lab_persona_outsider": 2700, "lab_collect": 1200, "lab_audit": 2700, "lab_evidence": 1800,
               "lab_teardown": 2700}
POLL_S = 20


class ConfigError(ValueError):
    pass


class Refused(Exception):
    """An identity step was refused (permission or policy): use the manual fallback."""

    def __init__(self, step: str, error: str):
        super().__init__(f"{step}: {error}")
        self.step, self.error = step, error


class Stop(Exception):
    """A clear, expected stop condition (message is shown to the user)."""


class NoDeployment(Stop):
    """No bundle deployment matches (as opposed to an ambiguous or inconsistent one): only then may teardown resume
    from a home-folder checkpoint."""


# --------------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Config:
    action: str
    mode: str
    catalog: str
    schema: str
    resource_prefix: str
    persona_prefix: str
    account_id: str = ""
    warehouse_id: str = ""
    vs_endpoint: str = ""
    agent_endpoint: str = ""
    ka_endpoint: str = ""
    expected_policy: str = ""
    approved_builders: tuple = ()
    cleartext_groups: tuple = ()
    usage_lookback_days: int = 30
    run_id: str = ""
    manual_app_ids: tuple = ()
    manual_groups: tuple = ()

    @property
    def full_schema(self) -> str:
        return f"{self.catalog}.{self.schema}"

    @property
    def tools_schema(self) -> str:
        return f"{self.catalog}.{self.schema}_tools"


def _csv(v: str) -> list:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def _fail(msg):
    raise ValueError(msg)


def parse_config(raw: dict) -> Config:
    """Validate every widget; raises ConfigError listing every problem at once."""
    g = {k: str(raw.get(k, d) if raw.get(k) is not None else d).strip() for k, d, _ in WIDGETS}
    errors, out = [], {}

    def check(key, fn):
        try:
            out[key] = fn(g[key])
        except ValueError as e:
            errors.append(f"{key}: {e}")

    check("action", lambda v: v if v in ACTIONS else _fail(f"one of {', '.join(ACTIONS)}"))
    check("mode", lambda v: v if v in MODES else _fail("demo or audit"))
    check("catalog", lambda v: name(v, "catalog") if v else _fail("required"))
    check("schema", lambda v: name(v, "schema"))
    check("resource_prefix", lambda v: prefix_ok(v, "resource_prefix"))
    check("persona_prefix", lambda v: prefix_ok(v, "persona_prefix"))
    check("account_id", lambda v: v if not v or _ACCOUNT_RE.fullmatch(v) else
          _fail("the account ID is a lowercase UUID (Account console > your profile menu)"))
    for k in ("warehouse_id", "vs_endpoint", "agent_endpoint", "ka_endpoint", "expected_policy"):
        check(k, lambda v, k=k: name(v, k) if v else "")
    check("approved_builders", lambda v: tuple(principal(x, "approved builder") for x in _csv(v)))
    check("cleartext_groups", lambda v: tuple(principal(x, "cleartext group") for x in _csv(v)))
    check("usage_lookback_days", valid_lookback_days)
    check("run_id", lambda v: run_id_ok(v) if v else "")
    check("manual_app_ids", lambda v: tuple(app_id(x).lower() for x in _csv(v)))
    check("manual_groups", lambda v: tuple(principal(x, "group") for x in _csv(v)))
    if not errors:
        a = out["action"]
        if a == "resume" and not out["run_id"]:
            errors.append("run_id: required for action=resume (the suite run ID printed by action=run)")
        if a in ("run", "resume", "evidence") and not out["warehouse_id"]:
            errors.append("warehouse_id: required for this action (serverless SQL warehouse ID)")
        if out["mode"] == "demo" and a in ("personas", "run", "resume", "evidence", "teardown-plan", "teardown-apply") \
                and not out["account_id"]:
            errors.append("account_id: required in demo mode (identity records are bound to the account)")
        errors += manual_identity_errors(out["manual_app_ids"], out["manual_groups"])
    if errors:
        raise ConfigError("invalid widgets:\n  " + "\n  ".join(errors))
    return Config(**out)


def manual_identity_errors(apps: tuple, groups: tuple) -> list:
    """Problems with the manual fallback widgets: counts, both-or-neither, and distinct values."""
    errs = []
    if apps and len(apps) != len(PERSONAS):
        errs.append("manual_app_ids: three application IDs: clinician,pii_reader,outsider")
    if groups and len(groups) != len(GROUP_ROLES):
        errs.append("manual_groups: four account groups: clinicians,pii_readers,all_facilities,outsiders")
    if bool(apps) != bool(groups):
        errs.append("manual_app_ids / manual_groups: give both, or neither")
    if len({a.lower() for a in apps}) != len(apps):
        errs.append("manual_app_ids: the three personas must be three different service principals")
    if len({g.lower() for g in groups}) != len(groups):
        errs.append("manual_groups: the four groups must be different groups")
    return errs


# --------------------------------------------------------------------------------------------
# Deployment discovery (from the bundle's own state)
# --------------------------------------------------------------------------------------------
@dataclass
class Deployment:
    root: str
    state_dir: str
    target: str
    jobs: dict = field(default_factory=dict)            # bundle resource key -> job ID
    dashboards: dict = field(default_factory=dict)      # key -> dashboard ID
    model_services: dict = field(default_factory=dict)  # key -> catalog.schema.name
    schemas: dict = field(default_factory=dict)         # key -> catalog.schema
    source_root: str | None = None                      # folder holding src/ (Git folder or bundle files)
    audit_mode: str | None = None                       # mode the deployed lab_audit job was given

    @property
    def mode(self) -> str:
        return "demo" if "lab_setup" in self.jobs else "audit"

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("root", "state_dir", "target", "jobs", "dashboards", "model_services",
                                               "schemas", "source_root", "audit_mode")}


def parse_bundle_state(resources: dict | None = None, tfstate: dict | None = None) -> dict:
    """Resource IDs a bundle deployment recorded: {'jobs': {key: id}, 'dashboards', 'model_services', 'schemas'}.
    resources: state/resources.json (direct engine); tfstate: state/terraform.tfstate (terraform engine)."""
    out = {"jobs": {}, "dashboards": {}, "model_services": {}, "schemas": {}}
    if resources:
        for k, v in ((resources or {}).get("state") or {}).items():
            parts = k.split(".")
            if len(parts) == 3 and parts[0] == "resources" and parts[1] in out and isinstance(v, dict) and v.get("__id__"):
                out[parts[1]][parts[2]] = str(v["__id__"])
    elif tfstate:
        kinds = {"databricks_job": "jobs", "databricks_dashboard": "dashboards", "databricks_schema": "schemas"}
        for r in tfstate.get("resources", []) or []:
            kind = kinds.get(r.get("type"))
            if kind and r.get("mode") == "managed":
                for inst in r.get("instances", []) or []:
                    rid = (inst.get("attributes") or {}).get("id")
                    if rid:
                        out[kind][r.get("name")] = str(rid)
    return out


def source_root_of(notebook_path: str | None) -> str | None:
    """'/Workspace/.../src/notebooks/20_audit_settings' -> '/Workspace/...' (the folder that holds src/)."""
    p = str(notebook_path or "")
    i = p.rfind("/src/notebooks/")
    if i <= 0:
        return None
    root = p[:i]
    return root if root.startswith("/Workspace") else "/Workspace" + root


def match_deployment(cfg: Config, job_id, state: dict) -> str | None:
    """None if this bundle state is the lab deployment for cfg (its audit job is job_id and its lab schema is
    catalog.schema), else the reason it isn't."""
    if state["jobs"].get("lab_audit") != str(job_id):
        return "its bundle state doesn't record this job as lab_audit"
    if state["schemas"].get("lab") != cfg.full_schema:
        return f"deployed for schema {state['schemas'].get('lab') or '(none)'}, not {cfg.full_schema}"
    return None


def check_mode(action: str, requested: str, dep: Deployment):
    """The requested mode must be the deployment's mode (as the CLI path insists): audit only on an audit-only
    deployment, demo only on a demo deployment. Teardown always follows the deployment's actual mode."""
    if dep.audit_mode and dep.audit_mode != dep.mode:
        raise Stop(f"inconsistent deployment at {dep.root}: its jobs are {dep.mode}-mode but its audit job was "
                   f"deployed with mode={dep.audit_mode}; redeploy the bundle")
    if action.startswith("teardown"):
        return
    if requested != dep.mode:
        raise Stop(f"mode={requested}, but the deployment at {dep.root} (target {dep.target}) is a {dep.mode}-mode "
                   f"deployment. Use mode={dep.mode}, or deploy a {requested}-mode target.")


def _kind(dep) -> str:
    k = getattr(dep, "kind", None)
    return str(getattr(k, "value", k) or "")


def find_deployment(w, cfg: Config, read_text) -> Deployment:
    """The single bundle deployment of this lab for cfg (catalog.schema, resource_prefix)."""
    audit = lab_names(cfg.resource_prefix)["jobs"]["lab_audit"]
    matches, notes = [], []
    for j in w.jobs.list(name=audit):
        full = w.jobs.get(j.job_id)
        dep = getattr(full.settings, "deployment", None)
        path = getattr(dep, "metadata_file_path", None)
        if _kind(dep) != "BUNDLE" or not path:
            notes.append(f"job {j.job_id}: not deployed by a bundle"); continue
        state_dir = posixpath.dirname(path)
        res = tf = None
        try:
            res = json.loads(read_text(state_dir + "/resources.json"))
        except Exception:  # noqa: BLE001 - try the other engine's state
            try:
                tf = json.loads(read_text(state_dir + "/terraform.tfstate"))
            except Exception as e:  # noqa: BLE001
                notes.append(f"job {j.job_id}: bundle state not readable ({first_line(e, 120)})"); continue
        st = parse_bundle_state(res, tf)
        why = match_deployment(cfg, j.job_id, st)
        if why:
            notes.append(f"job {j.job_id}: {why}"); continue
        try:
            target = (json.loads(read_text(path)).get("config") or {}).get("bundle", {}).get("target", "")
        except Exception:  # noqa: BLE001
            target = ""
        tasks = full.settings.tasks or []
        settings_task = next((t for t in tasks if t.task_key == "settings"), tasks[0] if tasks else None)
        nb = settings_task.notebook_task if settings_task else None
        matches.append(Deployment(root=posixpath.dirname(state_dir), state_dir=state_dir, target=target,
                                  source_root=source_root_of(nb.notebook_path if nb else None),
                                  audit_mode=((nb.base_parameters or {}).get("mode") if nb else None), **st))
    if not matches:
        raise NoDeployment(f"No bundle deployment of the lab found for {cfg.full_schema} with resource_prefix "
                   f"'{cfg.resource_prefix}' (looked for the job '{audit}'). Deploy the bundle first (Git folder > "
                   "Deploy, docs/workspace-setup.md) with the same catalog, schema and resource_prefix."
                   + ("\n  " + "\n  ".join(notes) if notes else ""))
    if len(matches) > 1:
        raise Stop(f"{len(matches)} deployments match {cfg.full_schema} / {cfg.resource_prefix}: "
                   + ", ".join(m.root for m in matches) + ". Remove the stale one first.")
    return matches[0]


def persona_jobs(w, dep: Deployment) -> dict:
    """{persona: job_id}, after checking each deployed persona job probes exactly that persona."""
    out = {}
    for p in PERSONAS:
        jid = dep.jobs.get(f"lab_persona_{p}")
        if not jid:
            raise Stop(f"the deployment has no lab_persona_{p} job (audit-only deployment?)")
        tasks = w.jobs.get(int(jid)).settings.tasks or []
        bp = (tasks[0].notebook_task.base_parameters or {}) if tasks and tasks[0].notebook_task else {}
        if bp.get("persona") != p:
            raise Stop(f"job {jid} (lab_persona_{p}) probes persona {bp.get('persona')!r}, not {p!r}")
        out[p] = int(jid)
    return out


# --------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------
_DENIED = ("permission", "not authorized", "unauthorized", "forbidden", "403", "permission_denied", "only admins",
           "is not an admin", "not allowed")


def is_denied(e) -> bool:
    """Used only to choose the manual fallback (the safe direction), so message text is acceptable here."""
    return type(e).__name__ in ("PermissionDenied", "Unauthenticated") or \
        str(getattr(e, "error_code", "") or "").upper() in ("PERMISSION_DENIED", "SCIM_403") or \
        any(k in str(e).lower() for k in _DENIED)


def retry(fn, tries: int = 8, sleep_s: float = 5.0, ok=lambda v: True, sleep=time.sleep):
    """Retry fn() until it returns a value for which ok(value) is true (eventual consistency). Returns the last
    value (or raises the last error)."""
    last_err, v = None, None
    for i in range(tries):
        try:
            v = fn()
            if ok(v):
                return v
            last_err = None
        except Exception as e:  # noqa: BLE001 - retried, re-raised at the end
            last_err = e
        if i < tries - 1:
            sleep(sleep_s)
    if last_err is not None:
        raise last_err
    return v


def ws_path(p: str) -> str:
    """Workspace API path (without the /Workspace FUSE prefix)."""
    return p[len("/Workspace"):] if p.startswith("/Workspace/") else p


def now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def new_suite_id() -> str:
    return f"suite-{now():%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# --------------------------------------------------------------------------------------------
# Bindings, sealed documents, ownership decisions (pure)
# --------------------------------------------------------------------------------------------
def make_binding(account_id: str, workspace_id: str, target: str, root: str, cfg: Config, mode: str = "demo") -> str:
    """What every record (identity receipts, checkpoints) is bound to: account, workspace, bundle target, deployment
    root, lab names. A demo deployment needs the account ID (its identities are account identities). An audit-only
    deployment has no identities, so it is bound to workspace, target, root and lab names only (the account ID, if
    given, is ignored; the generation is checked separately)."""
    if mode == "audit":
        account_id = "audit-only"
    for v, what in ((account_id, "account_id"), (workspace_id, "workspace_id"), (target, "target"), (root, "root")):
        if not v:
            raise Stop(f"can't bind the lab's records: {what} is unknown")
    return "|".join([account_id, str(workspace_id), target, root, cfg.full_schema, cfg.resource_prefix,
                     cfg.persona_prefix])


def binding_slug(workspace_id: str, cfg: Config) -> str:
    """Stable file-name key for this lab that needs neither the deployment nor the account ID (used to find a
    checkpoint after the deployment's jobs are gone; the full binding inside the checkpoint is checked on load)."""
    return sha256("|".join([str(workspace_id), cfg.full_schema, cfg.resource_prefix, cfg.persona_prefix]))[:16]


def new_generation_id() -> str:
    return f"gen-{now():%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"


def deployment_key(workspace_id: str, target: str, root: str, cfg: Config) -> str:
    """What a generation record is bound to (no account ID needed, so audit-only deployments get one too)."""
    return "|".join([str(workspace_id), target, root, cfg.full_schema, cfg.resource_prefix, cfg.persona_prefix])


def checkpoint_file(slug: str, generation: str) -> str:
    """Home-folder checkpoint name: one per binding AND generation."""
    if not GEN_RE.fullmatch(generation or ""):
        raise Stop(f"invalid deployment generation {generation!r}")
    return f"teardown-checkpoint-{slug}-{generation}.json"


def checkpoint_reusable(cp: dict | None, binding: str, generation: str | None) -> str | None:
    """None if checkpoint cp may be reused for this binding and live generation, else why not (pure)."""
    if cp is None:
        return None
    if cp.get("binding") != binding:
        return "it belongs to another binding"
    if not generation or cp.get("generation") != generation:
        return (f"it belongs to deployment generation {cp.get('generation') or '(none: written before generations)'}, "
                f"the live deployment is {generation or '(no generation yet)'}")
    if cp.get("complete"):
        return "it is marked complete although this deployment generation is still live"
    return None


def seal(doc: dict) -> str:
    """JSON with an embedded sha256 over the canonical content."""
    body = {k: v for k, v in doc.items() if k != "sha256"}
    return json.dumps({**body, "sha256": sha256(json.dumps(body, sort_keys=True, default=str))}, indent=1,
                      sort_keys=True, default=str)


def unseal(text: str, what: str) -> dict:
    """Parse a sealed document; Stop if it doesn't verify (fail closed)."""
    try:
        d = json.loads(text)
    except (TypeError, ValueError):
        raise Stop(f"{what} is not valid JSON: not trusted")
    want = d.pop("sha256", None) if isinstance(d, dict) else None
    if not want or sha256(json.dumps(d, sort_keys=True, default=str)) != want:
        raise Stop(f"{what} doesn't match its checksum: not trusted (inspect it, then remove it to start over)")
    return d


def unexpected_writers(effective: dict, allowed: set) -> list:
    """Principals other than `allowed` with an effective (direct or inherited) write privilege, from the UC
    effective-permissions response."""
    out = []
    for a in (effective or {}).get("privilege_assignments", []) or []:
        p = a.get("principal")
        for pr in a.get("privileges", []) or []:
            priv = str(pr.get("privilege") or "").upper()
            if priv in WRITE_PRIVS and p not in allowed:
                src = pr.get("inherited_from_name")
                out.append(f"{p}: {priv}" + (f" (inherited from {str(pr.get('inherited_from_type', '')).lower()} "
                                             f"{src})" if src else ""))
    return out


def receipt_proof_ok(rec: dict) -> bool:
    """A receipt is valid proof only if it says the name was absent before this runner created it, with the marker."""
    try:
        p = json.loads(rec.get("proof") or "{}")
    except (TypeError, ValueError):
        return False
    if not isinstance(p, dict) or p.get("confirmed_absent_before") is not True:
        return False
    if rec.get("kind") == "acl":
        return p.get("before") == [] and p.get("after") == ["CAN_READ"]
    return p.get("marker") == identity_marker(rec.get("display_name") or "")


def decide_identity(found: list, receipt: dict | None, display_name: str) -> tuple:
    """What to do with one persona identity BEFORE anything is created.
    found    account identities with exactly this display name (account SCIM, filtered by displayName)
    receipt  this binding's creation receipt for the role, or None
    -> ('create', None) | ('reuse', identity) | ('refuse', reason)"""
    if len(found) > 1:
        return "refuse", f"{len(found)} identities are named {display_name}; resolve by hand"
    if not found:
        return "create", None
    f = found[0]
    if receipt and str(receipt.get("scim_id")) == str(f.get("id")) and receipt_proof_ok(receipt) \
            and (receipt.get("kind") != "service_principal" or receipt.get("application_id") == f.get("applicationId")):
        return "reuse", f
    if is_marked(display_name, f.get("externalId")):
        # created by a lab (another deployment or the CLI bootstrap): usable, but never deleted by this binding
        return "reuse", {**f, "_origin": "marked"}
    return "refuse", (f"{display_name} already exists and this lab has no receipt for it (no marker visible); "
                      "it is never adopted. Use another persona_prefix, or the manual fallback")


def identity_teardown_action(rec: dict, live) -> tuple:
    """Decide one identity receipt at teardown. live: the account identity (dict), None after a STRUCTURED 404, or
    {'error': ...} for anything else. -> ('delete' | 'gone' | 'unresolved', reason). Only an exact match is deleted."""
    if not receipt_proof_ok(rec):
        return "unresolved", "receipt has no valid creation proof"
    if live is None:
        return "gone", "already gone (structured 404)"
    if not isinstance(live, dict) or "error" in live:
        return "unresolved", f"couldn't read it: {(live or {}).get('error') if isinstance(live, dict) else live}"
    if str(live.get("id")) != str(rec.get("scim_id")):
        return "unresolved", "live ID differs from the receipt"
    if live.get("displayName") != rec.get("display_name"):
        return "unresolved", f"displayName is now {live.get('displayName')!r}, receipt says {rec.get('display_name')!r}"
    ext = live.get("externalId")
    if ext is not None and ext != identity_marker(rec.get("display_name") or ""):
        return "unresolved", f"externalId {ext!r} is not the lab marker"
    if rec.get("kind") == "service_principal":
        if not live.get("applicationId"):
            return "unresolved", "live applicationId not readable: can't match the receipt"
        if live.get("applicationId") != rec.get("application_id"):
            return "unresolved", "live applicationId differs from the receipt"
    return "delete", "receipt with creation proof; live ID, displayName" + \
        (" and applicationId" if rec.get("kind") == "service_principal" else "") + " match"


def acl_teardown_action(rec: dict, live_levels) -> tuple:
    """The CAN_READ entry the runner added. live_levels: the SP's direct (non-inherited) levels on the object now, or
    None after a structured 404 for the object. -> ('remove' | 'gone' | 'unresolved', reason)"""
    if not receipt_proof_ok(rec):
        return "unresolved", "ACL receipt has no before/after proof"
    if live_levels is None:
        return "gone", "object gone (structured 404)"
    levels = sorted(live_levels)
    if not levels:
        return "gone", "entry already removed"
    if levels == ["CAN_READ"]:
        return "remove", "live entry is exactly the recorded CAN_READ"
    return "unresolved", f"live entry is now {levels}, not the recorded CAN_READ: manual review"


def manual_steps(cfg: Config, me: str, failed: str = "") -> str:
    """Exact console steps for the manual fallback (no account-level automation)."""
    ids = persona_identities(cfg.persona_prefix)
    sp, gr = ids["service_principals"], ids["groups"]
    lines = [f"Automated persona setup stopped{(' at: ' + failed) if failed else ''}.",
             "Manual fallback (an account admin, or your identity team through the IdP / SCIM):",
             "1. Account console > User management > Service principals: create three service principals",
             f"     {sp['clinician']}, {sp['pii_reader']}, {sp['outsider']}",
             "2. Account console > User management > Groups: create four ACCOUNT groups",
             f"     {gr['clinicians']}, {gr['pii_readers']}, {gr['all_facilities']}, {gr['outsiders']}",
             f"3. Members: {sp['clinician']} -> {gr['clinicians']}; {sp['pii_reader']} -> {gr['pii_readers']} and "
             f"{gr['all_facilities']}; {sp['outsider']} -> {gr['outsiders']}",
             "4. Account console > Workspaces > <this workspace> > Permissions: add the 3 service principals and 4 groups as User",
             "5. Workspace settings > Identity and access > Service principals: give each 'Workspace access' and "
             "'Databricks SQL access'",
             f"6. Each service principal > Permissions: grant 'Service principal: User' to {me} (needed to set run_as)",
             "7. Rerun 00_run_lab action=personas with",
             "     manual_app_ids = <clinician app ID>,<pii_reader app ID>,<outsider app ID>",
             f"     manual_groups  = {gr['clinicians']},{gr['pii_readers']},{gr['all_facilities']},{gr['outsiders']}",
             "   Identities given this way are bound as 'provided' and never deleted by teardown. Receipts of identities",
             "   the runner created earlier are kept, so teardown still removes those."]
    return "\n".join(lines)


# Preflight checklist (docs/workspace-setup.md, 'Before you start'); preflight verifies 4 and 5.
CHECKLIST = [
    (1, "Who provisions identities into the Databricks account (account-level SCIM from your IdP, automatic "
        "identity management, or workspace-level SCIM)?"),
    (2, "Will the IdP connector, Terraform or any clean-up job leave alone service principals and groups it didn't "
        "create (no 'remove unmanaged' or drift deletion)?"),
    (3, "Does policy allow workspace admins to create account service principals and groups in this workspace? If "
        "not, use the pre-provisioned path (manual_app_ids / manual_groups)."),
    (4, "Is the person running the lab a workspace admin? (Without it every identity step returns 403.)"),
    (5, "restrict_workspace_admins setting, and any naming convention or reserved prefix for groups (use a "
        "distinctive persona_prefix)."),
    (6, "Are lab identities allowed in this account at all, or only in a sandbox workspace, and what should "
        "teardown remove?"),
]


def restrict_admins_note(value: str | None) -> str:
    v = (value or "").upper()
    if not v:
        return "couldn't read restrict_workspace_admins: confirm manually"
    if v in ("ALLOW_ALL", "STATUS_UNSPECIFIED"):
        return f"restrict_workspace_admins = {v}: no restriction"
    if v == "RESTRICT_TOKENS_AND_JOB_RUN_AS":
        return (f"restrict_workspace_admins = {v}: supported (the lab grants you the Service Principal User role on "
                "each persona before it sets run_as)")
    return f"restrict_workspace_admins = {v}: unrecognised value, confirm manually"


# --------------------------------------------------------------------------------------------
# Run plan and resumable steps
# --------------------------------------------------------------------------------------------
def run_plan(mode: str) -> list:
    """[(step, job resource key)] in order. Every step depends on all steps before it."""
    if mode == "audit":
        return [("audit", "lab_audit")]
    return [("setup", "lab_setup"), *((f"persona_{p}", f"lab_persona_{p}") for p in PERSONAS),
            ("collect", "lab_collect"), ("audit", "lab_audit")]


def pending_steps(plan: list, done: set) -> list:
    """Resume from the first step that didn't succeed, and rerun everything after it (collect and audit depend on
    every persona step, so they rerun after any upstream retry)."""
    for i, (step, _) in enumerate(plan):
        if step not in done:
            return plan[i:]
    return []


def job_parameters(defined: set, values: dict) -> dict:
    """Only parameters the deployed job defines, and only non-empty values (empty = keep the job's default)."""
    return {k: str(v) for k, v in values.items() if k in defined and v is not None and str(v) != ""}


def list_param(widget: tuple, deployed_default: str, inferred: str) -> str | None:
    """approved_builders / cleartext_groups: an explicit widget value wins; a blank widget never overrides an allowlist
    the deployment already configured; only when nothing is configured anywhere is the persona group inferred."""
    if widget:
        return ",".join(widget)
    if str(deployed_default or "").strip():
        return None
    return inferred or None


def latest_states(rows: list) -> dict:
    """{step: state} from step-log rows (dicts with step, state, ts), latest row per step."""
    out = {}
    for r in sorted(rows, key=lambda r: r["ts"]):
        out[r["step"]] = r["state"]
    return out


def guard_skip_rows(persona: str, reason: str, run_id: str, catalogue: dict, ts=None) -> list:
    """Trusted SKIP results for a persona whose job refused to run as the wrong identity (written by the runner)."""
    rows = []
    for tid, meta in catalogue.items():
        personas = meta.get("personas") or list((meta.get("expected") or {}))
        if persona not in personas or meta.get("source") == "evidence":
            continue
        exp = (meta.get("expected") or {}).get(persona, "")
        rows.append({"run_id": run_id, "test_id": tid, "layer": int(meta.get("layer", 0)),
                     "title": meta.get("title", tid), "persona": persona, "status": "SKIP", "expected": str(exp),
                     "observed": reason[:2000], "evidence": json.dumps({"recorded_by": "00_run_lab (trusted)"}),
                     "doc_url": meta.get("doc", ""), "ts": ts or now()})
    return rows


def results_query(dashboard_json: dict) -> str:
    """The dashboard's own dataset SQL (MISSING / PENDING included) plus a stable order."""
    return "".join(dashboard_json["datasets"][0]["queryLines"]) + "\nORDER BY layer, test_id, persona"


def summarize_results(rows: list) -> dict:
    """{'suite': id, 'mode': m, 'counts': {status: n}, 'missing': [...], 'pending': [...]} from result dicts."""
    counts, missing, pending = {}, [], []
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        if r["status"] == "MISSING":
            missing.append(f"{r['test_id']}/{r['persona']}")
        elif r["status"] == "PENDING":
            pending.append(f"{r['test_id']}/{r['persona']}")
    suites = sorted({r.get("suite_run_id") for r in rows if r.get("suite_run_id")})
    return {"suite": suites[0] if len(suites) == 1 else (suites or None), "counts": dict(sorted(counts.items())),
            "missing": missing, "pending": pending,
            "mode": next((r.get("suite_mode") for r in rows if r.get("suite_mode")), None)}


def schema_leftovers(inventory: dict) -> list:
    """['table x', 'function y', ...] from {kind: [names]}: anything still in a schema."""
    return [f"{kind} {n}" for kind, names in sorted(inventory.items()) for n in sorted(names)]


# --------------------------------------------------------------------------------------------
# Table adapter: the runner's ONLY SQL surface for its own tables (faked in tests)
# --------------------------------------------------------------------------------------------
_PROP_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")


class Tables:
    def __init__(self, spark):
        self.spark = spark

    @staticmethod
    def _q(full: str) -> str:
        parts = full.split(".")
        if len(parts) != 3:
            raise ValueError(f"not a three-part table name: {full!r}")
        return ".".join(q(p) for p in parts)

    def exists(self, full: str) -> bool:
        self._q(full)
        return self.spark.catalog.tableExists(full)

    def create(self, full: str, columns: str, comment: str, props: dict):
        """CREATE TABLE without IF NOT EXISTS: fails if the table already exists."""
        for k, v in props.items():
            if not (_PROP_RE.fullmatch(k) and _PROP_RE.fullmatch(v)):
                raise ValueError(f"invalid table property {k!r}")
        if not re.fullmatch(r"[A-Za-z0-9 ,:._()-]{1,300}", comment):
            raise ValueError("invalid comment")
        tp = ", ".join(f"'{k}' = '{v}'" for k, v in props.items())
        self.spark.sql(f"CREATE TABLE {self._q(full)} ({columns}) COMMENT '{comment}' TBLPROPERTIES ({tp})")

    def props(self, full: str) -> dict:
        return {r.key: r.value for r in self.spark.sql(f"SHOW TBLPROPERTIES {self._q(full)}").collect()}

    def rows(self, full: str, where: dict | None = None) -> list:
        where = where or {}
        cond = " AND ".join(f"{q(k)} = :w_{k}" for k in where) or "TRUE"
        return [r.asDict() for r in self.spark.sql(f"SELECT * FROM {self._q(full)} WHERE {cond}",
                                                   args={f"w_{k}": v for k, v in where.items()}).collect()]

    def insert(self, full: str, row: dict):
        cols = list(row)
        self.spark.sql(f"INSERT INTO {self._q(full)} ({', '.join(q(c) for c in cols)}) VALUES "
                       f"({', '.join(':v_' + c for c in cols)})", args={f"v_{c}": row[c] for c in cols})

    def update(self, full: str, where: dict, values: dict):
        sets = ", ".join(f"{q(k)} = :s_{k}" for k in values)
        cond = " AND ".join(f"{q(k)} = :w_{k}" for k in where)
        self.spark.sql(f"UPDATE {self._q(full)} SET {sets} WHERE {cond}",
                       args={**{f"s_{k}": v for k, v in values.items()}, **{f"w_{k}": v for k, v in where.items()}})

    def drop(self, full: str):
        self.spark.sql(f"DROP TABLE {self._q(full)}")


RECEIPT_COLS = ("kind STRING, role STRING, display_name STRING, scim_id STRING, application_id STRING, "
                "binding STRING, generation STRING, proof STRING, state STRING, note STRING, updated_at STRING")
BINDING_COLS = ("kind STRING, role STRING, display_name STRING, scim_id STRING, application_id STRING, "
                "origin STRING, binding STRING, generation STRING, note STRING, updated_at STRING")
STEPS_COLS = ("run_id STRING, step STRING, job_key STRING, job_run_id STRING, state STRING, detail STRING, ts STRING, "
              "generation STRING")


# --------------------------------------------------------------------------------------------
# Identity store: runner-created, protected, receipts apart from bindings
# --------------------------------------------------------------------------------------------
class IdentityStore:
    """Receipts (what this runner created, with proof; never overwritten, only their state changes) and bindings
    (which identities the personas use now). Both tables must have been created by this runner: a creation record
    in the deployment folder holds their UC table IDs and a store ID that is also a table property."""

    def __init__(self, r: "Runner", dep: Deployment, binding: str, generation: str | None):
        self.r, self.dep, self.binding, self.generation = r, dep, binding, generation
        self.cfg = r.cfg
        self.record_path = f"{dep.root}/{RUNNER_DIR}/identity_store.json"
        self.full = {k: f"{self.cfg.full_schema}.{t}" for k, t in (("receipts", RECEIPTS_T), ("bindings", BINDINGS_T))}
        self.record = None

    # -- creation proof --------------------------------------------------------------------------
    def _load_record(self):
        try:
            text = self.r.read_text(self.record_path)
        except FileNotFoundError:
            return None
        except Exception as e:  # noqa: BLE001
            if is_not_found(e):
                return None
            raise Stop(f"identity store record {self.record_path} not readable: {first_line(e, 150)}")
        rec = unseal(text, f"identity store record {self.record_path}")
        if rec.get("binding") != self.binding:
            raise Stop(f"identity store record {self.record_path} is bound to another deployment/account/workspace: "
                       "not used")
        if not self.generation or rec.get("generation") != self.generation:
            raise Stop(f"identity store record {self.record_path} belongs to deployment generation "
                       f"{rec.get('generation') or '(none)'}, the live deployment is {self.generation or '(none)'}: "
                       "state can't be proven, not used")
        return rec

    def open(self, create: bool) -> bool:
        """True if the store is usable (verified). create=True creates it when neither record nor tables exist."""
        rec = self._load_record()
        exists = {k: self.r.tables.exists(f) for k, f in self.full.items()}
        if rec is None:
            if any(exists.values()):
                raise Stop("identity tables " + ", ".join(f for k, f in self.full.items() if exists[k]) +
                           " exist but this runner has no creation record for them: not trusted. Inspect and drop "
                           "them by hand (or use another schema).")
            if not create:
                return False
            if not self.generation:
                raise Stop("no deployment generation: the identity store can't be created")
            store_id = uuid.uuid4().hex
            props = {STORE_PROP: store_id, BINDING_PROP: sha256(self.binding)[:32]}
            for k, cols in (("receipts", RECEIPT_COLS), ("bindings", BINDING_COLS)):
                props_k = {**props, KIND_PROP: k}
                self.r.tables.create(self.full[k], cols, "GenAI governance lab: 00_run_lab identity store. Owner only",
                                     props_k)
            ids = {k: self.r.table_id(f) for k, f in self.full.items()}
            self.record = {"binding": self.binding, "generation": self.generation, "store_id": store_id, "tables": ids,
                           "created_by": self.r.me.user_name, "created_at": str(now())}
            self.r.write_text(self.record_path, seal(self.record))
            self.r.log(f"identity store created: {', '.join(self.full.values())} (record {self.record_path})")
        else:
            missing = [f for k, f in self.full.items() if not exists[k]]
            if missing:
                raise Stop(f"identity store tables missing ({missing}) although the record {self.record_path} exists: "
                           "state can't be proven; restore them or resolve by hand")
            for k, f in self.full.items():
                if self.r.table_id(f) != rec["tables"].get(k):
                    raise Stop(f"{f} is not the table this runner created (table ID differs from the record)")
                p = self.r.tables.props(f)
                if p.get(STORE_PROP) != rec["store_id"] or p.get(BINDING_PROP) != sha256(self.binding)[:32]:
                    raise Stop(f"{f} doesn't carry this runner's store marker: not trusted")
            self.record = rec
        self.check_protected()
        return True

    def check_protected(self):
        """Fail closed if anyone but the caller can write the store (effective privileges, including inherited)."""
        me = self.r.me.user_name
        problems = []
        for f in self.full.values():
            try:
                eff = self.r.api("GET", f"{UC}/effective-permissions/table/{f}")
            except Exception as e:  # noqa: BLE001
                raise Stop(f"can't read the effective permissions of {f} ({first_line(e, 120)}): protection unproven")
            problems += [f"{f}: {x}" for x in unexpected_writers(eff, {me})]
            owner = self.r.table_owner(f)
            if owner != me:
                problems.append(f"{f}: owned by {owner}, not {me}")
        if problems:
            raise Stop("identity store is writable by others; not trusted:\n  " + "\n  ".join(problems))

    # -- records ---------------------------------------------------------------------------------
    def _same_generation(self, rows: list, what: str) -> list:
        other = sorted({str(r.get("generation") or "(none)") for r in rows if r.get("generation") != self.generation})
        if other:
            raise Stop(f"{what} hold records of another deployment generation ({', '.join(other)}; live "
                       f"{self.generation}): state can't be proven, not used")
        return rows

    def receipts(self) -> list:
        return self._same_generation(self.r.tables.rows(self.full["receipts"], {"binding": self.binding}),
                                     self.full["receipts"])

    def bindings(self) -> list:
        return self._same_generation(self.r.tables.rows(self.full["bindings"], {"binding": self.binding}),
                                     self.full["bindings"])

    def add_receipt(self, kind, role, display_name, scim_id, application_id, proof):
        """A new receipt. Receipts are never replaced: a second receipt for the same kind/role/ID is refused."""
        if any(r["kind"] == kind and r["role"] == role and r["scim_id"] == str(scim_id) for r in self.receipts()):
            raise Stop(f"a receipt for {kind} {role} {scim_id} already exists: not overwritten")
        self.r.tables.insert(self.full["receipts"], {
            "kind": kind, "role": role, "display_name": display_name, "scim_id": str(scim_id or ""),
            "application_id": application_id or "", "binding": self.binding, "generation": self.generation,
            "proof": json.dumps(proof, default=str), "state": "active", "note": "", "updated_at": str(now())})

    def set_receipt_state(self, rec: dict, state: str, note: str):
        self.r.tables.update(self.full["receipts"], {"kind": rec["kind"], "role": rec["role"],
                                                     "scim_id": rec["scim_id"], "binding": self.binding},
                             {"state": state, "note": note[:500], "updated_at": str(now())})

    def set_binding(self, kind, role, display_name, scim_id, application_id, origin, note=""):
        """Bindings are replaceable (switching to the manual fallback); receipts are not touched."""
        where = {"kind": kind, "role": role, "binding": self.binding}
        row = {"generation": self.generation, "display_name": display_name, "scim_id": str(scim_id or ""), "application_id": application_id or "",
               "origin": origin, "note": note, "updated_at": str(now())}
        if self.r.tables.rows(self.full["bindings"], where):
            self.r.tables.update(self.full["bindings"], where, row)
        else:
            self.r.tables.insert(self.full["bindings"], {**where, **row})

    def drop(self):
        """Drop both tables (only after every receipt is resolved), verified as this runner's tables first."""
        for k, f in self.full.items():
            if not self.r.tables.exists(f):
                continue
            if self.record and (self.r.table_id(f) != self.record["tables"].get(k)
                                or self.r.tables.props(f).get(STORE_PROP) != self.record["store_id"]):
                raise Stop(f"{f} is not this runner's table: not dropped")
            self.r.tables.drop(f)


# --------------------------------------------------------------------------------------------
# The runner (talks to Databricks)
# --------------------------------------------------------------------------------------------
class Runner:
    def __init__(self, w, spark, cfg: Config, log=print, display=None, read_text=None, write_text=None,
                 sleep=time.sleep, tables=None, list_dir=None):
        self.w, self.spark, self.cfg, self.log = w, spark, cfg, log
        self.display = display or (lambda df: self.log(df))
        # Synchronous Workspace API, not the /Workspace FUSE mount: FUSE writes are flushed asynchronously, so a
        # checkpoint written just before its folder is deleted can be lost or recreate the folder (seen live).
        self.read_text = read_text or self._ws_read
        self.write_text = write_text or self._ws_write
        self.list_dir = list_dir or self._ws_list
        self.sleep = sleep
        self.tables = tables or Tables(spark)
        self.host = w.config.host.rstrip("/")
        self._me = None
        self._dep = None
        self._ws = None
        self._gen = {}                                   # deployment root -> generation ID (verified)
        self.names = lab_names(cfg.resource_prefix)
        self.T = f"{q(cfg.catalog)}.{q(cfg.schema)}"

    # ---- basics ------------------------------------------------------------------------------
    def _ws_read(self, path: str) -> str:
        from databricks.sdk.service.workspace import ExportFormat
        with self.w.workspace.download(ws_path(path), format=ExportFormat.AUTO) as f:
            return f.read().decode("utf-8")

    def _ws_write(self, path: str, text: str):
        from databricks.sdk.service.workspace import ImportFormat
        p = ws_path(path)
        self.w.workspace.mkdirs(posixpath.dirname(p))
        self.w.workspace.upload(p, io.BytesIO(text.encode("utf-8")), format=ImportFormat.AUTO, overwrite=True)

    def _ws_list(self, path: str) -> list:
        """File paths (with the /Workspace prefix) directly in a workspace folder; [] if the folder doesn't exist."""
        try:
            return [_fuse(o.path) for o in self.w.workspace.list(ws_path(path)) if o.path]
        except Exception as e:  # noqa: BLE001
            if is_not_found(e):
                return []
            raise Stop(f"can't list {path}: {first_line(e, 150)}")

    def _read_optional(self, path: str, what: str) -> str | None:
        """File text, or None only if it provably doesn't exist (FileNotFoundError / structured not-found)."""
        try:
            return self.read_text(path)
        except FileNotFoundError:
            return None
        except Exception as e:  # noqa: BLE001
            if is_not_found(e):
                return None
            raise Stop(f"{what} {path} not readable ({first_line(e, 150)}): state can't be proven")

    def api(self, method, path, query=None, body=None):
        return self.w.api_client.do(method, path, query=query, body=body)

    @property
    def me(self):
        if self._me is None:
            self._me = self.w.current_user.me()
        return self._me

    def workspace_id(self) -> str:
        if self._ws is None:
            self._ws = str(self.w.get_workspace_id())
        return self._ws

    def table_id(self, full: str) -> str:
        return str(self.w.tables.get(full).table_id)

    def table_owner(self, full: str) -> str:
        return str(self.w.tables.get(full).owner)

    def is_admin(self) -> bool:
        return any(getattr(g, "display", None) == "admins" for g in (self.me.groups or []))

    def require_admin(self):
        if not self.is_admin():
            raise Stop(f"{self.me.user_name} is not a workspace admin (not in the 'admins' group). Every identity and "
                       "run_as step needs it: ask a workspace admin to run the lab, or to add you (checklist item 4).")

    def deployment(self) -> Deployment:
        if self._dep is None:
            self._dep = find_deployment(self.w, self.cfg, self.read_text)
        check_mode(self.cfg.action, self.cfg.mode, self._dep)      # every time, also for a cached deployment
        return self._dep

    def binding(self, dep: Deployment | None = None) -> str:
        d = dep or self.deployment()
        return make_binding(self.cfg.account_id, self.workspace_id(), d.target, d.root, self.cfg, d.mode)

    # ---- deployment generation -----------------------------------------------------------------
    def generation_path(self, dep: Deployment) -> str:
        return f"{dep.root}/{RUNNER_DIR}/{GEN_FILE}"

    def generation(self, dep: Deployment, create: bool = False) -> str | None:
        """This deployment's generation ID (verified against the live deployment), or None if it has none yet and
        create is False. The record lives in the deployment folder, so a redeploy after teardown starts a new one."""
        if dep.root in self._gen:
            return self._gen[dep.root]
        p = self.generation_path(dep)
        key = deployment_key(self.workspace_id(), dep.target, dep.root, self.cfg)
        text = self._read_optional(p, "generation record")
        if text is not None:
            rec = unseal(text, f"generation record {p}")
            if rec.get("deployment_key") != key:
                raise Stop(f"generation record {p} is bound to another deployment: not used")
            if rec.get("audit_job") != dep.jobs.get("lab_audit"):
                raise Stop(f"generation record {p} was written for audit job {rec.get('audit_job')}, but this "
                           f"deployment's audit job is {dep.jobs.get('lab_audit')}: state can't be proven. Inspect the "
                           "deployment folder; remove the record only if nothing of the old deployment is left.")
            if not GEN_RE.fullmatch(str(rec.get("generation") or "")):
                raise Stop(f"generation record {p} holds no valid generation ID: not used")
            self._gen[dep.root] = rec["generation"]
            return rec["generation"]
        if not create:
            return None
        gen = new_generation_id()
        self.write_text(p, seal({"generation": gen, "deployment_key": key, "audit_job": dep.jobs.get("lab_audit"),
                                 "created_by": self.me.user_name, "created_at": str(now()), "actions": []}))
        back = self._read_optional(p, "generation record")
        if back is None or unseal(back, f"generation record {p}").get("generation") != gen:
            raise Stop(f"generation record {p} didn't read back as written: not used")
        self.log(f"deployment generation {gen} recorded ({p})")
        self._gen[dep.root] = gen
        return gen

    def generation_record(self, dep: Deployment) -> dict | None:
        """The sealed generation record (None only if it provably doesn't exist)."""
        p = self.generation_path(dep)
        text = self._read_optional(p, "generation record")
        return None if text is None else unseal(text, f"generation record {p}")

    def update_generation(self, dep: Deployment, change) -> dict:
        """Apply change(record) to the sealed generation record, write it, and verify it reads back as written."""
        gen = self.generation(dep, create=True)
        p = self.generation_path(dep)
        rec = self.generation_record(dep)
        if rec is None or rec.get("generation") != gen:
            raise Stop(f"generation record {p} changed while in use: not trusted")
        change(rec)
        self.write_text(p, seal(rec))
        back = self.generation_record(dep)
        if back != rec:
            raise Stop(f"generation record {p} didn't read back as written: not used")
        return rec

    def mark_generation(self, dep: Deployment, action: str, detail: str = "") -> str:
        """Record in the generation record that `action` (personas / run) started for this generation, BEFORE it changes
        anything: teardown uses this as evidence of what ran."""
        rec = self.update_generation(dep, lambda r: r.setdefault("actions", []).append(
            {"action": action, "detail": detail, "by": self.me.user_name, "at": str(now())}))
        return rec["generation"]

    # ---- audit results tables: creation proof (audit-only deployments) -----------------------
    def table_absent(self, full: str) -> bool:
        """True only on a structured not-found from Unity Catalog; False if it exists; Stop if that can't be told."""
        try:
            self.w.tables.get(full)
            return False
        except Exception as e:  # noqa: BLE001
            if is_not_found(e):
                return True
            raise Stop(f"couldn't tell whether {full} exists ({first_line(e, 150)}): state can't be proven")

    def record_results_absence(self, dep: Deployment):
        """Before the audit job first runs in this generation: for each table it will create, record whether it was
        absent (structured not-found). Entries are written once and never changed afterwards."""
        todo = {}
        rec = self.generation_record(dep) or {}
        known = rec.get("results_tables") or {}
        for t in AUDIT_RESULT_TABLES:
            if t not in known:
                todo[t] = {"absent_before": self.table_absent(f"{self.cfg.full_schema}.{t}"), "table_id": None,
                           "checked_at": str(now())}
        if todo:
            self.update_generation(dep, lambda r: r.setdefault("results_tables", {}).update(todo))
            pre = [t for t, v in todo.items() if not v["absent_before"]]
            if pre:
                self.log(f"NOTE: {', '.join(pre)} existed before this generation's first run: teardown won't drop them")

    def record_results_ids(self, dep: Deployment):
        """After the audit job ran: the UC table ID of each table this generation created (absent before, no ID yet)."""
        rec = self.generation_record(dep) or {}
        ids = {}
        for t, v in (rec.get("results_tables") or {}).items():
            if v.get("absent_before") and not v.get("table_id"):
                full = f"{self.cfg.full_schema}.{t}"
                if not self.table_absent(full):
                    ids[t] = self.table_id(full)
        if ids:
            self.update_generation(dep, lambda r: [r["results_tables"][t].update(
                {"table_id": i, "recorded_at": str(now())}) for t, i in ids.items()])
            self.log(f"recorded the UC table IDs of {', '.join(sorted(ids))} (created by this generation)")

    def audit_results_decisions(self, proofs: dict) -> list:
        """[(table, action, why)] for the audit results tables: action 'drop' (live UC ID equals the recorded one),
        'gone' (structured not-found), or 'manual' (no or incomplete proof, pre-existing, or replaced)."""
        out = []
        for t in AUDIT_RESULT_TABLES:
            full = f"{self.cfg.full_schema}.{t}"
            try:
                if self.table_absent(full):
                    out.append((full, "gone", "structured not-found"))
                    continue
                live = self.table_id(full)
            except Stop as e:
                out.append((full, "manual", str(e)))
                continue
            proof = proofs.get(t)
            if not proof:
                out.append((full, "manual", "no creation record for this generation"))
            elif not proof.get("absent_before"):
                out.append((full, "manual", "it existed before this generation's first run"))
            elif not proof.get("table_id"):
                out.append((full, "manual", "its table ID was never recorded after creation"))
            elif str(proof["table_id"]) != str(live):
                out.append((full, "manual", f"table ID {live} differs from the recorded {proof['table_id']} "
                                           "(replaced since the run)"))
            else:
                out.append((full, "drop", f"table ID {live} matches the record"))
        return out

    def teardown_audit_results(self, cp: dict) -> list:
        """Drop the proven audit results tables (no IF EXISTS, no CASCADE), verify each is gone. If any table lacks exact
        proof, nothing is dropped and the manual-review items are returned; the caller stops."""
        decisions = self.audit_results_decisions(cp.get("results_tables") or {})
        manual = [f"{full}: {why}" for full, act, why in decisions if act == "manual"]
        if manual:
            return manual                     # decided first: nothing is dropped unless every table is proven
        dropped = cp.setdefault("audit_results_dropped", [])
        for full, act, why in decisions:
            if act != "drop":
                continue
            self.tables.drop(full)
            self._gone(lambda f=full: self.w.tables.get(f), f"table {full}")
            dropped.append(full)
            self.log(f"dropped (verified): {full} ({why})")
        return []

    def store(self, dep: Deployment | None = None, create_generation: bool = False) -> IdentityStore:
        d = dep or self.deployment()
        return IdentityStore(self, d, self.binding(d), self.generation(d, create=create_generation))

    # ---- account / workspace SCIM --------------------------------------------------------------
    def account_lookup(self, kind: str, display_name: str) -> list:
        """Account identities named exactly display_name. Filter by displayName only (never externalId), then compare
        in code; the proxy's filter is not trusted to be exact."""
        r = self.api("GET", f"{ACCOUNT_SCIM}/{kind}", query={"filter": f'displayName eq "{display_name}"'}) or {}
        return [x for x in (r.get("Resources") or []) if x.get("displayName") == display_name]

    def account_get(self, kind: str, scim_id: str):
        """dict; None only after a STRUCTURED not-found; {'error': ...} for anything else (never treated as gone)."""
        if not re.fullmatch(r"[0-9]{1,20}", str(scim_id or "")):
            return {"error": f"invalid recorded id {scim_id!r}"}
        try:
            return self.api("GET", f"{ACCOUNT_SCIM}/{kind}/{scim_id}")
        except Exception as e:  # noqa: BLE001
            return None if is_not_found(e) else {"error": first_line(e, 160)}

    # ---- preflight ---------------------------------------------------------------------------
    def preflight(self) -> dict:
        cfg, rep = self.cfg, {}
        self.log(f"signed in as {self.me.user_name}")
        try:
            import databricks.sdk
            rep["sdk"] = databricks.sdk.version.__version__
        except Exception:  # noqa: BLE001
            rep["sdk"] = "unknown"
        rep["workspace_admin"] = self.is_admin()
        self.log(f"[{'ok' if rep['workspace_admin'] else 'STOP'}] workspace admin ('admins' group): {rep['workspace_admin']}")
        try:
            self.w.catalogs.get(cfg.catalog)
            self.log(f"[ok] catalog {cfg.catalog} exists")
        except Exception as e:  # noqa: BLE001
            raise Stop(f"catalog {cfg.catalog} is not readable: {first_line(e, 200)}")
        if cfg.warehouse_id:
            wh = self.w.warehouses.get(cfg.warehouse_id)
            serverless = bool(getattr(wh, "enable_serverless_compute", False))
            self.log(f"[{'ok' if serverless else 'WARN'}] warehouse {wh.name}: {getattr(wh.state, 'value', wh.state)}, "
                     f"serverless={serverless}")
        else:
            self.log("[WARN] warehouse_id is empty: required for action=run")
        dep = None
        try:
            dep = self.deployment()
            rep["deployment"] = {"root": dep.root, "target": dep.target, "mode": dep.mode, "jobs": len(dep.jobs),
                                 "source_root": dep.source_root}
            self.log(f"[ok] deployment found: target {dep.target}, mode {dep.mode}, {len(dep.jobs)} jobs, notebooks in "
                     f"{dep.source_root}")
            try:
                self.require_deployed_files(dep)
                rep["deployed_files"] = "present"
            except Stop as e2:
                rep["deployed_files"] = f"STOP: {e2}"
                self.log(f"[STOP] {e2}")
        except Stop as e:
            rep["deployment"] = None
            self.log(f"[STOP] {e}")
        if dep is not None:
            try:
                rep["generation"] = self.generation(dep) or "none yet (created by personas, run/resume or teardown-apply)"
                self.log(f"[ok] deployment generation: {rep['generation']}")
            except Stop as e:
                rep["generation"] = f"STOP: {e}"
                self.log(f"[STOP] deployment generation: {e}")
        try:
            from checks import ENFORCE_GATEWAY_KEY
            v = self.w.workspace_settings_v2.get_public_workspace_setting(ENFORCE_GATEWAY_KEY).as_dict()
            on = ((v.get("effective_boolean_val") or v.get("boolean_val") or {}).get("value"))
            rep["enforce_unity_gateway"] = on
            self.log(f"[info] Enforce Unity Gateway = {on}" + (": V-01/V-02, S-01 and F-02 are expected to SKIP "
                     "(legacy serving routes are disabled)" if on else ""))
        except Exception as e:  # noqa: BLE001
            self.log(f"[info] Enforce Unity Gateway not readable ({first_line(e, 120)})")
        try:
            ra = self.w.settings.restrict_workspace_admins.get().restrict_workspace_admins.status
            ra = getattr(ra, "value", ra)
        except Exception:  # noqa: BLE001
            ra = None
        rep["restrict_workspace_admins"] = ra
        self.log("Before you start (docs/workspace-setup.md):")
        for n, text in CHECKLIST:
            if n == 4:
                self.log(f"  {n}. verified: workspace admin = {rep['workspace_admin']}")
            elif n == 5:
                self.log(f"  {n}. {restrict_admins_note(ra)}; group naming convention / reserved prefixes: confirm "
                         f"manually (persona_prefix '{cfg.persona_prefix}')")
            else:
                self.log(f"  {n}. confirm manually: {text}")
        if cfg.mode == "demo" and dep is not None:
            receipts = {}
            try:
                st = self.store(dep)
                if st.open(create=False):
                    receipts = {(r["kind"], r["role"]): r for r in st.receipts() if r["state"] == "active"}
                    rep["identity_store"] = "verified"
                else:
                    rep["identity_store"] = "not created yet"
            except Stop as e:
                rep["identity_store"] = f"STOP: {e}"
                self.log(f"[STOP] identity store: {e}")
            ids = persona_identities(cfg.persona_prefix)
            plan = {}
            for kind, scim, names in (("service_principal", "ServicePrincipals", ids["service_principals"]),
                                      ("group", "Groups", ids["groups"])):
                for role, n in names.items():
                    try:
                        act, why = decide_identity(self.account_lookup(scim, n), receipts.get((kind, role)), n)
                    except Exception as e:  # noqa: BLE001
                        act, why = "unknown", first_line(e, 150)
                    plan[n] = act if act not in ("refuse", "unknown") else f"{act.upper()}: {why}"
            rep["identity_plan"] = plan
            self.log("identity plan: " + json.dumps(plan, indent=1))
            if any(v.startswith("REFUSE") for v in plan.values()):
                self.log("[STOP] some persona names are taken by identities the lab didn't create: use another persona_prefix")
        if not rep["workspace_admin"]:
            raise Stop(f"{self.me.user_name} is not a workspace admin: the lab can't create personas or bind run_as.")
        return rep

    # ---- personas ----------------------------------------------------------------------------
    def setup_personas(self) -> dict:
        self.require_admin()
        st = self.store(create_generation=True)
        st.open(create=True)
        self.mark_generation(st.dep, "personas")
        self.ensure_steps_table()           # the step log exists (empty) from the first identity change on
        if self.cfg.manual_app_ids:
            return self._manual_personas(st)
        try:
            return self._automated_personas(st)
        except Refused as e:
            self.log(manual_steps(self.cfg, self.me.user_name, str(e)))
            raise Stop(f"automated persona setup refused at '{e.step}': {e.error}. Follow the manual steps above.")

    def _automated_personas(self, st: IdentityStore) -> dict:
        from databricks.sdk.service.iam import ComplexValue
        ids = persona_identities(self.cfg.persona_prefix)
        receipts = {(r["kind"], r["role"]): r for r in st.receipts() if r["state"] == "active"}
        me = self.me.user_name
        # 1. decide every name BEFORE creating anything
        plan, refuse = {}, []
        for kind, scim, names in (("service_principal", "ServicePrincipals", ids["service_principals"]),
                                  ("group", "Groups", ids["groups"])):
            for role, n in names.items():
                try:
                    found = self.account_lookup(scim, n)
                except Exception as e:  # noqa: BLE001
                    raise (Refused(f"look up {n} (account SCIM read)", first_line(e, 200)) if is_denied(e) else e)
                act, x = decide_identity(found, receipts.get((kind, role)), n)
                plan[(kind, role)] = (act, x, n)
                if act == "refuse":
                    refuse.append(x)
        if refuse:
            raise Stop("Refusing (nothing created):\n  " + "\n  ".join(refuse))
        apps, sp_ids, group_ids = {}, {}, {}
        # 2. service principals: workspace SCIM create = account SP + workspace assignment
        for p in PERSONAS:
            act, x, n = plan[("service_principal", p)]
            if act == "create":
                try:
                    sp = self.w.service_principals.create(
                        display_name=n, external_id=identity_marker(n),
                        entitlements=[ComplexValue(value="workspace-access"), ComplexValue(value="databricks-sql-access")])
                except Exception as e:  # noqa: BLE001
                    raise (Refused(f"create service principal {n}", first_line(e, 200)) if is_denied(e) else e)
                st.add_receipt("service_principal", p, n, sp.id, sp.application_id,
                               {"confirmed_absent_before": True, "marker": identity_marker(n),
                                "marker_in_create_response": getattr(sp, "external_id", None) == identity_marker(n),
                                "created_by": me, "created_at": str(now()), "via": "workspace SCIM"})
                self.log(f"created service principal {n} (receipt recorded)")
                acc = retry(lambda n=n: self.account_lookup("ServicePrincipals", n), sleep=self.sleep,
                            ok=lambda r: len(r) == 1)
                if not acc or str(acc[0].get("id")) != str(sp.id) or acc[0].get("applicationId") != sp.application_id:
                    raise Stop(f"{n}: the account view doesn't show the identity just created (id {sp.id})")
                sp_ids[p], apps[p] = str(sp.id), sp.application_id
                origin = "created"
            else:
                sp_ids[p], apps[p] = str(x["id"]), x["applicationId"]
                origin = "created" if x.get("_origin") != "marked" else "marked"
                self._ensure_entitlements(sp_ids[p], n)
                self.log(f"reusing service principal {n} ({origin})")
            st.set_binding("service_principal", p, n, sp_ids[p], apps[p], origin)
        # 3. account groups through the proxy
        for role in GROUP_ROLES:
            act, x, n = plan[("group", role)]
            if act == "create":
                try:
                    g = self.api("POST", f"{ACCOUNT_SCIM}/Groups",
                                 body={"schemas": [GROUP_SCHEMA], "displayName": n, "externalId": identity_marker(n)})
                except Exception as e:  # noqa: BLE001
                    raise (Refused(f"create account group {n}", first_line(e, 200)) if is_denied(e) else e)
                st.add_receipt("group", role, n, g["id"], "",
                               {"confirmed_absent_before": True, "marker": identity_marker(n),
                                "marker_in_create_response": (g or {}).get("externalId") == identity_marker(n),
                                "created_by": me, "created_at": str(now()), "via": "account SCIM proxy"})
                self.log(f"created account group {n} (receipt recorded)")
                group_ids[role], origin = str(g["id"]), "created"
            else:
                group_ids[role] = str(x["id"])
                origin = "created" if x.get("_origin") != "marked" else "marked"
                self.log(f"reusing account group {n} ({origin})")
            st.set_binding("group", role, n, group_ids[role], "", origin)
        # 4. membership
        for p, roles in PERSONA_GROUPS.items():
            for role in roles:
                self._add_member(group_ids[role], sp_ids[p], ids["groups"][role], ids["service_principals"][p])
        # 5. workspace assignment of the groups (the SPs were assigned by the workspace create)
        for role in GROUP_ROLES:
            try:
                retry(lambda gid=group_ids[role]: self.api(
                    "PUT", f"/api/2.0/preview/permissionassignments/principals/{gid}", body={"permissions": ["USER"]}),
                    tries=6, sleep=self.sleep)
            except Exception as e:  # noqa: BLE001
                raise (Refused(f"assign {ids['groups'][role]} to the workspace", first_line(e, 200)) if is_denied(e) else e)
        # 6. Service Principal User role for the caller (to set run_as)
        for p in PERSONAS:
            self._grant_sp_user(apps[p], ids["service_principals"][p])
        self.log("personas ready: " + json.dumps({"service_principals": ids["service_principals"],
                                                    "groups": ids["groups"]}, indent=1))
        return {"apps": apps, "groups": dict(ids["groups"])}

    def _ensure_entitlements(self, scim_id, n):
        from databricks.sdk.service.iam import Patch, PatchOp, PatchSchema
        try:
            self.w.service_principals.patch(scim_id, schemas=[PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP],
                                            operations=[Patch(op=PatchOp.ADD, path="entitlements", value=[
                                                {"value": "workspace-access"}, {"value": "databricks-sql-access"}])])
        except Exception as e:  # noqa: BLE001
            raise (Refused(f"entitlements for {n}", first_line(e, 200)) if is_denied(e) else e)

    def _add_member(self, gid, sp_id, gname, spname):
        body = {"schemas": [PATCH_SCHEMA], "Operations": [{"op": "add", "path": "members", "value": [{"value": sp_id}]}]}

        def members():
            g = self.api("GET", f"{ACCOUNT_SCIM}/Groups/{gid}") or {}
            return {str(m.get("value")) for m in g.get("members") or []}
        try:
            if sp_id in members():
                return
            retry(lambda: self.api("PATCH", f"{ACCOUNT_SCIM}/Groups/{gid}", body=body), tries=6, sleep=self.sleep)
        except Exception as e:  # noqa: BLE001
            raise (Refused(f"add {spname} to {gname}", first_line(e, 200)) if is_denied(e) else e)
        if sp_id not in retry(members, sleep=self.sleep, ok=lambda m: sp_id in m):
            raise Stop(f"{spname} is not shown as a member of {gname} after the PATCH")
        self.log(f"member: {spname} -> {gname}")

    def _grant_sp_user(self, app, spname):
        from databricks.sdk.service.iam import GrantRule, RuleSetUpdateRequest
        rs_name = f"accounts/{self.cfg.account_id}/servicePrincipals/{app}/ruleSets/default"
        who = f"users/{self.me.user_name}"
        acp = self.w.account_access_control_proxy

        def grant():
            rs = acp.get_rule_set(name=rs_name, etag="")
            rules = list(rs.grant_rules or [])
            if any(r.role == SP_USER_ROLE and who in (r.principals or []) for r in rules):
                return "held"
            rules.append(GrantRule(role=SP_USER_ROLE, principals=[who]))
            acp.update_rule_set(name=rs_name, rule_set=RuleSetUpdateRequest(name=rs_name, etag=rs.etag, grant_rules=rules))
            return "granted"
        try:
            state = retry(grant, tries=8, sleep=self.sleep)
        except Exception as e:  # noqa: BLE001
            raise (Refused(f"Service Principal User role on {spname}", first_line(e, 200)) if is_denied(e) else e)
        self.log(f"Service Principal User on {spname} for {self.me.user_name}: {state}")

    def _workspace_group(self, gname):
        """The group as the workspace sees it (no account SCIM read needed). Must be an ACCOUNT group."""
        found = [g for g in self.w.groups.list(filter=f'displayName eq "{gname}"') if g.display_name == gname]
        if len(found) != 1:
            return None, f"{len(found)} workspace-visible groups named {gname}"
        g = found[0]
        rtype = getattr(getattr(g, "meta", None), "resource_type", None)
        if rtype not in (None, "Group"):
            return None, f"{gname} is a {rtype} (workspace-local), not an account group"
        return g, ""

    def _manual_personas(self, st: IdentityStore) -> dict:
        """Bind provided identities. Receipts are never touched; values must be distinct; works without any account
        SCIM read (workspace SCIM shows assigned account identities)."""
        errs = manual_identity_errors(self.cfg.manual_app_ids, self.cfg.manual_groups)
        if errs:
            raise Stop("; ".join(errs))
        apps = dict(zip(PERSONAS, self.cfg.manual_app_ids))
        groups = dict(zip(GROUP_ROLES, self.cfg.manual_groups))
        resolved_sp, resolved_g = {}, {}
        for p, a in apps.items():
            sp = next(iter(self.w.service_principals.list(filter=f'applicationId eq "{a}"')), None)
            if not sp or (sp.application_id or "").lower() != a:
                raise Stop(f"manual_app_ids: no service principal with application ID {a[:8]}… is assigned to this workspace")
            resolved_sp[p] = sp
        if len({str(sp.id) for sp in resolved_sp.values()}) != len(PERSONAS):
            raise Stop("manual_app_ids resolve to fewer than three different service principals")
        for role, gname in groups.items():
            g, why = self._workspace_group(gname)
            if g is None:
                raise Stop(f"manual_groups: {why}")
            resolved_g[role] = g
        if len({str(g.id) for g in resolved_g.values()}) != len(GROUP_ROLES):
            raise Stop("manual_groups resolve to fewer than four different groups")
        for p, sp in resolved_sp.items():
            st.set_binding("service_principal", p, sp.display_name, sp.id, apps[p], "provided",
                           "manual_app_ids; never deleted by the lab")
        for role, g in resolved_g.items():
            st.set_binding("group", role, g.display_name, g.id, "", "provided", "manual_groups; never deleted by the lab")
        self.log("bound the provided identities (never deleted by teardown; earlier receipts kept)")
        return {"apps": apps, "groups": groups}

    def personas(self, st: IdentityStore | None = None) -> dict:
        """{'apps': {persona: app ID}, 'groups': {role: group name}} from this binding's bindings."""
        st = st or self.store()
        if not st.open(create=False):
            raise Stop("no identity store for this deployment yet: run action=personas first")
        b = st.bindings()
        apps = {r["role"]: r["application_id"] for r in b if r["kind"] == "service_principal"}
        groups = {r["role"]: r["display_name"] for r in b if r["kind"] == "group"}
        if set(apps) != set(PERSONAS) or set(groups) != set(GROUP_ROLES):
            raise Stop(f"personas not bound for this deployment ({len(apps)} service principals, {len(groups)} groups). "
                       "Run action=personas first.")
        return {"apps": apps, "groups": groups}

    # ---- run-time bindings -------------------------------------------------------------------
    def ensure_run_as(self, job_id: int, app: str, persona: str) -> str:
        from databricks.sdk.service.jobs import JobRunAs, JobSettings
        app_id(app)
        if getattr(self.w.jobs.get(job_id), "run_as_user_name", None) == app:
            return "already bound"
        self.w.jobs.update(job_id, new_settings=JobSettings(run_as=JobRunAs(service_principal_name=app)))
        retry(lambda: self.w.jobs.get(job_id).run_as_user_name, sleep=self.sleep, ok=lambda v: v == app)
        if self.w.jobs.get(job_id).run_as_user_name != app:
            raise Stop(f"run_as of job {job_id} ({persona}) is not the persona after the update")
        return "bound"

    def require_deployed_files(self, dep: Deployment, job_keys: list | None = None):
        """Every notebook the given jobs (default: all of the deployment's jobs) run must exist before any of them
        starts. After a teardown-apply, a CLI deploy that reuses its local bundle cache (.databricks/bundle/<target>)
        skips uploading files the cache still lists, and the jobs then fail on a missing notebook."""
        keys = list(job_keys) if job_keys is not None else sorted(dep.jobs)
        paths = []
        for k in keys:
            jid = dep.jobs.get(k)
            if not jid:
                raise Stop(f"the deployment has no {k} job")
            try:
                tasks = self.w.jobs.get(int(jid)).settings.tasks or []
            except Exception as e:  # noqa: BLE001
                raise Stop(f"couldn't read job {k} ({jid}): {first_line(e, 150)}")
            paths += [t.notebook_task.notebook_path for t in tasks if getattr(t, "notebook_task", None)
                      and t.notebook_task.notebook_path]
        missing = []
        for nb in sorted(set(paths)):
            try:
                self.w.workspace.get_status(ws_path(nb))
            except Exception as e:  # noqa: BLE001
                if not is_not_found(e):
                    raise Stop(f"couldn't check the deployment's notebook {nb}: {first_line(e, 150)}")
                missing.append(nb)
        if missing:
            raise Stop(f"the deployment's notebooks are missing ({', '.join(missing)}): its jobs can't run. Deploy "
                       "the bundle again with a fresh file sync (CLI: remove the local .databricks/bundle/<target> "
                       "folder first), then rerun.")

    def _source_object(self):
        root = self.deployment().source_root
        if not root:
            raise Stop("couldn't tell where the deployed notebooks live (no notebook path on the audit job)")
        info = self.w.workspace.get_status(ws_path(root))
        otype = {"REPO": "repos", "DIRECTORY": "directories"}.get(getattr(info.object_type, "value", str(info.object_type)))
        if not otype:
            raise Stop(f"{root} is a {info.object_type}, not a folder")
        return root, otype, str(info.object_id)

    def sp_direct_levels(self, otype, oid, app):
        """The SP's direct (non-inherited) permission levels on an object; None after a structured 404."""
        try:
            acl = self.w.permissions.get(otype, oid).access_control_list or []
        except Exception as e:  # noqa: BLE001
            if is_not_found(e):
                return None
            raise
        return [getattr(p.permission_level, "value", str(p.permission_level)) for a in acl
                if a.service_principal_name == app for p in (a.all_permissions or []) if not p.inherited]

    def grant_source_read(self, apps: dict, st: IdentityStore) -> list:
        """CAN_READ for each persona SP on the folder that holds the notebooks, with a before/after receipt."""
        from databricks.sdk.service.iam import AccessControlRequest, PermissionLevel
        root, otype, oid = self._source_object()
        added = []
        for p, a in apps.items():
            before = self.sp_direct_levels(otype, oid, a) or []
            if before:
                continue                                  # already has a direct entry: not the lab's, not recorded
            self.w.permissions.update(otype, oid, access_control_list=[
                AccessControlRequest(service_principal_name=a, permission_level=PermissionLevel.CAN_READ)])
            after = sorted(retry(lambda a=a: self.sp_direct_levels(otype, oid, a) or [], sleep=self.sleep,
                                 ok=lambda v: v == ["CAN_READ"]))
            if after != ["CAN_READ"]:
                raise Stop(f"CAN_READ on {root} for {p} not visible as exactly CAN_READ (got {after})")
            st.add_receipt("acl", f"{otype}|{oid}|{p}", a, a, a,
                           {"confirmed_absent_before": True, "before": [], "after": ["CAN_READ"], "object": root,
                            "created_at": str(now())})
            added.append(p)
        return added

    # ---- jobs --------------------------------------------------------------------------------
    def run_job(self, key: str, params: dict) -> dict:
        jid = int(self.deployment().jobs[key])
        defined = {p.name for p in (self.w.jobs.get(jid).settings.parameters or [])}
        jp = job_parameters(defined, params)
        started = self.w.jobs.run_now(job_id=jid, job_parameters=jp)
        run_id = getattr(getattr(started, "response", None), "run_id", None) or getattr(started, "run_id", None)
        deadline = time.time() + JOB_TIMEOUT.get(key, 1800)
        run = self.w.jobs.get_run(run_id)
        self.log(f"{key} ({self.names['jobs'][key]}): run {run_id} {run.run_page_url}")
        while True:
            life = getattr(run.state.life_cycle_state, "value", str(run.state.life_cycle_state))
            if life in ("TERMINATED", "SKIPPED", "INTERNAL_ERROR"):
                break
            if time.time() > deadline:
                self.w.jobs.cancel_run(run_id)
                return {"run_id": run_id, "state": "TIMEOUT", "url": run.run_page_url, "message": "timed out"}
            self.sleep(POLL_S)
            run = self.w.jobs.get_run(run_id)
        res = getattr(run.state.result_state, "value", None) if run.state.result_state else None
        msg = (run.state.state_message or "")[:300]
        error = ""
        if res != "SUCCESS":
            try:
                error = (self.w.jobs.get_run_output(run.tasks[0].run_id).error or "")[:1500]
            except Exception:  # noqa: BLE001
                error = ""
        self.log(f"{key}: {life} {res or ''} {msg} {error[:200]}")
        return {"run_id": run_id, "state": res or life, "url": run.run_page_url, "message": msg, "error": error,
                "run": run}

    def steps_table(self) -> str:
        return f"{self.cfg.full_schema}.{STEPS_T}"

    def ensure_steps_table(self):
        t = self.steps_table()
        if self.tables.exists(t):
            if self.tables.props(t).get(KIND_PROP) != "steps":
                raise Stop(f"{t} exists but wasn't created by this runner: not used")
            return
        self.tables.create(t, STEPS_COLS, "GenAI governance lab: 00_run_lab step log", {KIND_PROP: "steps"})

    def record_step(self, run_id, step, key, job_run_id, state, detail=""):
        dep = self.deployment()
        self.tables.insert(self.steps_table(), {"run_id": run_id, "step": step, "job_key": key,
                                                "job_run_id": str(job_run_id or ""), "state": state,
                                                "detail": str(detail)[:1000], "ts": now().isoformat(),
                                                "generation": self._gen.get(dep.root, "")})

    def done_steps(self, run_id) -> set:
        if not self.tables.exists(self.steps_table()):
            return set()
        rows = self.tables.rows(self.steps_table(), {"run_id": run_id})
        return {s for s, st in latest_states(rows).items() if st == "SUCCESS"}

    def deployed_param_default(self, key: str, param: str) -> str:
        jid = self.deployment().jobs.get(key)
        if not jid:
            return ""
        for p in self.w.jobs.get(int(jid)).settings.parameters or []:
            if p.name == param:
                return p.default or ""
        return ""

    def base_params(self, suite: str, groups: dict | None) -> dict:
        cfg = self.cfg
        g = groups or {}
        return {"suite_run_id": suite, "warehouse_id": cfg.warehouse_id, "vs_endpoint": cfg.vs_endpoint,
                "agent_endpoint": cfg.agent_endpoint, "ka_endpoint": cfg.ka_endpoint,
                "expected_policy": cfg.expected_policy,
                "approved_builders": list_param(cfg.approved_builders,
                                                self.deployed_param_default("lab_audit", "approved_builders"),
                                                g.get("clinicians", "")),
                "cleartext_groups": list_param(cfg.cleartext_groups,
                                               self.deployed_param_default("lab_audit", "cleartext_groups"),
                                               g.get("pii_readers", "")),
                "usage_lookback_days": cfg.usage_lookback_days,
                "clinicians_group": g.get("clinicians"), "pii_readers_group": g.get("pii_readers"),
                "all_facilities_group": g.get("all_facilities"), "outsiders_group": g.get("outsiders")}

    def record_guard_skips(self, persona: str, reason: str, suite: str) -> int:
        """Trusted SKIP rows (written by the runner, not by the wrongly-running job) for a refused persona job."""
        rows = guard_skip_rows(persona, reason, suite, load_catalogue())
        t = f"{self.cfg.full_schema}.test_results"
        for r in rows:
            self.tables.insert(t, {c: r[c] for c in RESULTS_COLS})      # typed: layer INT, ts TIMESTAMP
        return len(rows)

    def run(self, resume_id: str = "") -> str:
        self.require_admin()
        dep = self.deployment()
        mode = dep.mode
        self.require_deployed_files(dep, [k for _, k in run_plan(mode)])
        st = self.store() if mode == "demo" else None
        ident = self.personas(st) if mode == "demo" else None
        suite = resume_id or new_suite_id()
        self.generation(dep, create=True)   # audit-mode runs get a generation too
        self.ensure_steps_table()
        self.mark_generation(dep, "run", suite)
        if mode == "audit":
            self.record_results_absence(dep)
        try:
            return self._run_steps(dep, mode, suite, resume_id, ident, st)
        finally:
            if mode == "audit":
                self.record_results_ids(dep)

    def _run_steps(self, dep, mode, suite, resume_id, ident, st) -> str:
        plan = run_plan(mode)
        done = self.done_steps(suite) if resume_id else set()
        todo = pending_steps(plan, done)
        self.log(f"== suite run {suite} ({mode}); steps: {[s for s, _ in plan]}; rerunning: {[s for s, _ in todo]}")
        if resume_id and not todo:
            self.log("nothing left to run for this suite")
            return suite
        params = self.base_params(suite, ident["groups"] if ident else None)
        jobs = persona_jobs(self.w, dep) if mode == "demo" else {}
        if mode == "demo":
            added = self.grant_source_read(ident["apps"], st)
            self.log(f"notebook read access for the personas: {len(added)} added")
        for step, key in todo:
            p = dict(params)
            persona = key[len("lab_persona_"):] if key.startswith("lab_persona_") else None
            if persona:
                app = ident["apps"][persona]
                self.log(f"run_as {persona}: {self.ensure_run_as(jobs[persona], app, persona)}")   # every run
                p["expected_principal"] = app
            r = self.run_job(key, p)
            self.record_step(suite, step, key, r["run_id"], r["state"], r.get("error") or r.get("message", ""))
            if r["state"] == "SUCCESS":
                continue
            if persona and GUARD_MARK in (r.get("error") or ""):
                reason = (r["error"].split(GUARD_MARK, 1)[1].strip(" :") or "wrong identity")[:500]
                n = self.record_guard_skips(persona, f"persona job refused to run: {reason}", suite)
                raise Stop(f"{key} refused to run as the wrong identity ({reason}); the runner recorded {n} SKIP "
                           f"result(s). Fix run_as, then action=resume run_id={suite} ({r['url']})")
            raise Stop(f"{key} ended {r['state']}; later steps (collect, audit) were not run. Fix it, then "
                       f"action=resume run_id={suite} ({r['url']})")
        self.log(f"== done: suite {suite}. action=results shows it; action=evidence the next day.")
        return suite

    # ---- results / evidence ------------------------------------------------------------------
    def dashboard_json(self) -> dict:
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "..", "dashboards", "results.lvdash.json")) as f:
            return json.load(f)

    def result_rows(self):
        if not self.tables.exists(f"{self.cfg.full_schema}.suite_runs"):
            raise Stop(f"no suite has run in {self.cfg.full_schema} yet (action=run)")
        self.spark.sql(f"USE CATALOG {q(self.cfg.catalog)}")
        self.spark.sql(f"USE SCHEMA {q(self.cfg.schema)}")
        return self.spark.sql(results_query(self.dashboard_json()))

    def results(self) -> dict:
        dep = self.deployment()                    # deployment and mode are checked before anything is read
        df = self.result_rows()
        rows = [r.asDict() for r in df.collect()]
        s = summarize_results(rows)
        self.log(f"latest suite {s['suite']} ({s['mode']}): " + ", ".join(f"{k} {v}" for k, v in s["counts"].items()))
        self.log(f"MISSING ({len(s['missing'])}): {', '.join(s['missing']) or 'none'}")
        self.log(f"PENDING ({len(s['pending'])}): {', '.join(s['pending']) or 'none'} (evidence arrives the next day)")
        self.display(df.select("test_id", "persona", "status", "expected", "observed", "title", "suite_run_id"))
        dash = dep.dashboards.get("lab_results")
        if dash:
            s["dashboard"] = f"{self.host}/dashboardsv3/{dash}/published"
            self.log(f"dashboard: {s['dashboard']}")
        return s

    def evidence(self) -> dict:
        self.require_admin()
        ident = self.personas()
        self.require_deployed_files(self.deployment(), ["lab_evidence"])
        params = {"suite_run_id": new_suite_id(), "evidence_run_id": self.cfg.run_id,
                  "persona_apps": ",".join(ident["apps"][p] for p in PERSONAS)}
        r = self.run_job("lab_evidence", params)
        if r["state"] != "SUCCESS":
            raise Stop(f"lab_evidence ended {r['state']} ({r['url']})")
        return self.results()

    # ---- export ------------------------------------------------------------------------------
    def export(self, dep: Deployment) -> dict:
        rows = self.result_rows().collect()
        if not rows:
            raise Stop("the results query returned no rows; nothing to export")
        cols = list(rows[0].asDict().keys())
        buf = io.StringIO()
        wr = csv.writer(buf)
        wr.writerow(cols)
        for r in rows:
            d = r.asDict()
            wr.writerow(["" if d[c] is None else d[c] for c in cols])
        text = buf.getvalue()
        suites = sorted({r.suite_run_id for r in rows if r.suite_run_id})
        folder = f"/Workspace/Users/{self.me.user_name}/{EXPORT_DIR}"
        base = f"{folder}/results-{self.cfg.full_schema}-{suites[0] if len(suites) == 1 else 'mixed'}"
        out, n = base + ".csv", 1
        while self._exists(out):
            n += 1
            out = f"{base}-{n}.csv"
        digest = sha256(text)
        self.write_text(out, text)
        meta = {"workspace_id": self.workspace_id(), "deployment": dep.root, "catalog": self.cfg.catalog,
                "schema": self.cfg.schema, "suite_run_ids": suites, "rows": len(rows), "sha256": digest}
        self.write_text(out[:-4] + ".meta.json", json.dumps(meta, indent=1))
        if sha256(self.read_text(out)) != digest:
            raise Stop(f"export {out} doesn't match its checksum after writing")
        self.log(f"exported {len(rows)} rows (suite {suites}) to {out} (sha256 verified)")
        return {"path": out, "sha256": digest, "rows": len(rows), "suites": suites}

    def export_verified(self, exp: dict | None) -> bool:
        if not exp or not exp.get("path"):
            return False
        try:
            return sha256(self.read_text(exp["path"])) == exp.get("sha256")
        except Exception:  # noqa: BLE001
            return False

    def _exists(self, path) -> bool:
        try:
            self.read_text(path)
            return True
        except Exception:  # noqa: BLE001
            return False

    # ---- teardown checkpoint -----------------------------------------------------------------
    def home_export_dir(self) -> str:
        return f"/Workspace/Users/{self.me.user_name}/{EXPORT_DIR}"

    def _slug(self) -> str:
        return binding_slug(self.workspace_id(), self.cfg)

    def checkpoint_paths(self, dep: Deployment, generation: str) -> list:
        """[deployment-folder copy, home-folder copy]; the home copy is named by binding AND generation."""
        return [f"{dep.root}/{RUNNER_DIR}/teardown_checkpoint.json",
                f"{self.home_export_dir()}/{checkpoint_file(self._slug(), generation)}"]

    def load_checkpoint(self, paths: list):
        """The first checkpoint that exists (unsealed). Missing only on a provable not-found; a present but unreadable
        or tampered one stops."""
        for p in paths:
            text = self._read_optional(p, "teardown checkpoint")
            if text is not None:
                return unseal(text, f"teardown checkpoint {p}")
        return None

    def save_checkpoint(self, cp: dict, dep: Deployment, root_too: bool = True):
        cp["updated_at"] = str(now())
        text = seal(cp)
        for p in self.checkpoint_paths(dep, cp["generation"])[(0 if root_too else 1):]:
            self.write_text(p, text)

    def home_checkpoints(self) -> list:
        """[(path, checkpoint)] of this binding's generation-named checkpoints in the home folder."""
        prefix = f"teardown-checkpoint-{self._slug()}-"
        out = []
        for p in sorted(self.list_dir(self.home_export_dir())):
            fn = posixpath.basename(p)
            if not (fn.startswith(prefix) and fn.endswith(".json")):
                continue                  # includes pre-generation checkpoints: never reused
            gen = fn[len(prefix):-len(".json")]
            cp = self.load_checkpoint([p])
            if cp is None:
                continue
            if cp.get("generation") != gen:
                raise Stop(f"teardown checkpoint {p} holds generation {cp.get('generation')!r}, not the {gen!r} in "
                           "its name: not trusted")
            out.append((p, cp))
        return out

    def _newer_generation(self, cp: dict) -> str | None:
        """Evidence that the deployment moved on after cp's generation: a generation record in its folder or receipts
        in its identity store with another generation. None if there is none."""
        d = Deployment(**cp["deployment"])
        text = self._read_optional(f"{d.root}/{RUNNER_DIR}/{GEN_FILE}", "generation record")
        if text is not None:
            g = unseal(text, "generation record").get("generation")
            if g != cp["generation"]:
                return f"the deployment folder {d.root} now has generation {g}"
        t = f"{self.cfg.full_schema}.{RECEIPTS_T}"
        if self.tables.exists(t):
            other = {r.get("generation") for r in self.tables.rows(t, {"binding": cp["binding"]})} - {cp["generation"]}
            if other:
                return f"{t} holds receipts of generation(s) {sorted(str(x) for x in other)}"
        return None

    def _teardown_context(self, apply: bool):
        """(deployment, checkpoint, generation).
        * Live deployment: its generation (created now for teardown-apply if it has none); only a checkpoint of that
          generation and binding is reused, anything else stops.
        * No deployment found any more (its jobs are already gone): resume the single unfinished checkpoint of this
          binding in the home folder, unless a newer generation exists; otherwise stop."""
        try:
            dep = self.deployment()
        except NoDeployment:
            open_cps = [(p, cp) for p, cp in self.home_checkpoints() if not cp.get("complete")]
            if not open_cps:
                raise
            if len(open_cps) > 1:
                raise Stop("several unfinished teardown checkpoints for this lab binding: "
                           + ", ".join(p for p, _ in open_cps) + ". Resolve by hand.")
            path, cp = open_cps[0]
            dep = Deployment(**cp["deployment"])
            if cp.get("binding") != make_binding(self.cfg.account_id, self.workspace_id(), dep.target, dep.root,
                                                 self.cfg, dep.mode):
                raise Stop(f"teardown checkpoint {path} belongs to another binding: not used")
            newer = self._newer_generation(cp)
            if newer:
                raise Stop(f"teardown checkpoint {path} (generation {cp['generation']}) is not resumed: {newer}")
            self._dep = dep
            self._gen[dep.root] = cp["generation"]
            self.log(f"deployment no longer discoverable; resuming teardown of generation {cp['generation']} from "
                     f"{path} ({dep.root})")
            return dep, cp, cp["generation"]
        gen = self.generation(dep, create=apply)
        binding = self.binding(dep)
        paths = self.checkpoint_paths(dep, gen) if gen else [f"{dep.root}/{RUNNER_DIR}/teardown_checkpoint.json"]
        cp = self.load_checkpoint(paths)
        why = checkpoint_reusable(cp, binding, gen)
        if why:
            raise Stop(f"the teardown checkpoint for {dep.root} is not reused: {why}. Inspect it (and remove it if it "
                       "is stale), then rerun.")
        others = [p for p, c in self.home_checkpoints() if c.get("generation") != gen and not c.get("complete")]
        if others:
            self.log("NOTE: unfinished teardown checkpoints of earlier generations (not used here; review them): "
                     + ", ".join(others))
        return dep, cp, gen

    # ---- teardown ----------------------------------------------------------------------------
    def bundle_resources(self, dep: Deployment) -> list:
        """[(kind, id)] this deployment recorded, in deletion order. The audit job (used to find the deployment) is the
        last job, the deployment folder (holding the checkpoint) the very last item."""
        cfg = self.cfg
        out = [("model_service", v) for v in dep.model_services.values() if v.startswith(cfg.full_schema + ".")]
        out += [("dashboard", v) for v in dep.dashboards.values()]
        out += [("runner_tables", cfg.full_schema)]
        for key in ("lab_tools", "lab"):
            v = dep.schemas.get(key)
            if v and v in (cfg.full_schema, cfg.tools_schema):
                out.append(("schema", v))
        out += [("job", v) for k, v in sorted(dep.jobs.items()) if k != "lab_audit"]
        if dep.jobs.get("lab_audit"):
            out.append(("job", dep.jobs["lab_audit"]))
        out.append(("bundle_folder", dep.root))
        return out

    def teardown(self, apply: bool) -> dict:
        self.require_admin()
        dep, cp, gen = self._teardown_context(apply)
        binding = self.binding(dep)
        groups = None
        st = self.store(dep) if dep.mode == "demo" else None
        store_ok = False
        if st is not None:
            try:
                store_ok = st.open(create=False)
            except Stop:
                if not (cp and cp.get("phases", {}).get("identities") == "done"):
                    raise
        if store_ok:
            try:
                groups = self.personas(st)["groups"]
            except Stop:
                groups = None
        lab_groups = ",".join(groups[r] for r in ("clinicians", "pii_readers", "outsiders")) if groups else ""
        if not apply:
            report = {"apply": False, "generation": gen, "checkpoint": cp and cp.get("phases")}
            if dep.mode == "demo" and dep.jobs.get("lab_teardown") and not (cp and cp["phases"].get("lab_teardown")):
                self.require_deployed_files(dep, ["lab_teardown"])
                report["lab_teardown"] = self._notebook_result(
                    self.run_job("lab_teardown", {"dry_run": "true", "lab_groups": lab_groups}))
            receipts = (st.receipts() if store_ok else (cp or {}).get("receipts", []))
            report["identities"] = [(r["kind"], r["display_name"], *self._receipt_decision(r))
                                    for r in receipts if r["state"] == "active"]
            if dep.mode == "audit":
                proofs = (cp or {}).get("results_tables") or ((self.generation_record(dep) or {}).get("results_tables")
                                                             or {})
                report["audit_results"] = self.audit_results_decisions(proofs)
            report["bundle_resources"] = self.bundle_resources(dep)
            self.log("TEARDOWN PLAN (nothing changed):\n" + json.dumps(report, indent=1, default=str))
            return report
        cp = cp or {"version": 2, "binding": binding, "generation": gen, "deployment": dep.as_dict(), "phases": {},
                    "receipts": [], "bundle_resources": self.bundle_resources(dep)}
        phases = cp["phases"]
        # 1. export (a verified checkpoint export is reused: the results tables may already be gone). A deployment
        #    generation that never ran a suite (neither results table exists) has nothing to export; anything else
        #    must export, or the teardown stops.
        if not (self.export_verified(cp.get("export")) or (cp.get("export") or {}).get("nothing_to_export")):
            if any(self.tables.exists(f"{self.cfg.full_schema}.{t}") for t in ("suite_runs", "test_results")):
                cp["export"] = self.export(dep)
            else:
                why = self.suite_may_have_run(dep, gen)
                if why:
                    raise Stop(f"results missing for a generation that ran ({why}); restore or confirm manually. "
                               "Nothing was removed.")
                cp["export"] = {"nothing_to_export": True, "note": "no suite ran in this deployment generation"}
                self.log("export: no suite ran in this deployment generation (generation record and step log show "
                         "no run; no results tables): nothing to export")
        phases["export"] = "done"
        if store_ok:
            cp["receipts"] = st.receipts()
        self.save_checkpoint(cp, dep)
        # 2. the deployment's own teardown job (manifest items)
        if dep.mode == "demo" and phases.get("lab_teardown") != "done":
            self.require_deployed_files(dep, ["lab_teardown"])
            r = self.run_job("lab_teardown", {"dry_run": "false", "lab_groups": lab_groups})
            cp["lab_teardown"] = self._notebook_result(r)
            if r["state"] != "SUCCESS":
                self.save_checkpoint(cp, dep)
                raise Stop(f"lab_teardown ended {r['state']}: nothing else removed. Fix and rerun ({r['url']})")
            phases["lab_teardown"] = "done"
            self.save_checkpoint(cp, dep)
        # 2b. audit-only deployments: the results tables the audit job created (proven by UC table ID), resumable
        if dep.mode == "audit" and phases.get("audit_results") != "done":
            if "results_tables" not in cp:
                cp["results_tables"] = (self.generation_record(dep) or {}).get("results_tables") or {}
            manual = self.teardown_audit_results(cp)
            cp["audit_results_manual"] = manual
            if manual:
                phases["audit_results"] = "manual"
                self.save_checkpoint(cp, dep)
                raise Stop("audit results tables without exact creation proof are kept (manual review); nothing else "
                           "was removed:\n  " + "\n  ".join(manual) + "\nReview them, drop them by hand if they are "
                           "the lab's, then rerun teardown-apply.")
            phases["audit_results"] = "done"
            self.save_checkpoint(cp, dep)
        # 3. ACL entries and identities from the receipts (live store, or the checkpoint copy)
        if dep.mode == "demo" and phases.get("identities") != "done":
            unresolved = self.teardown_identities(st if store_ok else None, cp)
            cp["identities_unresolved"] = unresolved
            if unresolved:
                phases["identities"] = "unresolved"
                self.save_checkpoint(cp, dep)
                raise Stop(f"{len(unresolved)} receipt(s) unresolved and kept: {unresolved}. Schemas, jobs and the "
                           "checkpoint are kept; resolve them and rerun teardown-apply.")
            phases["identities"] = "done"
            self.save_checkpoint(cp, dep)
        # 4. the deployment's bundle resources (only those its bundle state recorded)
        done = phases.setdefault("destroyed", [])
        for kind, rid in cp["bundle_resources"]:
            key = f"{kind}:{rid}"
            if key in done:
                continue
            self._destroy(kind, rid, cp, st if store_ok else None)
            done.append(key)
            if kind == "bundle_folder":
                cp["complete"] = True                          # only after the folder is verifiably gone
                self.save_checkpoint(cp, dep, root_too=False)  # the root copy went with the folder; home copy remains
            else:
                self.save_checkpoint(cp, dep)
            self.log(f"deleted (verified): {kind} {rid}")
        self.log("TEARDOWN DONE:\n" + json.dumps({"export": cp["export"], "phases": phases}, indent=1, default=str))
        return {"export": cp["export"], "phases": phases, "lab_teardown": cp.get("lab_teardown"),
                "identities_unresolved": cp.get("identities_unresolved", [])}

    def suite_may_have_run(self, dep: Deployment, gen: str | None) -> str | None:
        """None only with POSITIVE evidence that no suite ran in this generation: its generation record exists and
        records no run, and the runner's step log records no step (it exists, empty, once personas ran). Otherwise the
        reason a run can't be ruled out."""
        try:
            rec = self.generation_record(dep)
        except Stop as e:
            return f"generation record unreadable: {e}"
        if rec is None or not gen or rec.get("generation") != gen:
            return "no generation record for this deployment generation"
        actions = {a.get("action") for a in rec.get("actions", []) if isinstance(a, dict)}
        if "run" in actions:
            return "its generation record shows a run started"
        t = self.steps_table()
        try:
            exists = self.tables.exists(t)
            if exists and self.tables.props(t).get(KIND_PROP) != "steps":
                return f"{t} is not the runner's step log"
            rows = self.tables.rows(t) if exists else []
        except Exception as e:  # noqa: BLE001
            return f"the step log {t} is unreadable ({first_line(e, 120)})"
        if rows:
            return f"the step log {t} records {len(rows)} step(s)"
        if not exists and actions:
            return f"the step log {t} is missing although {', '.join(sorted(actions))} ran"
        if exists and not actions:
            return f"the step log {t} exists but the generation record records no action that created it"
        return None

    def _notebook_result(self, r) -> dict:
        try:
            run = r.get("run") or self.w.jobs.get_run(r["run_id"])
            out = self.w.jobs.get_run_output(run.tasks[0].run_id).notebook_output
            return {"state": r["state"], "result": json.loads(out.result) if out and out.result else None, "url": r["url"]}
        except Exception as e:  # noqa: BLE001
            return {"state": r["state"], "url": r["url"], "note": first_line(e, 120)}

    def _identity_kind(self, rec) -> str:
        return {"service_principal": "ServicePrincipals", "group": "Groups"}.get(rec["kind"], "")

    def _receipt_decision(self, rec) -> tuple:
        if rec["kind"] == "acl":
            otype, oid, _ = rec["role"].split("|")
            try:
                return acl_teardown_action(rec, self.sp_direct_levels(otype, oid, rec["application_id"]))
            except Exception as e:  # noqa: BLE001
                return "unresolved", f"couldn't read the ACL: {first_line(e, 120)}"
        return identity_teardown_action(rec, self.account_get(self._identity_kind(rec), rec["scim_id"]))

    def teardown_identities(self, st: IdentityStore | None, cp: dict) -> list:
        """ACL entries first, then identities, from the receipts. Only exact matches are removed (verified)."""
        receipts = st.receipts() if st else cp.get("receipts", [])
        order = sorted((r for r in receipts if r["state"] == "active"), key=lambda r: r["kind"] != "acl")
        unresolved = []

        def mark(rec, state, note):
            rec["state"], rec["note"] = state, note
            for c in cp.get("receipts", []):
                if (c["kind"], c["role"], c["scim_id"]) == (rec["kind"], rec["role"], rec["scim_id"]):
                    c["state"], c["note"] = state, note
            if st:
                st.set_receipt_state(rec, state, note)
        for rec in order:
            label = f"{rec['kind']} {rec['display_name']}"
            act, why = self._receipt_decision(rec)
            if act == "gone":
                mark(rec, "deleted", why); self.log(f"already gone: {label} ({why})")
            elif act == "remove":
                ok, why2 = self._remove_acl_entry(rec)
                if ok:
                    mark(rec, "deleted", why2); self.log(f"removed CAN_READ entry (verified): {label}")
                else:
                    mark(rec, "unresolved", why2); unresolved.append(f"{label}: {why2}")
            elif act == "delete":
                kind = self._identity_kind(rec)
                try:
                    self.api("DELETE", f"{ACCOUNT_SCIM}/{kind}/{rec['scim_id']}")
                except Exception as e:  # noqa: BLE001
                    if not is_not_found(e):
                        mark(rec, "unresolved", f"delete failed: {first_line(e, 150)}")
                        unresolved.append(f"{label}: delete failed"); continue
                after = retry(lambda: self.account_get(kind, rec["scim_id"]), tries=12, sleep=self.sleep,
                              ok=lambda v: v is None)
                if after is None:
                    mark(rec, "deleted", "deleted; structured 404 verified"); self.log(f"deleted (verified 404): {label}")
                else:
                    mark(rec, "unresolved", "still present (or unreadable) after DELETE")
                    unresolved.append(f"{label}: not verified gone")
            else:
                mark(rec, "unresolved", why); self.log(f"UNRESOLVED {label}: {why}")
                unresolved.append(f"{label}: {why}")
        return unresolved

    def _remove_acl_entry(self, rec) -> tuple:
        """Remove exactly the recorded CAN_READ entry (PUT of every other direct entry, unchanged), then verify the SP
        has no direct entry and every other direct entry is still there."""
        from databricks.sdk.service.iam import AccessControlRequest
        otype, oid, _ = rec["role"].split("|")
        app = rec["application_id"]
        acl = self.w.permissions.get(otype, oid).access_control_list or []
        mine = sorted(getattr(p.permission_level, "value", "") for a in acl if a.service_principal_name == app
                      for p in (a.all_permissions or []) if not p.inherited)
        if mine != ["CAN_READ"]:
            return False, f"live entry changed to {mine} before removal"
        keep = [(a, p) for a in acl for p in (a.all_permissions or [])
                if not p.inherited and a.service_principal_name != app]
        others = [(a.user_name, a.group_name, a.service_principal_name, getattr(p.permission_level, "value", ""))
                  for a, p in keep]
        self.w.permissions.set(otype, oid, access_control_list=[
            AccessControlRequest(user_name=a.user_name, group_name=a.group_name,
                                 service_principal_name=a.service_principal_name, permission_level=p.permission_level)
            for a, p in keep])
        acl2 = self.w.permissions.get(otype, oid).access_control_list or []
        left = [getattr(p.permission_level, "value", "") for a in acl2 if a.service_principal_name == app
                for p in (a.all_permissions or []) if not p.inherited]
        others2 = sorted((a.user_name, a.group_name, a.service_principal_name, getattr(p.permission_level, "value", ""))
                         for a in acl2 for p in (a.all_permissions or []) if not p.inherited and a.service_principal_name != app)
        if left:
            return False, f"entry still present after removal: {left}"
        if others2 != sorted(others):
            return False, "other direct entries changed during the removal: manual review"
        return True, "exact CAN_READ entry removed; other entries unchanged (verified)"

    def schema_inventory(self, full: str) -> dict:
        """Everything still inside a schema: tables/views, functions, volumes, models, AI services, connections."""
        c, s = full.split(".")
        inv = {"table": [t.name for t in self.w.tables.list(catalog_name=c, schema_name=s)],
               "function": [f.name for f in self.w.functions.list(catalog_name=c, schema_name=s)],
               "volume": [v.name for v in self.w.volumes.list(catalog_name=c, schema_name=s)],
               "registered_model": [m.name for m in self.w.registered_models.list(catalog_name=c, schema_name=s)]}
        for kind in ("model-services", "mcp-services", "model-provider-services"):
            r = self.api("GET", f"{UC}/{kind}", query={"parent": f"schemas/{full}"}) or {}
            inv[kind.rstrip("s")] = [x.get("full_name") or x.get("name") for x in r.get(kind.replace("-", "_"), []) or []]
        inv["connection"] = [x.full_name for x in self.w.connections.list()
                             if (x.full_name or "").startswith(full + ".")]
        return {k: v for k, v in inv.items() if v}

    def _absent(self, get) -> bool:
        """True only if get() fails with a STRUCTURED not-found."""
        try:
            get()
            return False
        except Exception as e:  # noqa: BLE001
            return is_not_found(e)

    def _delete(self, delete, get, what):
        """Call delete(); if it errors, the object counts as already gone only if a GET proves it (structured 404).
        Deleting an already-deleted job, for example, fails with INVALID_PARAMETER_VALUE, which proves nothing."""
        try:
            delete()
        except Exception as e:  # noqa: BLE001
            if not (is_not_found(e) or self._absent(get)):
                raise Stop(f"couldn't delete {what}: {first_line(e, 150)}")

    def _gone(self, fn, what):
        def check():
            try:
                fn()
                return False
            except Exception as e:  # noqa: BLE001
                if is_not_found(e):
                    return True
                raise
        if not retry(check, tries=12, sleep=self.sleep, ok=lambda v: v):
            raise Stop(f"{what} still exists after delete (or isn't verifiably gone)")

    def _destroy(self, kind, rid, cp: dict, st: IdentityStore | None):
        cfg = self.cfg
        if kind == "model_service":
            [name(x) for x in rid.split(".")]
            try:
                svc = self.api("GET", f"{UC}/model-services/{rid}")
            except Exception as e:  # noqa: BLE001
                if not is_not_found(e):
                    raise
                return
            it = (svc.get("config") or {}).get("inference_table") or {}
            if it.get("parent") == f"schemas/{cfg.full_schema}" and it.get("table_name_prefix"):
                cp.setdefault("inference_tables", [])
                t = f"{cfg.full_schema}.{name(it['table_name_prefix'])}_payload"
                if t not in cp["inference_tables"]:
                    cp["inference_tables"].append(t)
            self._delete(lambda: self.api("DELETE", f"{UC}/model-services/{rid}"),
                         lambda: self.api("GET", f"{UC}/model-services/{rid}"), f"model service {rid}")
            self._gone(lambda: self.api("GET", f"{UC}/model-services/{rid}"), f"model service {rid}")
        elif kind == "dashboard":
            try:
                self.w.lakeview.trash(rid)
            except Exception as e:  # noqa: BLE001
                if is_not_found(e) or self._absent(lambda: self.w.lakeview.get(rid)):
                    return
                if getattr(getattr(self.w.lakeview.get(rid), "lifecycle_state", None), "value", None) != "TRASHED":
                    raise Stop(f"couldn't trash dashboard {rid}: {first_line(e, 150)}")
            state = getattr(self.w.lakeview.get(rid).lifecycle_state, "value", None)
            if state != "TRASHED":
                raise Stop(f"dashboard {rid} is {state} after trash")
        elif kind == "runner_tables":
            # The runner's own tables, each only if it is provably the runner's: step log (marker), identity store
            # (creation record), the model service's inference table (read from the service before it was deleted).
            if self.tables.exists(self.steps_table()):
                if self.tables.props(self.steps_table()).get(KIND_PROP) != "steps":
                    raise Stop(f"{self.steps_table()} has no runner marker: not dropped (manual review)")
                self.tables.drop(self.steps_table())
            if st:
                st.drop()
            for t in cp.get("inference_tables", []):
                if self.tables.exists(t):
                    self.tables.drop(t)
        elif kind == "schema":
            if rid not in (cfg.full_schema, cfg.tools_schema):
                raise Stop(f"refusing to drop {rid}: not this lab's schema")
            try:
                left = schema_leftovers(self.schema_inventory(rid))
            except Exception as e:  # noqa: BLE001
                if is_not_found(e):
                    return
                raise Stop(f"couldn't list the contents of {rid} ({first_line(e, 150)}): not dropped")
            if left:
                raise Stop(f"schema {rid} is not empty after the scoped cleanup; not dropped. Review and remove by "
                           "hand, then rerun teardown-apply:\n  " + "\n  ".join(left[:50]))
            c, s = rid.split(".")
            self.spark.sql(f"DROP SCHEMA {q(c)}.{q(s)}")          # never CASCADE: it is empty, verified above
            self._gone(lambda: self.w.schemas.get(rid), f"schema {rid}")
        elif kind == "job":
            self._delete(lambda: self.w.jobs.delete(int(rid)), lambda: self.w.jobs.get(int(rid)), f"job {rid}")
            self._gone(lambda: self.w.jobs.get(int(rid)), f"job {rid}")
        elif kind == "bundle_folder":
            root = ws_path(rid)
            home = f"/Users/{self.me.user_name}"
            if root.rstrip("/") in ("", "/Users", home) or len(root.strip("/").split("/")) < 3 or "/.bundle/" not in root:
                raise Stop(f"refusing to delete {rid}: not a bundle root")
            try:
                info = self.w.workspace.get_status(root)
            except Exception as e:  # noqa: BLE001
                if is_not_found(e):
                    return
                raise
            if getattr(info.object_type, "value", str(info.object_type)) != "DIRECTORY":
                raise Stop(f"refusing to delete {rid}: it is a {info.object_type}, not the bundle's own folder")
            self.w.workspace.delete(root, recursive=True)
            self._gone(lambda: self.w.workspace.get_status(root), f"folder {rid}")


def _fuse(path: str, root: str = "/Workspace") -> str:
    return path if path.startswith(root) else root + path


def _read_text_fuse(path: str, root: str = "/Workspace") -> str:
    p = _fuse(path, root)
    with open(p, newline="") as f:          # keep line endings exactly (the export checksum covers them)
        return f.read()


def _write_text_fuse(path: str, text: str, root: str = "/Workspace"):
    p = _fuse(path, root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", newline="") as f:     # no newline translation: the bytes written are the bytes hashed
        f.write(text)
