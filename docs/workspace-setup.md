# Workspace setup: run the lab from a Git folder (no CLI)

This is the instruction manual for running the GenAI governance lab **entirely inside the Databricks workspace**:
you clone the repository as a Git folder, deploy the bundle once from the Git folder's bundle UI, and then drive
everything from **one notebook**, `src/notebooks/00_run_lab`. The CLI path (`scripts/*.sh`, [runbook](runbook.md))
keeps working; both paths use the same notebooks, the same validation and the same naming.

```
Git folder ──Deploy (target: workspace)──> jobs, schemas, model service, dashboard   (no identities yet)
00_run_lab: preflight → personas → run → results → (next day) evidence → teardown-plan → teardown-apply
```

## Before you start: confirm with your identity and platform teams
The lab creates three service principals and four account groups, gives them access, and deletes them at the end.
Agree on the following first. `action=preflight` verifies items 4 and 5 and prints the others as *confirm manually*.

1. **Who provisions identities into the Databricks account?** Account-level SCIM from your identity provider
   (for example Entra ID or Okta), automatic identity management, or workspace-level SCIM.
2. **Will the IdP connector, Terraform or any clean-up job leave alone service principals and groups it didn't
   create?** Settings such as "remove unmanaged identities" or drift deletion would delete the lab's personas in
   the middle of a run, or recreate what teardown removed.
3. **May workspace admins create account service principals and groups in this workspace?** If policy says no,
   use the **pre-provisioned path**: an account admin (or your IdP) creates the personas, and the notebook only
   assigns and grants (`manual_app_ids` / `manual_groups`, below).
4. **Will the person running the lab be a workspace admin?** Without it every identity step returns 403.
   *(preflight checks the `admins` group)*
5. **What is the workspace's `restrict_workspace_admins` setting, and is there a naming convention or a reserved
   prefix for groups?** The lab works under `RESTRICT_TOKENS_AND_JOB_RUN_AS`, because it grants you the Service
   Principal User role on each persona before it sets `run_as`. Pick a **distinctive `persona_prefix`** so the lab's
   groups can't clash with groups your IdP pushes. *(preflight reads the setting when it can)*
