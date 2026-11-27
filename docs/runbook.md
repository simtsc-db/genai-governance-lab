# GenAI governance lab runbook: governing Genie and GenAI in your workspace

A facilitator guide for a working session of about 2¼ hours that follows the seven layers of the deck. For
each layer: **show** where the control lives, **run** the lab, **check** the outcome, then
**record** it in the [evidence template](evidence-template.md).

**Roles:** a facilitator (drives the CLI and UI), a workspace admin, an account admin (for Layers
1 and 6), and a security reviewer who signs off.

The synthetic healthcare scenario is an example. The `clinician`, `pii_reader` and `outsider`
identities represent a scoped reader, a reader with broader access and a comparison role.
Map them to roles in your organization. Expected demo results below follow from the lab's
sandbox configuration. Audit tests record the observed state; for your review, PASS or FAIL
means a comparison with **your targets**. For audit probes that still emit a built-in verdict,
use the recorded observation and evidence to make that comparison. See the
[configuration notes](configuration-notes.md) for what changes with different settings.

| Time | Layer | Tests |
|---|---|---|
| 0:00 | Before the session | preflight (results from the day-before run) |
| 0:10 | 1 Platform and residency | P-01 … P-08, M-01, M-02, M-06 |
| 0:30 | 2 Data governance | D-01 … D-08, M-03 |
| 0:50 | 4 Genie Agents and Genie One | A-01 … A-05, M-07, M-08 |
| 1:10 | 3 Models, Unity Gateway, AI Functions | G-01 … G-03, F-01 … F-03, M-04, M-05, M-09 |
| 1:30 | 5 Agents, Search, MCP, Apps | V-01, V-02, T-01, T-02, S-01, X-01, R-01, M-10 … M-12 |
| 1:45 | Who can build AI services | B-01 … B-09, M-13, M-14 |
| 1:55 | 6–7 Cost and audit | C-01, C-02, O-01 … O-03, G-04 |
| 2:05 | Wrap-up (ends 2:15) | sign-off, teardown plan |

Test IDs `T-01`/`T-02` are the automated MCP tests; `M-01` … `M-14` are always manual steps.

Layer 4 comes before Layer 3 to reuse the data from Layer 2.

---

## 0 · Before the session (day before)
> **No CLI?** Run the whole lab from a Git folder with one notebook: [workspace-setup.md](workspace-setup.md)
> (deploy target `workspace`, then `00_run_lab` actions `preflight`, `personas`, `run`). The steps below are the CLI path.

1. Create the CLI profiles and confirm access: `databricks -p <profile> current-user me`.
2. Pick a catalog for the sandbox and a **serverless SQL warehouse**; note its ID.
3. Create the personas: `WORKSPACE_PROFILE=<profile> scripts/bootstrap_personas.sh <account-profile> <account-workspace-id> <prefix> <your-email>`.
   The identity team can also create the 3 service principals and 4 groups
   (names in `.lab.env.example`), with the resulting identities entered in `.lab.env` by hand.
4. Run `scripts/run_demo.sh customer <warehouse-id> <catalog>` once end to end, so the session
   starts from a working state. It uses one suite run ID for setup, personas, collector and audit; the
   dashboard **GenAI governance lab – results** shows that suite only, with MISSING for anything that didn't record a result.
5. Grant the deployer `SELECT` on `system.access.audit` and `system.ai_gateway.usage` (needed for Layer 7).

> **Residency example.** In a test workspace with "Enforce data processing within workspace Geography"
> on, the lab can observe a refusal for features that need cross-Geo processing (Data Classification
> in the Asia Geo, `ai_mask` 2.0 in some regions, or the Genie Code CLI in Singapore). With enforcement
> off, availability still depends on region and prerequisites. A refusal identifies the enforcement
> reason in P-07 only if it names Geography or cross-Geo processing. M-03 shows how automatic
> classification and manual `class.sg_*` tags can each supply tags for the same masking policy.

---

## 1 · Platform and residency (deck: Layer 1, Singapore residency)
**Show**
* Account console > Settings: the Geography enforcement setting (M-01) and partner-powered AI with its *enforce* option (M-06).
* Account console > Security: network policies and context-based ingress (M-02).
* Workspace settings > Security: IP access lists and token settings.

