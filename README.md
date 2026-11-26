# GenAI governance lab

A hands-on lab that tests GenAI and Genie governance controls in your own Databricks workspace and records the
evidence. It ships as a [Declarative Automation Bundle](https://docs.databricks.com/aws/en/dev-tools/bundles/) and
accompanies the companion deck on governing GenAI and Genie. Controls it can't test automatically are covered by
[manual steps](docs/manual-steps.md) or listed as [gaps](docs/gaps.md).

## Modes
* **Audit** (`customer_audit` target): read-only checks of workspace settings, such as residency, partner-powered AI,
  IP access lists, token lifetime, CSP, warehouse timeout, who may build AI services, *Enforce Unity Gateway*,
  the legacy AI endpoint inventory and Singapore identifier tags vs ABAC masks. Deploys only the audit job, a results
  schema and the dashboard.
* **Demo**: synthetic patient data with Unity Catalog masks, a row filter and ABAC policies. Three persona service
  principals then exercise each GenAI surface:

| Surface | What the lab tests |
|---|---|
| Genie Agents | masks and row filter apply per user; no CAN RUN, no answer |
| Unity Gateway model service | EXECUTE grant, per-group rate limit, guardrail blocks an NRIC prompt, inference table |
| AI Functions | `ai_query` only sees what Unity Catalog lets the caller read; `ai_mask` on free text |
| Vector / AI Search | SELECT on the index; retrieval returns only masked text |
| MCP and agent tools | only granted groups can call a UC-function tool; the DBSQL MCP applies masks |
| Model serving | CAN QUERY; who can create services, connections and external-model endpoints (B-01 … B-09) |
| MLflow traces | experiment permission |
| Platform | region, in-Geo enforcement, partner-powered AI, CSP |
| Singapore identifiers | an ABAC mask covers every column tagged `class.sg_nric`, `class.sg_fin` or `class.sg_work_permit_number` |

A test passes only on positive evidence: exact seeded values, an error that names the object under test, or a
non-empty answer. Anything else is SKIP (with the reason), MANUAL, INFO or FAIL. Results land in
`<catalog>.<schema>.test_results` and on the dashboard **GenAI governance lab – results**, which shows the latest run
and marks missing tests.

## Quickstart
### Workspace (no CLI)
1. Clone this repository as a **Git folder** in your workspace.
2. Set `catalog`, `schema`, `resource_prefix`, `persona_prefix` and `warehouse_id` for target `workspace`, then click
   **Deploy** in the Git folder's bundle panel.
3. Open `src/notebooks/00_run_lab` on serverless and run the actions in order: `preflight`, `personas`, `run`,
   `results`; the next day `evidence`; at the end `teardown-plan`, `teardown-apply`.

See [docs/workspace-setup.md](docs/workspace-setup.md) for prerequisites, the identity checklist and teardown.

### CLI
```bash
# Audit only (read-only; no personas)
scripts/run_audit.sh customer_audit <warehouse-id> <catalog>

# Demo
WORKSPACE_PROFILE=<workspace-profile> \
  scripts/bootstrap_personas.sh --target customer <account-profile> <account-workspace-id> <prefix>
scripts/run_demo.sh customer <warehouse-id> <catalog>
databricks bundle run -t customer lab_evidence          # next day: audit-log evidence

# Export, then remove only what the lab created
scripts/export_results.sh customer <warehouse-id> <catalog>
scripts/teardown.sh --dry-run customer <warehouse-id> <catalog>
scripts/teardown.sh customer <warehouse-id> <catalog>
scripts/teardown_identities.sh customer <account-workspace-id>
```

## Prerequisites
* Databricks CLI v1.20+ (CLI path) and a serverless SQL warehouse.
* A **workspace admin** with `CREATE SCHEMA` and `MANAGE` on the target catalog. Run the audit as a **metastore admin**
  for a conclusive B-06.
* For the evidence job: `SELECT` on `system.access.audit` and `system.ai_gateway.usage`.
* With *Enforce Unity Gateway* on, `databricks-*` pay-per-token endpoints are disabled, so tests that rely on them
  (V-01, V-02, S-01, F-02) report SKIP. See the [migration guide](https://docs.databricks.com/aws/en/ai-gateway/migrate-to-unity-gateway).

## Targets
| Target | Mode | Notes |
|---|---|---|
| `demo` | demo + audit | workspace set in the untracked `local/targets.yml` |
| `customer` | demo + audit | needs the persona IDs from `bootstrap_personas.sh` |
| `customer_audit` | audit only | deploys nothing else |
| `workspace` | demo + audit | for the Git-folder path; `00_run_lab` creates the personas at run time |

## Names
The visible name is **GenAI governance lab**. `resource_prefix` names jobs, the model service and other lab objects,
and `persona_prefix` names the service principals and groups; both default to `lab`. The bundle name
(`genie-governance-lab`) and default schema (`genie_governance_lab`) keep their original names so existing
deployments keep working; a fresh deployment can change them before its first deploy.

## Safety and data
* **Synthetic data only.** Patients are labels such as `Test Patient 017`. NRIC-like values use invalid check letters,
  and phone numbers start with `5`, a level reserved in Singapore's numbering plan, so none is real or dialable.
* **Teardown removes only what the lab can prove it created**, from its manifest and creation records. Anything else
  is listed for manual removal, and a failed teardown can be resumed.
* **Before sharing or publishing**, run `scripts/check_shareable.sh`, `python3 scripts/check_no_literal_pii.py` and
  `scripts/check_release.sh --range HEAD` (see `scripts/check_release.py` for what it checks).

## Docs
| Doc | Use it for |
|---|---|
| [workspace-setup.md](docs/workspace-setup.md) | Running the lab from a Git folder with one notebook |
| [runbook.md](docs/runbook.md) | Facilitator guide for a ~2-hour session |
| [controls-matrix.md](docs/controls-matrix.md) | Every control in the deck and how the lab covers it |
| [manual-steps.md](docs/manual-steps.md) | UI and account-console steps (M-01 … M-14) |
| [gaps.md](docs/gaps.md) | What the lab can't prove, and how each gap is covered |
| [evidence-template.md](docs/evidence-template.md) | Sign-off sheet (generated by `scripts/gen_evidence_template.py`) |
| [controls.yml](expectations/controls.yml) | Test catalogue with the expected result per persona |

## Cost
A few minutes of serverless job and SQL compute per run, AI Functions on ~200 short notes, a few dozen Genie questions
and model calls, and a 200-row vector index on an existing endpoint. Genie usage by service principals is billed.