6. **Are lab identities allowed in this account at all, or only in a sandbox workspace, and what should teardown
   remove?** Teardown removes only identities the lab created and recorded (see [Teardown](#7-teardown)).

## Prerequisites
* A **workspace admin** runs the notebook (checklist item 4). Admin rights cover creating the personas, setting
  `run_as` and the notebook permissions.
* The **account ID** (a UUID: Account console > your profile menu). The notebook needs it for the Service Principal
  User role; notebooks don't know it otherwise.
* `CREATE SCHEMA` and `MANAGE` on the catalog you pick (for grants and the ABAC policies).
* A **serverless SQL warehouse** (its ID: SQL Warehouses > the warehouse > Overview).
* Serverless notebooks and jobs, and Git folders, enabled in the workspace.
* Optional: existing vector search / agent / Knowledge Assistant endpoints (`vs_endpoint`, `agent_endpoint`,
  `ka_endpoint`); empty means the dependent tests SKIP.
* For the next-day evidence: `SELECT` on `system.access.audit` and `system.ai_gateway.usage`.

**Effects of Enforce Unity Gateway** (Settings > Advanced). When it is on, `databricks-*` pay-per-token endpoints
are disabled, so a vector index with managed embeddings, an agent endpoint that calls one, and `ai_query` on a
`databricks-*` endpoint name stop working: V-01, V-02, S-01 and F-02 then **SKIP** with that reason (gap 30), and
new classic external-model endpoints can't be created (B-05). preflight prints the setting.

**Guardrail (M-04).** G-02 passes only once a service policy that blocks the NRIC prompt is attached to the lab's
model service `<resource_prefix>_chat` and its name is in `expected_policy`
([manual-steps.md M-04](manual-steps.md)). Until then G-02 is MANUAL.

**Customer-managed groups (IdP caveat).** If your IdP owns group membership (checklist items 1 and 2), the groups the
lab creates may be changed or removed by the next sync. Use the pre-provisioned path with groups your IdP manages,
or exclude the lab's `persona_prefix` from the sync.

## 1. Clone the repository as a Git folder
Workspace > your user folder > **Create > Git folder**, paste the repository URL, and check out the branch you want.
Your Git credentials need read access (Settings > Linked accounts).

## 2. Configure and deploy (target `workspace`)
The `workspace` target references **no identity at deploy time**: the personas don't exist yet. It deploys the
lab schema and `<schema>_tools` (no grants), the jobs (no `run_as`), the model service `<resource_prefix>_chat`
(no grants or rate limit) and the dashboard.

1. Set the deployment's values. Either edit `targets.workspace.variables` in `databricks.yml` in your Git folder,
   or create an untracked `local/targets.yml` there (gitignored, so it is never committed):
   ```yaml
   targets:
     workspace:
       variables:
         catalog: <your catalog>
         schema: <a new schema, e.g. genai_governance_lab>
         resource_prefix: <2-16 lowercase letters/digits, e.g. glab1>
         persona_prefix: <2-16 lowercase letters/digits, distinctive>
         warehouse_id: <serverless SQL warehouse ID>
   ```
   Use your own `schema` and `resource_prefix` per deployment so two deployments in one workspace never share
   names (job names are `<resource_prefix>_setup`, `<resource_prefix>_audit`, ...).
2. Open the bundle panel of the Git folder (the **Deployments** / bundle icon), pick target **workspace** and click
   **Deploy**. Wait for it to finish.

## 3. Run `00_run_lab`
Open `src/notebooks/00_run_lab`, attach **serverless**, fill the widgets, choose an **action**, and **Run all**.
Use the same `catalog`, `schema`, `resource_prefix` and `persona_prefix` you deployed with: the notebook finds
the deployment from its bundle state (the job `<resource_prefix>_audit` whose state records the lab schema
`catalog.schema`) and stops if it doesn't find exactly one.

| Widget | Meaning |
|---|---|
| `action` | `preflight`, `personas`, `run`, `resume`, `results`, `evidence`, `teardown-plan`, `teardown-apply` |
| `mode` | `demo` (personas and probes) or `audit` (read-only audit only); must match the deployment. `teardown-plan` / `teardown-apply` ignore it and follow the deployed mode |
| `catalog`, `schema` | the deployment's lab schema |
| `resource_prefix`, `persona_prefix` | as deployed |
| `account_id` | account UUID; demo mode only (identity records are bound to it). Audit-only deployments have no identities and don't need it, teardown included |
| `warehouse_id` | serverless SQL warehouse (for `run`, `resume`, `evidence`) |
| `vs_endpoint`, `agent_endpoint`, `ka_endpoint` | optional existing endpoints |
| `expected_policy` | the M-04 guardrail policy name (G-02) |
| `approved_builders`, `cleartext_groups` | B-06 / D-08 (empty = the clinicians / PII readers group) |
| `usage_lookback_days` | B-09 usage history window (1-365) |
| `run_id` | the suite run ID for `resume` / `evidence` |
| `manual_app_ids`, `manual_groups` | pre-provisioned personas (see 4b) |

Every value is validated with the lab's strict patterns before it is used. Empty optional values keep the job's
deployed default.

### 3a. `preflight`
Checks that you are a workspace admin, the catalog, the warehouse and the deployment, reads Enforce Unity
Gateway and `restrict_workspace_admins`, prints the checklist, and shows what `personas` would do for each name
(`create`, `reuse`, or `REFUSE` when a same-name identity exists that the lab has no record of creating).

## 4. Personas
### 4a. Automated (`action=personas`)
For each persona name (`<persona_prefix>-clinician`, `-pii-reader`, `-outsider`; groups `<persona_prefix>_clinicians`,
`_pii_readers`, `_all_facilities`, `_outsiders`) the notebook decides first, then creates:

1. **Service principals** through workspace SCIM, with the marker externalId `genie-governance-lab:<name>` and the
   entitlements *Workspace access* and *Databricks SQL access*. This creates an **account** service principal and
   assigns it to the workspace.
2. **Account groups** through the account SCIM proxy of the workspace, with the same marker.
3. **Membership** (clinician -> clinicians; PII reader -> PII readers and all facilities; outsider -> outsiders) and
   **workspace assignment** of the groups (User).
4. **Service Principal User** on each persona for you, so you can set them as job `run_as`.

**Identity store.** Each identity gets a **receipt** right after it is created (creation proof: no identity of that
name existed before; the marker was sent) in `<catalog>.<schema>.lab_identity_receipts`, and the personas' current
identities are **bindings** in `lab_persona_bindings`. Receipts are never overwritten; bindings can change (manual
fallback). Both are bound to the account ID, workspace ID, bundle target, deployment folder and lab names. Both tables
must have been created by this runner: a sealed creation record with their table IDs is kept in the deployment
folder (`runner/identity_store.json`), and a table that exists without it is never trusted. Before every use the
runner checks the tables' **effective** (including inherited) privileges: if anyone but you can write them, or you
don't own them, it stops. Reads are retried for eventual consistency. A same-name identity the lab has no receipt
for is **never adopted**: the action stops before creating anything.

### 4b. Manual fallback (pre-provisioned path)
If any step is refused (403, policy), the notebook prints the exact console steps and stops. After an account admin
or your IdP created the identities, set
`manual_app_ids = <clinician>,<pii_reader>,<outsider>` (application IDs) and
`manual_groups = <clinicians>,<pii_readers>,<all_facilities>,<outsiders>` and run `action=personas` again. The three
application IDs and the four groups must all be different. The fallback needs no account-level SCIM read: it checks
the identities through the workspace (each group must be an account group assigned to the workspace). Provided
identities are bound as `provided` and **never deleted** by teardown; receipts of identities the runner created
earlier are kept, so teardown still removes those. You still need the Service Principal User role on each.

## 5. `run` and `resume`
`run` creates a suite run ID and runs, in order: `<prefix>_setup` (synthetic data, UC policies, masked notes, Genie
Agent, GenAI objects; it also grants `USE SCHEMA` and binds the model service's EXECUTE grants and the clinicians'
rate limit), the three persona jobs, `<prefix>_collect` and `<prefix>_audit`. Before the persona jobs it

* gives each persona `CAN_READ` on the folder that holds the deployed notebooks, with a before/after receipt;
* sets **`run_as`** on each deployed persona job to its persona and verifies it. This is re-asserted before every
  run, because a redeploy resets it. The probe also checks it runs as exactly that persona (`expected_principal`):
  if not, the persona job **fails** without writing results, and the runner records the SKIPs itself.

`mode` must match the deployment (audit only on an audit-only deployment, demo only on a demo deployment; teardown
ignores the widget and follows the deployed mode). Each step
is logged in `lab_runner_steps`. If any step fails, the run **stops** (collect and audit never run on partial persona
results). Fix the cause and run `action=resume` with `run_id`: it reruns the first step that didn't succeed and
every step after it. Waits are bounded (per job) and the persona probe keeps its 65-second pauses for the
rate-limit test. `approved_builders` / `cleartext_groups`: an empty widget keeps the allowlist the deployment
already has; only when none is configured is the clinicians / PII readers group used.

## 6. `results` and `evidence`
`results` shows the latest suite exactly as the dashboard does, including **MISSING** (no result recorded) and
**PENDING** (evidence not collected yet), and prints the dashboard link. The next day, `evidence` runs
`<prefix>_evidence` with the personas' application IDs and shows the results again.

## 7. Teardown
* `teardown-plan` changes nothing: it runs the teardown job as a dry run and lists the identities and the bundle
  resources that would be removed.
* `teardown-apply` (resumable: rerun it after fixing whatever stopped it):
  1. exports the latest suite to your home folder (`genai_governance_lab_exports/`, CSV + checksum metadata). If no
     results table exists, it skips the export only with positive evidence that no suite ran in this generation: the
     generation record shows no `run` and the runner's step log records no step. Otherwise it stops with *results
     missing for a generation that ran* and removes nothing (restore the tables, or check by hand). It then
     writes a **sealed checkpoint** (sha256: the deployment generation, the export, the identity receipts, the phases
     done) to the deployment folder and your home folder (`teardown-checkpoint-<binding>-<generation>.json`). A retry
     reuses the verified export, so it still works after the results tables are gone, and resumes from the checkpoint
     even after the deployment's jobs are gone;
  2. runs `<prefix>_teardown` (demo; deletes only what `lab_manifest` records) and stops if anything fails.
     **Audit-only deployments** have no teardown job. There, the runner removes the results tables the audit job
     created (`suite_runs`, `test_catalogue`, `test_results`, `lab_setup_status`), but only with proof. Before the
     generation's first `run`, it records in the sealed generation record that each table didn't exist (a structured
     not-found). After the run, it records each table's Unity Catalog table ID. Teardown first checks every table:
     a table may be dropped only if its live table ID equals the recorded one, and a table that is already gone is
     fine. If any table existed before the first run, has no recorded ID, or has a different ID (it was replaced),
     nothing is dropped: the tables are listed for manual review and the teardown stops. Otherwise each table is
     dropped (`DROP TABLE`, never `CASCADE`) and verified gone. This phase is resumable: a rerun after a partial drop
     treats the dropped tables as gone and drops the rest;
  3. removes the `CAN_READ` entries the runner added, each only if it is still exactly that entry, then deletes only
     identities with a **receipt**, only if the live ID, displayName and (service principals) applicationId all
     match it, and verifies each is gone. Only a structured not-found counts as gone, never an error message.
     Provided identities are released, not deleted. Anything else is **unresolved**: it stays recorded and the
     teardown stops before touching the schemas or jobs;
  4. removes this deployment's own bundle resources, as recorded in its bundle state: model service, dashboard, the
     runner's own tables (each verified as the runner's), then each lab schema **only if it is empty** (tables, views,
     functions, volumes, models, AI services and connections are checked; anything left is listed for manual review
     and nothing is dropped; never `CASCADE`), then the jobs and the bundle's deployment folder. The Git folder is not
     touched.

**Deployment generations (deploy again after a teardown).** A deployment's first `personas`, `run` / `resume` (in
**both** modes: an audit-mode run creates a generation too) or `teardown-apply` records a **generation ID** in its
folder (`runner/generation.json`, sealed and bound to the workspace, target, folder, lab names and the deployed audit
job). `preflight`, `results`, `evidence` and `teardown-plan` only read it. `personas` and `run` also note in the
record that they started, before they change anything. The identity store, every receipt, every step-log row and
every checkpoint carry the generation. Teardown deletes the folder, so deploying again starts a **new generation**, and nothing recorded for an earlier
generation is reused: a checkpoint of another generation stops the teardown instead of being resumed, and an earlier
generation's *complete* checkpoint is never looked at for the new one. Without a live deployment, teardown resumes
only the single unfinished checkpoint of this lab binding, and only if neither the folder nor the identity store shows
a newer generation.

