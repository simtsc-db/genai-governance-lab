# GenAI governance lab

A hands-on lab that accompanies the companion deck on governing GenAI and Genie on Databricks. It deploys, as a
[Declarative Automation Bundle](https://docs.databricks.com/aws/en/dev-tools/bundles/), a small sandbox that
**tests a selected set of governance controls in your own workspace** and records the evidence. Controls
it can't test automatically are covered by guided manual steps or documented as walk-throughs and known
gaps (see the [controls matrix](docs/controls-matrix.md) and [gaps](docs/gaps.md)).

* **Audit mode** (`customer_audit` target): read-only checks of workspace settings (residency, partner-powered AI,
  IP access lists, token lifetime, CSP, warehouse timeout, who may build AI services, *Enforce Unity Gateway*, the
  legacy AI endpoint inventory, Singapore identifier tags vs ABAC masks). It deploys only the audit job, a results schema with no grants and the dashboard.
  Account-level settings are an optional read-only inventory (`scripts/account_checks.py`).
* **Demo mode**: synthetic patient data and Unity Catalog masks, a row filter and an ABAC tag policy,
  then the same three **persona service principals** exercise the main GenAI surfaces:

  | Surface | What the lab tests |
  |---|---|
  | Genie Agents | per-user masks and row filter in answers; no CAN RUN, no answer |
  | Unity Gateway model service (`<prefix>_chat`, default `lab_chat`) | EXECUTE grant, per-group rate limit, the intended guardrail blocks an NRIC prompt (custom judge attached by hand), inference table |
  | AI Functions | `ai_query` only receives what UC lets the caller read; `ai_mask` on free text |
  | Vector / AI Search | SELECT on the index; retrieval returns only masked text |
  | MCP and agent tools (T-01, T-02) | only groups with EXECUTE can call a UC-function tool; DBSQL MCP applies masks |
  | Model serving / agents | CAN QUERY on the endpoint; no **new** classic external-model endpoint outside Unity Gateway (B-05; blocked by *Enforce Unity Gateway*, B-08). Existing ones stay queryable: B-09 (audit) inventories them |
  | MLflow traces | experiment permission |
  | Platform and residency | region, in-Geo enforcement (cross-Geo-only features refused), partner-powered AI, CSP |
  | Singapore identifiers | ABAC mask for every column tagged `class.sg_nric` / `class.sg_fin` / `class.sg_work_permit_number` (D-08) |

  A test passes only on positive evidence: exact seeded values, an error that names the object under test,
  a non-empty answer. Anything else is SKIP (with the reason), MANUAL, INFO, or FAIL.

Every result lands in `<catalog>.<schema>.test_results` and on the dashboard **GenAI governance lab – results**
(the default name; the `workspace` target names it `GenAI governance lab – results (<resource_prefix>)`, so several
deployments in one workspace stay apart), which shows the **latest suite run** only (one run ID shared by setup, personas and audit) and marks missing
tests as MISSING / PENDING. Each persona writes only its own staging table; a trusted collector merges them.

**All data is synthetic.** Patients are obviously fictional labels (`Test Patient 017`), generated at runtime.
NRIC-like values match the pattern `[STFGM]` + 7 digits + letter but use check letters that are never valid for the
`S` prefix, so none is a real NRIC; phone numbers start with `5`, a level that IMDA's National Numbering Plan
reserves, so none can be dialled in Singapore (the lab's phone pattern covers `5` as well as the allocated levels
`3`, `6`, `8` and `9`). Both still match the patterns the probes and guardrails look for. The repository
holds no literal NRIC, phone number or realistic name; `scripts/check_no_literal_pii.py` checks this with
pattern and denylist coverage (NRIC/FIN shapes in any letter case, SG phone numbers with common punctuation and
`+65`, and a configurable list of name tokens, also when wrapped across lines). It can't recognise every personal
value, so review what you add. `--staged` checks the staged content before a commit.

Everything the lab creates is recorded in a manifest (`lab_manifest`), and `scripts/teardown.sh` removes only
those items (`--dry-run` lists them first).

| Doc | Use it for |
|---|---|
| [docs/workspace-setup.md](docs/workspace-setup.md) | Run the lab from a Git folder with one notebook (no CLI) |
| [docs/runbook.md](docs/runbook.md) | The facilitator guide for a ~2-hour session, layer by layer |
| [docs/controls-matrix.md](docs/controls-matrix.md) | Every control in the deck, its status and how the lab covers it |
| [docs/manual-steps.md](docs/manual-steps.md) | UI and account-console steps (M-01 … M-14), each with a check |
| [docs/gaps.md](docs/gaps.md) | What this approach can't prove, and how each gap is covered |
| [docs/evidence-template.md](docs/evidence-template.md) | Sign-off sheet for your security review (generated: `python3 scripts/gen_evidence_template.py`) |
| [expectations/controls.yml](expectations/controls.yml) | Test catalogue: expected result per persona |

## Names
The human-visible name is **GenAI governance lab** (dashboard, Genie Agent, object comments). Technical identifiers
keep their original names so existing deployments keep working: bundle name `genie-governance-lab`, default schema
`genie_governance_lab`, job resource keys, and the identity marker `genie-governance-lab:<name>`.
Two variables name everything else: **`resource_prefix`** (job names `<prefix>_setup` ..., the model service
`<prefix>_chat` and its inference table, connections, ABAC policies, functions, the vector index, the experiment and
probe endpoints) and **`persona_prefix`** (service principals `<prefix>-clinician` ..., groups `<prefix>_clinicians`
...). Both default to `lab`, which reproduces the historical names (`lab_setup`, `lab_chat`, `facility_capacity`,
`glab-probe-...`, `lab_clinicians`), so existing deployments are unchanged. Tables inside the lab schema keep fixed
names (the schema scopes them). A fresh deployment may use `genai_governance_lab` as the schema (`--var schema=...`)
and `genai-governance-lab` as the bundle name (edit `bundle.name` in `databricks.yml` before the first deploy;
renaming it later orphans the existing deployment). `scripts/teardown_identities.sh` must keep accepting the
`genie-governance-lab:` marker, because identities created by earlier bootstraps carry it.

## Prerequisites
* Databricks CLI **v1.20+** (`databricks --version`) and Python 3.10+ with `databricks-sdk` (for `scripts/account_checks.py`).
* A workspace CLI profile (`databricks auth login --host <workspace-url> --profile <name>`), and an
  **account-console** profile if you want the persona bootstrap and account checks automated.
* The deployer is a workspace admin with `CREATE SCHEMA` on the target catalog and `MANAGE` on it
  (for grants and the ABAC policy), and can use a **serverless SQL warehouse**.
* For the evidence job: `SELECT` on `system.access.audit` and `system.ai_gateway.usage`.
* For a conclusive B-06 (who may build AI services): run the audit as a **metastore admin**; otherwise it reports MANUAL.
* Network: the machine running the CLI must reach the workspace (Private Link / IP access lists).
* *Enforce Unity Gateway* (Settings > Advanced) blocks **new** B-05 routes (existing external-model endpoints stay
  queryable: B-09 inventories them; PASS needs a workspace-admin caller and every endpoint classified, and B-08 PASS +
  B-09 PASS means no existing external-model / PT route right now; usage history is attached but reviewed separately), and it disables `databricks-*` pay-per-token endpoints: a vector index with managed embeddings on one, an agent endpoint that calls one, and `ai_query` on a
  `databricks-*` endpoint name stop working (V-01, V-02, S-01, F-02 then SKIP; gap 30). See the [migration guide](https://docs.databricks.com/aws/en/ai-gateway/migrate-to-unity-gateway).

## Targets
| Target | Mode | Workspace | Deploys |
|---|---|---|---|
| `demo` (default) | demo + audit | set in `local/targets.yml`, or `-p <profile>` | everything below |
| `customer` | demo + audit | your workspace (profile `customer`, or `-p <profile>`) | results schema, dashboard, `lab_audit`, plus model service `lab_chat`, tools schema, setup / persona / collect / evidence / teardown jobs, persona grants |
| `customer_audit` | audit only | your workspace | results schema (no grants), dashboard, `lab_audit`. Nothing else. |
| `workspace` | demo + audit | the workspace of the Git folder (bundle UI **Deploy**) | same as `customer`, but nothing that references an identity: `00_run_lab` creates the personas and binds `run_as`, grants and the rate limit at run time ([workspace-setup](docs/workspace-setup.md)) |

**Workspace-specific settings stay out of git.** Copy `local.example/targets.yml` to `local/targets.yml`
(gitignored) to set the CLI profile and existing endpoints for your workspace; every target still validates
without it. Run `scripts/check_shareable.sh` and `scripts/check_no_literal_pii.py` before sharing the folder: it fails on workspace hosts or IDs, ticket keys,
go/ or Slack links and e-mail addresses, plus the literal internal names (profiles, endpoints, prefixes) you list in
the gitignored `local/shareable_patterns.txt` (template in `local.example/`), so those names never sit in git.

**Before publishing** (a new or public repository), also run the release check on exactly what you will push:

```bash
scripts/check_shareable.sh --require-local && python3 scripts/check_no_literal_pii.py
scripts/check_release.sh --range HEAD      # every commit you will push: its full tree and its metadata
scripts/check_release.sh                   # HEAD's committed tree (warns if the working tree differs)
scripts/check_release.sh --worktree        # the working copy, before you commit
```

`scripts/check_release.sh` reads committed blobs with `git cat-file` (not the working copy) and fails on UUIDs, runs
of 16+ hex digits and 15+ decimal digits (also in file names), on any e-mail address in the `databricks.com` domain,
and on binary files, symlinks and submodules. The only exceptions are exact placeholder values, binaries or symlinks
listed per path in `scripts/release_allowlist.txt`, read from each scanned commit (so an uncommitted or later
exception never clears a finding; `--allowlist <file>` overrides it explicitly). With `--range` it also fails on
commits whose author or committer isn't an allowed name (`--allow-name`, default `simtsc-db`) with a GitHub noreply
e-mail (`--allow-email <regex>`), or whose message has a `Co-authored-by` / `Signed-off-by` line, any trailer
(unless its key is given with `--allow-trailer`), or any e-mail address.

Persona jobs need the three persona application IDs (`.lab.env`); a deploy without them fails by design.
The deployer needs the **Service Principal User** role on each persona (the bootstrap grants it), and
the personas need **Workspace access** and **Databricks SQL access** entitlements plus CAN_VIEW on the
bundle folder (granted by the bundle's top-level `permissions`).

**Catalog on Default Storage accounts:** `CREATE CATALOG` needs a managed location there, for example
`CREATE CATALOG lab MANAGED LOCATION 's3://<bucket>/<path>'` on an existing external location.

## Quickstart
### Workspace (no CLI)
1. Clone this repository as a **Git folder** in your workspace.
2. Set `catalog`, `schema`, `resource_prefix`, `persona_prefix` and `warehouse_id` for target `workspace`
   (`databricks.yml` or an untracked `local/targets.yml` in the Git folder) and click **Deploy** (target `workspace`)
   in the Git folder's bundle panel.
3. Open `src/notebooks/00_run_lab` on serverless and run the actions in order: `preflight`, `personas`, `run`,
   `results`; the next day `evidence`; at the end `teardown-plan`, `teardown-apply`.

Prerequisites, the identity checklist to agree with your identity and platform teams, the manual fallback for
personas and what teardown removes: [docs/workspace-setup.md](docs/workspace-setup.md).

### CLI
```bash
# AUDIT mode only (read-only checks; no personas needed)
scripts/run_audit.sh customer_audit <warehouse-id> <catalog> [<account-profile> <workspace-id>]
#   only runs on a target it positively identifies as audit-only; --no-deploy skips the deploy

# DEMO mode
WORKSPACE_PROFILE=<workspace-profile> \
  scripts/bootstrap_personas.sh --target customer <account-profile> <account-workspace-id> [prefix] [deployer-email]
#   3 SPs + 4 account groups (marked as lab-created), workspace assignment, entitlements, and the
#   Service Principal User role for the deployer -> .lab.env, bound to --target (same target as below) and
#   the workspace. Refuses to reuse existing identities with the same names unless you pass --adopt (adopted
#   ones are never deleted). Use a unique prefix.
scripts/run_demo.sh customer <warehouse-id> <catalog>   # deploy, setup, 3 personas, collect, audit (one suite run ID)
#   refuses audit-only targets
#   optional gateway tests: GATEWAY_ENDPOINT=<catalog>.<schema>.<service> scripts/run_demo.sh ...
#   (three-part Unity Gateway model service name; anything else is rejected by the persona probe)
databricks bundle run -t customer lab_evidence                      # next day: system-table evidence

# Export results, then remove what the lab created
scripts/export_results.sh customer <warehouse-id> <catalog>
scripts/teardown.sh --dry-run customer <warehouse-id> <catalog>    # lists what would be deleted
scripts/teardown.sh customer <warehouse-id> <catalog>              # stops before bundle destroy if a deletion fails
#   needs a fresh export or a verified earlier one (evidence/*.meta.json checksum); things the lab can't prove
#   it owns (legacy probe objects, connections, ACL entries / grants without before-after proof) are listed for manual removal
scripts/teardown_identities.sh customer <workspace-id>             # identities only; separate and retryable
```

**Facilitator dry run.** To see what Genie returns for *you*, run `src/notebooks/30_persona_probe`
interactively with `allow_human=true`; results are recorded as INFO in `test_results_facilitator`.

## What gets created
| Object | Where | Removed by |
|---|---|---|
| Schema `genie_governance_lab`: data, results, staging, manifest tables; mask functions | your catalog | `lab_teardown` job + `bundle destroy` |
| Genie Agent "Clinic visits (GenAI governance lab)" (created by `lab_setup`, see `optional/` for bundle-managed) | workspace | `lab_teardown` job |
| ABAC policies `lab_mask_tagged_pii` (tag `class.phone_number`) and `lab_mask_sg_identifiers` (`class.sg_*`) | schema | `lab_teardown` job |
| Table `sg_identifiers_simulated` with a **manually set** `class.sg_nric` tag (labelled SIMULATED) | schema | `lab_teardown` job |
| `USE CATALOG` for the persona groups, only where they didn't already have it | your catalog | `lab_teardown` job (revokes only what it added) |
| Jobs `<prefix>_*`, dashboard | workspace | `bundle destroy` (workspace path: `00_run_lab` teardown-apply) |
| Unity Gateway model service `lab_chat` (+ its guardrails and inference table) | lab schema | `bundle destroy` |
| Schema `<schema>_tools` with the MCP tool function | your catalog | `lab_teardown` + `bundle destroy` |
| Dummy MCP connections `lab_mcp_conn` (no `USE CONNECTION`) and `lab_mcp_conn_builders` (`USE CONNECTION` for clinicians, only if the lab added it); placeholder host, never sends traffic | tools schema | `lab_teardown` job (revokes the grant it added, drops only connections it proved it created, verifies both; others listed for manual review) |
| Vector search index `clinical_notes_idx`, MLflow experiment | lab schema / your user folder | `lab_teardown` job |
| Lab-group ACL entries on the existing agent and vector search endpoints, only where the group had none | existing endpoints | `lab_teardown` job (removes only those entries) |
| Probe objects from B-01 … B-05 and B-04b (unique per-run names; deleted and verified by the probe itself) | tools schema / workspace | probe; leftovers by `lab_teardown` |
| Service principals `<persona_prefix>-*`, groups `<persona_prefix>_*` (marked as created by bootstrap / `00_run_lab`) | account | `scripts/teardown.sh` or `00_run_lab` teardown-apply (only recorded ones with creation proof) |
| `lab_identity_receipts`, `lab_persona_bindings`, `lab_runner_steps` (workspace path only: identity receipts, persona bindings and the step log) | lab schema | `00_run_lab` teardown-apply |

## Variables for the GenAI surfaces
| Variable | Default | Purpose |
|---|---|---|
| `chat_model` | `models/system.ai.databricks-claude-sonnet-4-6` | model behind `<prefix>_chat` and the B-01 probe (must work in your Geo) |
| `vs_endpoint` | empty (SKIP) | existing vector search endpoint for V-01/V-02 |
| `agent_endpoint` | empty (SKIP) | existing agent or serving endpoint for S-01 |
| `ka_endpoint` | empty (SKIP) | existing Knowledge Assistant endpoint for R-01 (configuration only) |
| `ka_invoke` | `false` | `true` also sends one synthetic question, only to an endpoint tagged `lab_synthetic=true`; stores HTTP status and correlation headers, never the answer |
| `expected_policy` | empty | name of the service policy that should block the NRIC prompt (G-02); empty = a block is INFO |
| `cleartext_groups` | empty (demo: PII readers group) | groups an ABAC mask may exempt for D-08 to count it as coverage |
| `approved_builders` | empty (demo: clinicians group) | principals allowed to create AI services / connections (B-06) |
| `usage_lookback_days` | `30` | days of `system.serving` usage B-09 attaches as supplementary evidence (never part of its verdict; 1–365) |
| `nric_tag_keys` | `class.sg_nric,class.sg_fin,class.sg_work_permit_number` | tags the SG-identifier ABAC policy and D-08 use |
| `embedding_model` | `databricks-qwen3-embedding-0-6b` | embeddings for the index |

## Cost
Serverless job and SQL compute for a few minutes per run, AI Functions for ~200 short notes,
a few dozen Genie questions and model calls, and a small vector index (200 rows) on an existing endpoint. **Genie usage by service principals is billed** (it is not covered by
the free user allowance).

## Layout
```
databricks.yml            bundle, variables, targets
local.example/            template for the untracked workspace overlay (local/targets.yml)
resources/                audit mode: results schema, lab_audit, dashboard
resources/demo/           demo mode only: model service, tools schema, setup, personas, collect, evidence, teardown
src/notebooks/            00_run_lab (workspace runner) · 00 data · 10 UC policies · 15 masked notes · 16 Genie Agent · 17 GenAI surfaces ·
                          20 audit settings · 25 platform probe · 30 persona probe · 40 collect · 50 evidence · 99 teardown
src/lib/checks.py         result recorder, assertions, names
src/lib/runner.py         workspace-native runner used by 00_run_lab
src/constraints.txt       the one place Python dependencies are pinned
expectations/controls.yml test catalogue
scripts/                  bootstrap, run, export, account checks, teardown, check_shareable, check_release, check_no_literal_pii,
                          validate_names (the notebooks' validators for the CLI), gen_evidence_template
optional/                 bundle-managed Genie Agent (phase 2)
docs/                     workspace setup, runbook, matrix, manual steps, gaps, evidence template
```