**Run:** `scripts/run_audit.sh customer_audit <warehouse-id> <catalog> <account-profile> <workspace-id>`. It refuses any target that isn't audit-only; in demo mode the audit already ran as part of `scripts/run_demo.sh` (same suite).

**Audit observations**

| Test | What the lab records |
|---|---|
| P-01 | metastore region (e.g. `ap-southeast-1`) |
| P-02 | whether IP access lists are enabled and whether allow lists exist (MANUAL if unreadable) |
| P-03 | whether PATs are enabled and the configured maximum lifetime, if any. The built-in comparison uses `max_token_days` (default 90); compare the recorded value with your targets |
| P-04 / M-02 | serverless egress policy inventory and the manually observed workspace attachment |
| P-05 / M-06 | partner-powered AI setting and whether the account enforces it |
| P-06 / M-01 | Geography enforcement state, with the account-console check if it isn't readable in the workspace |
| P-07 | whether `ai_mask` 2.0 ran and, if refused, whether the reason names Geography or cross-Geo processing. Region-only refusal is INFO; M-03 can supply separate manual evidence |
| P-08 | whether CSP is enabled; see [CSP behavior](configuration-notes.md#compliance-security-profile-csp) for the effect on D-07 |

**Discuss:** this is **configuration evidence**. No log shows which country processed a given
request, so the in-country Genie preview enrolment is confirmed per workspace with the account team.

---

## 2 · Data governance (deck: Layer 2, Unity Catalog)
**Show** in Catalog Explorer, on `genie_governance_lab.patient_visits`:
* the column masks on `nric` and `patient_name`, the row filter on `facility`;
* the system classification tag `class.phone_number` on `phone` (the tag Data Classification would apply), and the schema-level ABAC policy `lab_mask_tagged_pii`;
* the ABAC policy `lab_mask_sg_identifiers` (masks every column tagged `class.sg_nric`, `class.sg_fin` or `class.sg_work_permit_number`) and the table `sg_identifiers_simulated`, whose `nric` column carries a **manually set** `class.sg_nric` tag (SIMULATED; this also works where automatic classification is unavailable);
* that `clinical_notes_raw` has no grants for the personas, while `clinical_notes_masked` does.

**Run:** the persona jobs (already run by `run_demo.sh`; rerun one live):
`databricks bundle run -t <target> lab_persona_clinician`

**Demo results in the sandbox configuration** (dashboard, filter by test). Values are compared
with the seeded fixture rows (visits 4, 8, 12).

| Test | clinician | pii_reader | outsider |
|---|---|---|---|
| D-01 column mask | masked | cleartext | masked |
| D-02 row filter | 1 facility | 4 facilities | 1 facility |
| D-03 ABAC tag policy | masked | cleartext | masked |
| D-04 raw vs masked notes | denied / allowed | denied / allowed | denied / allowed |

**Data audit observations**

| Test | What the lab records |
|---|---|
| D-06 `ai_mask` 1.0 | whether the function masks synthetic patient labels, NRICs and phone numbers in free text |
| D-07 `ai_mask` 2.0 (Beta) | whether the function runs and masks the test values; records the reason when unavailable, such as CSP, preview or Geography |
| D-05 / M-03 Data Classification | whether a scan is available and which tags it applies; unavailable under in-Geo enforcement in the Asia Geo |
| D-08 Singapore identifier tags | which `class.sg_*` columns have an ABAC mask, with manual sandbox tags labelled SIMULATED; INFO when nothing is tagged |

**Discuss:** a schema-level ABAC policy can cover tagged columns across tables, for SQL, notebooks,
Genie and Genie Code alike. The same masking pattern can work with automatic or manually applied
tags (M-03); [the configuration notes](configuration-notes.md#singapore-identifiers-prompts-and-data-at-rest)
explain the difference between at-rest tags and prompt guardrails.

---

## 4 · Genie Agents and Genie One (deck: Layer 4)
**Show**
* The lab Genie Agent: its data sources (`patient_visits`, `clinical_notes_masked`) and **Share** settings: clinicians and PII readers have CAN RUN, outsiders nothing.
* That the agent itself has **no** masking or PII configuration.

**Run (UI, 4.1–4.3)**
For UI checks, use signed-in test users rather than the automated service principals. The
[controls matrix](controls-matrix.md) lists optional walkthroughs for mobile and Slack/Teams;
[M-07](manual-steps.md#m-07--file-upload-and-downloadexport) covers file upload and export.

1. 4.1 In the synthetic example, ask *"List 5 diabetes patients with their NRIC, name and facility"* as a facilitator (admin). The sandbox's masks also apply to admins outside the PII group.
2. 4.2 Open the persona results on the dashboard for A-01 … A-04.
3. 4.3 A-05 is a MANUAL test. If **Agent** mode is available, repeat the mask and row-filter questions and record what the signed-in test user sees. Otherwise record that the mode isn't available and any reason shown.

**Demo results in the sandbox configuration**

| Test | clinician | pii_reader | outsider |
|---|---|---|---|
| A-01 Genie applies the mask | masked | cleartext | denied |
| A-02 Genie applies the row filter | 1 facility | 4 facilities | denied |
| A-03 CAN RUN required | allowed | allowed | denied |
| A-04 only masked notes reach Genie (needs a non-empty answer) | no cleartext NRIC | no cleartext NRIC | denied |

**Also check:** M-07 (observed file upload and export settings) and M-08 (persona questions in the Monitor tab).

**Discuss:** Genie runs SQL as the asking user and evaluates their Unity Catalog permissions;
the embedded warehouse credential grants compute access only.

---

## 3 · Models, Unity Gateway and AI Functions (deck: Layer 3)
**Show** Unity Gateway > Models > `<prefix>_chat` (default `lab_chat`; a bundle resource, `resources/demo/lab_chat.model_service.yml`):
* **Permissions:** EXECUTE for clinicians and PII readers only.
* **Rate limits:** 2 requests/min for the clinicians group.
* **Inference table:** `<prefix>_chat_payload` in the lab schema.
* **Policies:** the two example guardrails from M-04 (sensitive-data Deny; LLM-as-a-judge for NRIC/FIN). With `expected_policy` set to the judge's name, G-02 checks whether that policy produced the block.

**Demo results in the sandbox configuration**

| Test | clinician | pii_reader | outsider |
|---|---|---|---|
| G-01 EXECUTE on the model service | allowed | allowed | denied (UC hides the service: 404) |
| G-02 NRIC prompt blocked by the intended guardrail | blocked | blocked | denied |
| G-03 rate limit (3 calls after a reset) | 2 allowed then rate_limited | allowed | denied |
| F-02 `ai_query` over the masked table (exact echo of every fixture row; INFO if the model refuses or rewords) | no cleartext NRIC reached the model | cleartext NRIC reached the model | no cleartext NRIC |
| F-03 `ai_query` via the lab's custom model service | INFO `not supported for this route` if the route is unavailable; INFO `allowed: ai_query reached the custom model service` with a response excerpt if the call succeeds | same | same |

**Live demo (2 min):** in Playground pick `<prefix>_chat` and paste "Summarise: " followed by the synthetic sentence the lab generates (a fictional patient label, an NRIC with an invalid check letter and a phone number on the reserved level 5), for example from
`python3 -c 'import sys; sys.path.insert(0, "src/lib"); from checks import synthetic_sentence; print(synthetic_sentence())'`.
In the sandbox with the example judge policy from M-04 enforcing, the synthetic prompt is blocked
and "Reply with OK" is allowed.

**Discuss:**
* A custom policy can check Singapore identifiers in prompts (G-02, M-04); `class.sg_*` tags and ABAC masks operate on data at rest (D-08). Service policies cover Unity Gateway traffic, not Genie's own model calls.
* `ai_query` route support and batch telemetry differ from the gateway API route (F-03); see the [configuration notes](configuration-notes.md#ai_query-and-model-service-support).
* Schema-level `EXECUTE` covers the model services in the schema; direct `EXECUTE` grants can scope access to individual services.
* F-01 observes who can call AI Functions. B-07 inventories `system.ai` grants, which are broad by default. F-02 tests a separate question: what data the caller can send.

---

## 5 · Agents, Search, MCP and Apps (deck: Layer 5)
**Demo results in the sandbox configuration**

| Test | clinician | pii_reader | outsider |
|---|---|---|---|
| V-01 Vector Search index needs SELECT | allowed | allowed | denied |
| V-02 retrieval returns only masked text | no cleartext PII | no cleartext PII | denied |
| T-01 MCP tool = UC function with EXECUTE (exact tool, result 40) | allowed | denied | denied |
| T-02 DBSQL MCP runs SQL as the caller (fixture rows) | masked | cleartext | masked |
| S-01 agent endpoint needs CAN QUERY | allowed | denied | denied |
| X-01 MLflow traces need experiment permission (seeded trace returned) | denied | allowed | denied |

**Audit observation:** R-01 records the Knowledge Assistant endpoint state and who can query it.
Invocation is opt-in (M-12).

**Show:**
* Unity Gateway > MCPs: the managed servers are UC objects with grants.
* The lab tool function in `<schema>_tools` with EXECUTE for clinicians only.
* M-09 Playground, M-10 Genie Code and M-11 Apps as short UI walk-throughs.

**Discuss:** the same identities can be used across controls, but the object and permission model
vary: Unity Catalog grants on functions, indexes and model services; ACLs on serving endpoints
and MLflow experiments; and app identity or on-behalf-of-user access (M-11).

### Who can build AI services (B-01 … B-09)
Using a service is one control; **creating** one is another, and it is where new routes to models and tools appear.
Each persona tries to create the objects that open a new AI route, with a unique per-run name. Anything it manages to create is deleted straight away and the deletion is verified; if it isn't, the result is FAIL "cleanup pending" and teardown removes it.
In the sandbox only the example clinicians hold `CREATE SERVICE`, scoped to `<schema>_tools`;
none of the personas holds `CREATE CONNECTION`. The probes don't send traffic through the two
dummy MCP connections: `lab_mcp_conn` has no persona `USE CONNECTION` grants (B-04);
`lab_mcp_conn_builders` grants it to clinicians (B-04b).

**Demo results in the sandbox configuration**

| Test | clinician | pii_reader | outsider |
|---|---|---|---|
| B-01 create a Unity Gateway model service | allowed (CREATE SERVICE) | denied | denied |
| B-02 add an external model provider (BYOK key) | allowed (CREATE SERVICE) | denied | denied |
| B-03 create a connection (first step to register an external MCP) | denied | denied | denied |
| B-04 register an MCP service on an existing connection | denied (no USE CONNECTION); SKIP if the error is a generic 403 that names neither the privilege nor the connection. If B-04b is allowed, the evidence notes that CREATE SERVICE + USE CONNECTION is sufficient; the SKIP stays a SKIP | denied | denied |
| B-04b register an MCP service on the builders' connection (granted USE CONNECTION) | allowed (CREATE SERVICE + USE CONNECTION; tests creation, not listing tools) | denied | denied |
| B-05 classic serving endpoint with an external model | if *Enforce Unity Gateway* is on, denied with the enforcement error naming the probe endpoint (generic 403 is SKIP); if off, creation can succeed with Workspace access | same | same |

**Audit observations**

| Test | What the lab records |
|---|---|
| B-06 | visible `CREATE SERVICE`, `CREATE CONNECTION` and `ALL PRIVILEGES` grants, including broad grants and principals outside `approved_builders`. Only a metastore-admin audit can establish that no other grants exist |
| B-07 | `system.ai` grants, including whether all users have `EXECUTE` |
| B-08 | whether *Enforce Unity Gateway* is on or off; MANUAL if not offered or readable (M-14). Covers new legacy creation routes and `databricks-*` pay-per-token endpoints, not existing external-model or provisioned-throughput endpoints |
| B-09 | visible serving endpoints read in full and classified, including external-model, provisioned-throughput and other AI endpoints. Absence of endpoints is conclusive only for a complete workspace-admin inventory. `system.serving` usage over `usage_lookback_days` (default 30) is supplementary |

**Show:**
* Catalog Explorer > `<schema>_tools` > Permissions: the sandbox's `CREATE SERVICE` grant for clinicians.
* Catalog Explorer > `<schema>_tools` > connections: `lab_mcp_conn_builders` grants `USE CONNECTION` to clinicians; `lab_mcp_conn` grants it to nobody.
* Unity Gateway > Models / MCPs > **+** as a user without `CREATE SERVICE`: creation fails (M-13).
* The B-06 evidence: grants visible to the audit identity that let a principal create services or connections.
* Settings > Advanced > **Enforce Unity Gateway** (M-14), and the B-05 response for each persona.

**Discuss:**
* Model, provider and MCP services require `CREATE SERVICE` on the target schema; an external MCP also needs a connection (`CREATE CONNECTION`, or `USE CONNECTION` on an existing one). Schema-level grants to a builders group scope creation to that group and schema; catalog-level grants to `account users` open a broader creation surface.
* Provider services hold API keys without exposing them to callers. `MANAGE` controls who can change the provider configuration.
* `system.ai` access can remain broad or be granted by group (B-07, M-13), depending on who uses the built-in models and MCPs.
* B-05 shows whether the workspace allows the new classic route; B-08 and B-09 separately record the setting and existing routes. A complete B-09 inventory with no legacy external-model or provisioned-throughput endpoints, combined with enforcement on, shows that those routes are absent and cannot be newly created at the time of the check.
* For an enforcement rollout, [M-14](manual-steps.md#m-14--enforce-unity-gateway) starts with B-09, then reviews usage, moves workloads to model services or external model providers, and validates that legacy request counts have dropped to zero before enabling the setting. The [migration guide](https://docs.databricks.com/aws/en/ai-gateway/migrate-to-unity-gateway) covers workload compatibility; the [configuration notes](configuration-notes.md#workloads-that-still-use-legacy-endpoints) explain why V-01, V-02, S-01 and F-02 can skip with enforcement on.
* B-09 uses the serving endpoint list and, with account-admin access, `system.serving.endpoint_usage` and `system.serving.served_entities`. Usage records typically arrive within about an hour, sometimes longer for new workspaces. [Usage tracking](https://docs.databricks.com/aws/en/ai-gateway/configure-ai-gateway-endpoints) determines coverage; today's tracking flag and an empty usage history do not establish that there was no traffic.
* For retrospective review or an alert on endpoint creation, filter `system.access.audit` on `service_name = 'serverlessRealTimeInference' AND action_name = 'createServingEndpoint'`, regardless of the enforcement setting. Restricted serverless egress (M-02) is a separate control for Model Serving calls to external models.

---

## 6–7 · Cost and audit (deck: Layers 6 and 7)
**Workspace audit observations**

| Test | What the lab records |
|---|---|
| C-01 | the Genie warehouse's configured `statement_timeout`, plus the effective `SET STATEMENT_TIMEOUT` value when readable. The warehouse field may need a manual check |
| C-02 | whether budgets are present (MANUAL; `account_checks.py` lists them as an inventory) |

**Evidence expected from the lab's sandbox configuration**

| Test | Expected sandbox evidence |
|---|---|
| O-01 | `genieStartConversationMessage` succeeded on the lab agent for clinician and PII reader (and not for the outsider), in the suite window |
| O-02 | clinician and PII reader usage rows on `lab_chat` in `system.ai_gateway.usage`, in the suite window |
| O-03 | the outsider's `genieStartConversationMessage` on the lab agent with HTTP 403/404 |
| G-04 | `lab_chat` inference rows of the G-01 request for clinician and PII reader, none from the outsider |

Run the evidence job the next day: `databricks bundle run -t <target> lab_evidence` (or pass
`--params evidence_run_id=<suite>`). Each check is correlated with the suite run's time window, this
workspace, the lab's Genie Agent / model service and the persona IDs; it reports **PENDING** until the logs
are in, for up to `log_latency_hours` (24 h) after the run. Missing sandbox evidence after that
window is reported as FAIL for the evidence check, not as a workspace configuration verdict.

---

## Wrap-up
1. `scripts/export_results.sh <target> <warehouse-id> <catalog>` writes `evidence/results-<date>.csv` (latest suite run, including MISSING / PENDING rows).
2. Fill in the [evidence template](evidence-template.md) and attach the CSV and `evidence/account_checks.json`.
3. Compare audit observations with your targets and note owners and dates for any mismatches or open manual checks.
4. Remove the lab when done: first `scripts/teardown.sh --dry-run <target> <warehouse-id> <catalog>` to see exactly what will be deleted (only proven `lab_manifest` items and identities bootstrap recorded for this target and workspace) and what is listed for manual review, then without `--dry-run`. It stops before `bundle destroy` if any deletion fails. Identity cleanup is a separate, retryable step: `scripts/teardown_identities.sh <target> <workspace-id>`.