*Deploying again after a teardown:* teardown removes the deployment folder directly (not through `bundle destroy`),
so a deploy that reuses an old local bundle cache can skip uploading the notebooks it believes are already there
(seen with the CLI: `Files: 0 uploaded`). The notebook checks that every notebook the action's jobs use exists
(`preflight`: all jobs; `run` / `resume`: setup, the persona jobs, collect and audit, or audit only in audit mode;
`evidence`: the evidence job; teardown: the teardown job) and stops with this hint before any job starts. With the CLI, remove the local
`.databricks/bundle/<target>` folder before deploying again.

**"Gone" means a structured not-found** in both the runner and the `<prefix>_teardown` job (SDK `NotFound` /
`ResourceDoesNotExist`, a not-found error code, an HTTP 404, a not-found Spark error class). A permission error or a
503 never counts as gone, even if its message says "not found"; the step stays unresolved and the manifest and results
tables are kept for a rerun.

Nothing else is removed. Account admins should still check their IdP (checklist item 2).

## What stays manual
M-01 … M-14 in [manual-steps.md](manual-steps.md) (the guardrail policy M-04 in particular), the account-level
checks (`scripts/account_checks.py`), deploying the bundle (one click), and the checklist above.

Also manual: anything teardown lists for **manual review** instead of deleting, because it can't prove the object is
the lab's. Check each item, remove it by hand if it is, then rerun `teardown-apply` (which treats what is gone as
done). The cases are:
- audit results tables that existed before the deployment's first run, have no recorded table ID, or were replaced;
- identities or ACL entries whose live state no longer matches their receipt;
- Genie spaces without creation proof;
- objects left in a schema after the scoped cleanup.
