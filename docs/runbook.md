# GenAI governance lab runbook: governing Genie and GenAI in your workspace

A facilitator guide for a working session of about 2¼ hours that follows the seven layers of the deck. For
each layer: **show** where the control lives, **run** the lab, **check** the expected result, then
**record** it in the [evidence template](evidence-template.md).

**Roles:** a facilitator (drives the CLI and UI), a workspace admin, an account admin (for Layers
1 and 6), and a security reviewer who signs off.

| Time | Layer | Tests |
|---|---|---|
| 0:00 | Before the session | preflight (results from the day-before run) |
| 0:10 | 1 Platform and residency | P-01 … P-08, M-01, M-02, M-06 |
| 0:30 | 2 Data governance | D-01 … D-08, M-03 |
| 0:50 | 4 Genie Agents and Genie One | A-01 … A-05, M-07, M-08 |
| 1:10 | 3 Models, Unity Gateway, AI Functions | G-01 … G-03, F-01 … F-03, M-04, M-05, M-09 |
| 1:30 | 5 Agents, Search, MCP, Apps | V-01, V-02, T-01, T-02, S-01, X-01, R-01, M-10 … M-12 |
| 1:45 | Who can build AI services | B-01 … B-07, M-13 |
| 1:55 | 6–7 Cost and audit | C-01, C-02, O-01 … O-03, G-04 |
| 2:05 | Wrap-up (ends 2:15) | sign-off, teardown plan |

Test IDs `T-01`/`T-02` are the automated MCP tests; `M-01` … `M-14` are always manual steps.

Layer 4 comes before Layer 3 because it reuses the data from Layer 2 and is the part most people want to see.

---

## 0 · Before the session (day before)
> **No CLI?** Run the whole lab from a Git folder with one notebook: [workspace-setup.md](workspace-setup.md)
> (deploy target `workspace`, then `00_run_lab` actions `preflight`, `personas`, `run`). The steps below are the CLI path.

1. Create the CLI profiles and confirm access: `databricks -p <profile> current-user me`.
2. Pick a catalog for the sandbox and a **serverless SQL warehouse**; note its ID.
3. Create the personas: `WORKSPACE_PROFILE=<profile> scripts/bootstrap_personas.sh <account-profile> <account-workspace-id> <prefix> <your-email>`.
   Without account admin, have your identity team create the 3 service principals and 4 groups
   (names in `.lab.env.example`) and fill in `.lab.env` by hand.
4. Run `scripts/run_demo.sh customer <warehouse-id> <catalog>` once end to end, so the session
   starts from a working state. It uses one suite run ID for setup, personas, collector and audit; the
   dashboard **GenAI governance lab – results** shows that suite only, with MISSING for anything that didn't record a result.
5. Grant the deployer `SELECT` on `system.access.audit` and `system.ai_gateway.usage` (needed for Layer 7).

> **Residency reminder.** On a workspace with "Enforce data processing within workspace Geography"
> on, some features are refused by design (Data Classification in the Asia Geo, `ai_mask` 2.0 in some regions,
> the Genie Code CLI). The lab treats a refusal as the expected result. P-07 passes only when the refusal names
> Geography / cross-Geo processing. For Data Classification the documented fallback is to apply the same
> `class.sg_*` tags by hand (M-03).

---

## 1 · Platform and residency (deck: Layer 1, Singapore residency)
**Show**
* Account console > Settings: the Geography enforcement setting (M-01) and partner-powered AI with its *enforce* option (M-06).
* Account console > Security: network policies and context-based ingress (M-02).
* Workspace settings > Security: IP access lists and token settings.

**Run:** `scripts/run_audit.sh customer_audit <warehouse-id> <catalog> <account-profile> <workspace-id>`. It refuses any target that isn't audit-only; in demo mode the audit already ran as part of `scripts/run_demo.sh` (same suite).

**Expect**
| Test | Expected |
|---|---|
| P-01 | region is the intended one (e.g. ap-southeast-1) |
| P-02 | IP access lists on, at least one allow list (MANUAL if the settings can't be read) |
| P-03 | PATs off, or a maximum lifetime of at most `max_token_days` (default 90) |
| P-05 / M-06 | partner-powered AI value recorded and agreed; enforced at account level if required |
| P-06 / M-01 | in-Geo enforcement on |
| P-07 | `ai_mask` 2.0 refused with the Geography / cross-Geo reason (PASS); refused as "not available in this region" (INFO); otherwise MANUAL with M-03 |
| P-08 | CSP state recorded. Decide whether patient-data workspaces need it (HIPAA and other standards); enabling it can't be undone, so rerun the lab afterwards |

**Discuss:** this is **configuration evidence**. No log shows which country processed a given
request, so the in-country Genie preview enrolment is confirmed per workspace with the account team.

---

## 2 · Data governance (deck: Layer 2, Unity Catalog is the main control)
**Show** in Catalog Explorer, on `genie_governance_lab.patient_visits`:
* the column masks on `nric` and `patient_name`, the row filter on `facility`;
* the system classification tag `class.phone_number` on `phone` (the tag Data Classification would apply), and the schema-level ABAC policy `lab_mask_tagged_pii`;
* the ABAC policy `lab_mask_sg_identifiers` (masks every column tagged `class.sg_nric`, `class.sg_fin` or `class.sg_work_permit_number`) and the table `sg_identifiers_simulated`, whose `nric` column carries a **manually set** `class.sg_nric` tag (SIMULATED, because automatic classification is unavailable under in-Geo enforcement);
* that `clinical_notes_raw` has no grants for the personas, while `clinical_notes_masked` does.

**Run:** the persona jobs (already run by `run_demo.sh`; rerun one live):
`databricks bundle run -t <target> lab_persona_clinician`

**Expect** (dashboard, filter by test). Values are compared with the seeded fixture rows (visits 4, 8, 12).
| Test | clinician | pii_reader | outsider |
|---|---|---|---|
| D-01 column mask | masked | cleartext | masked |
| D-02 row filter | 1 facility | 4 facilities | 1 facility |
| D-03 ABAC tag policy | masked | cleartext | masked |
| D-04 raw vs masked notes | denied / allowed | denied / allowed | denied / allowed |

| Test | Expected |
|---|---|
| D-06 `ai_mask` 1.0 | patient labels, NRICs and phone numbers removed from free text |
| D-07 `ai_mask` 2.0 (Beta) | SKIP on CSP; refused under in-Geo enforcement where the region needs cross-Geo; otherwise masks |
| D-05 / M-03 Data Classification | unavailable under in-Geo enforcement in the Asia Geo (needs cross-Geo processing); otherwise tags the PII columns |
| D-08 Singapore identifier tags | every column tagged `class.sg_*` is covered by an ABAC mask (PASS; manual tags labelled SIMULATED); INFO when nothing is tagged |

**Discuss:** one ABAC policy covers every column with the tag, in every table of the schema, for
SQL, notebooks, Genie and Genie Code alike. That is what scales to hundreds of clinical tables. For NRIC,
FIN and work permit numbers, Data Classification would tag the columns automatically, but under in-Geo
enforcement in Singapore it is unavailable; tag the columns by hand (M-03) and the same policy applies.

---

## 4 · Genie Agents and Genie One (deck: Layer 4)
**Show**
* The lab Genie Agent: its data sources (`patient_visits`, `clinical_notes_masked`) and **Share** settings: clinicians and PII readers have CAN RUN, outsiders nothing.
* That the agent itself has **no** masking or PII configuration.

**Run (UI, 4.1–4.3)**
1. 4.1 As a facilitator (admin), ask *"List 5 diabetes patients with their NRIC, name and facility"*. Admins who aren't in the PII group see masked values too.
2. 4.2 Open the persona results on the dashboard for A-01 … A-04.
3. 4.3 Switch the agent to **Agent** mode (if enabled on this workspace) and ask the same two questions. The masks and the row filter still apply (A-05, record by hand).

**Expect**
| Test | clinician | pii_reader | outsider |
|---|---|---|---|
| A-01 Genie applies the mask | masked | cleartext | denied |
| A-02 Genie applies the row filter | 1 facility | 4 facilities | denied |
| A-03 CAN RUN required | allowed | allowed | denied |
| A-04 only masked notes reach Genie (needs a non-empty answer) | no cleartext NRIC | no cleartext NRIC | denied |

**Also check:** M-07 (file upload default) and M-08 (Monitor tab shows the persona questions).

**Discuss:** Genie runs SQL as the asking user, and the embedded warehouse credential grants
compute only. Protect data in Unity Catalog; Genie inherits it.

---

## 3 · Models, Unity Gateway and AI Functions (deck: Layer 3)
**Show** Unity Gateway > Models > `<prefix>_chat` (default `lab_chat`; a bundle resource, `resources/demo/lab_chat.model_service.yml`):
* **Permissions:** EXECUTE for clinicians and PII readers only.
* **Rate limits:** 2 requests/min for the clinicians group.
* **Inference table:** `<prefix>_chat_payload` in the lab schema.
* **Policies:** the two guardrails from M-04 (sensitive-data Deny; LLM-as-a-judge for NRIC/FIN). Set `expected_policy` to the judge's name so G-02 checks the block came from it.

| Test | clinician | pii_reader | outsider |
|---|---|---|---|
| G-01 EXECUTE on the model service | allowed | allowed | denied (UC hides the service: 404) |
| G-02 NRIC prompt blocked by the intended guardrail | blocked | blocked | denied |
| G-03 rate limit (3 calls after a reset) | 2 allowed then rate_limited | allowed | denied |
| F-02 `ai_query` over the masked table (exact echo of every fixture row; INFO if the model refuses or rewords) | no cleartext NRIC reached the model | cleartext NRIC reached the model | no cleartext NRIC |
| F-03 `ai_query` via the lab's custom model service | not supported for this route (INFO) | same | same |

**Live demo (2 min):** in Playground pick `<prefix>_chat` and paste "Summarise: " followed by the synthetic sentence the lab generates (a fictional patient label, an NRIC with an invalid check letter and a phone number on the reserved level 5), for example from
`python3 -c 'import sys; sys.path.insert(0, "src/lib"); from checks import synthetic_sentence; print(synthetic_sentence())'`.
It is blocked by the judge policy from M-04. Then "Reply with OK" passes.

**Discuss:**
* There's no built-in Singapore NRIC class in the guardrails for prompts. Use a custom guardrail, and rely on Unity Catalog masks first (at rest, Data Classification's `class.sg_*` tags plus an ABAC policy do the job).
* `ai_query` supports only Databricks-provided models through Unity Gateway; model services you create aren't supported yet (F-03), and only usage tracking applies to `ai_query` batch calls.
* A schema-level EXECUTE grant silently opens every model service in the schema.
* AI Functions only see what UC lets the caller read (F-02). Who may call them at all is EXECUTE on `system.ai`, which is open by default.
* Guardrails cover Unity Gateway traffic, not Genie's own model calls.

---

## 5 · Agents, Search, MCP and Apps (deck: Layer 5)
| Test | clinician | pii_reader | outsider |
|---|---|---|---|
| V-01 Vector Search index needs SELECT | allowed | allowed | denied |
| V-02 retrieval returns only masked text | no cleartext PII | no cleartext PII | denied |
| T-01 MCP tool = UC function with EXECUTE (exact tool, result 40) | allowed | denied | denied |
| T-02 DBSQL MCP runs SQL as the caller (fixture rows) | masked | cleartext | masked |
| S-01 agent endpoint needs CAN QUERY | allowed | denied | denied |
| X-01 MLflow traces need experiment permission (seeded trace returned) | denied | allowed | denied |
| R-01 Knowledge Assistant configuration (admin, audit) | recorded: state, who can query; invocation opt-in | | |

**Show:**
* Unity Gateway > MCPs: the managed servers are UC objects with grants.
* The lab tool function in `<schema>_tools` with EXECUTE for clinicians only.
* M-09 Playground, M-10 Genie Code and M-11 Apps as short UI walk-throughs.

**Discuss:** every agent surface resolves to a Unity Catalog grant on a concrete object: a function, an index, an endpoint, an experiment or a model service. The same identities and groups govern all of them.

### Who can build AI services (B-01 … B-09)
Using a service is one control; **creating** one is another, and it is where new routes to models and tools appear.
Each persona tries to create the objects that open a new AI route, with a unique per-run name. Anything it manages to create is deleted straight away and the deletion is verified; if it isn't, the result is FAIL "cleanup pending" and teardown removes it.
In the lab only clinicians hold `CREATE SERVICE`, and only on `<schema>_tools`; nobody holds `CREATE CONNECTION`. There are two dummy MCP connections in `<schema>_tools` that never send traffic: `lab_mcp_conn`, where nobody holds `USE CONNECTION` (B-04), and `lab_mcp_conn_builders`, where only clinicians do (B-04b).

| Test | clinician | pii_reader | outsider |
|---|---|---|---|
| B-01 create a Unity Gateway model service | allowed (CREATE SERVICE) | denied | denied |
| B-02 add an external model provider (BYOK key) | allowed (CREATE SERVICE) | denied | denied |
| B-03 create a connection (first step to register an external MCP) | denied | denied | denied |
| B-04 register an MCP service on an existing connection | denied (no USE CONNECTION); SKIP if the error is a generic 403 that names neither the privilege nor the connection. If B-04b is allowed, the evidence notes that CREATE SERVICE + USE CONNECTION is sufficient; the SKIP stays a SKIP | denied | denied |
| B-04b register an MCP service on the builders' connection (granted USE CONNECTION) | allowed (CREATE SERVICE + USE CONNECTION; creation only, tools are never listed) | denied | denied |
| B-05 classic serving endpoint with an external model | denied when *Enforce Unity Gateway* is on (PASS needs the error "Creating external model endpoint '<name>' is disabled for this workspace. Please use Unity Gateway." naming the probe's endpoint; generic 403 → SKIP); with the setting off: **allowed → FAIL (finding)** | same | same |
| B-06 (audit) CREATE SERVICE / CREATE CONNECTION / ALL PRIVILEGES only for `approved_builders` | admin: PASS as metastore admin with no other grants; MANUAL if not metastore admin | | |
| B-07 (audit) who may use every `system.ai` model and MCP | admin: recorded | | |
| B-08 (audit) *Enforce Unity Gateway* workspace setting (`enforce_ai_gateway_v3_only`; undocumented; discovered from workspace settings metadata; best effort; may change without notice; the UI is authoritative): `databricks-*` pay-per-token endpoints disabled, no **new** external-model / provisioned-throughput endpoints outside Unity Gateway | admin: PASS when on; FAIL when off; MANUAL if not offered or not readable (M-14). PASS says nothing about existing endpoints: they stay queryable, so inventory them (`system.serving.served_entities` or the serving endpoint list) | | |
| B-09 (audit) legacy AI endpoint inventory: every serving endpoint read in full and classified; `system.serving` usage over `usage_lookback_days` (default 30) attached as supplementary evidence | admin: PASS as a workspace admin with every endpoint classified and no AI serving endpoints at all; FAIL if any external-model / PT endpoint exists (it stays queryable with enforcement; any caller); INFO for other AI endpoints (agents, custom models, Knowledge Assistant, listed `databricks-*`) to migrate; MANUAL for a non-admin caller or incomplete classification. Usage never changes the verdict | | |

**Show:**
* Catalog Explorer > `<schema>_tools` > Permissions: `CREATE SERVICE` for clinicians, nothing else.
* Catalog Explorer > `<schema>_tools` > connections: `lab_mcp_conn_builders` grants `USE CONNECTION` to clinicians; `lab_mcp_conn` grants it to nobody.
* Unity Gateway > Models / MCPs > **+** as a user without `CREATE SERVICE`: creation fails (M-13).
* The B-06 evidence: every grant in the metastore that lets someone create services or connections.
* Settings > Advanced > **Enforce Unity Gateway** (M-14), and the B-05 error each persona got with it on.

**Discuss:**
* Model services, provider services and MCP services all need `CREATE SERVICE` on the target schema; external MCP servers also need a connection (`CREATE CONNECTION`, or `USE CONNECTION` on an existing one). Give these to a small builders group on a dedicated schema, never to `account users` at catalog level.
* Provider services hold the API keys: callers never see them, and only `MANAGE` holders can change them.
* `system.ai` grants `EXECUTE` to all users by default, so every user can use every built-in model and MCP (B-07). Revoke and regrant per group if that is too broad.
* **B-05 / B-08: creating a new route outside Unity Gateway is blocked by a workspace setting, not by a grant; routes that already exist stay open.** Without it, any user with the *Workspace access* entitlement can create a classic Model Serving endpoint with an external model, pointing at any URL with their own API key, bypassing Unity Gateway provider services and `CREATE SERVICE` (B-05 FAIL). With **Enforce Unity Gateway** on (Settings > Advanced, workspace admin; B-08), "You can no longer create external model serving endpoints without Unity Gateway" and the same holds for provisioned throughput; Databricks-provided pay-per-token endpoints (`databricks-*`) are disabled. Caveats from the [migration guide](https://docs.databricks.com/aws/en/ai-gateway/migrate-to-unity-gateway):
  1. **Existing** external-model and provisioned-throughput endpoints "stay intact and remain queryable", so B-08 PASS doesn't close them. **B-09** inventories them (serving endpoint list plus `system.serving.endpoint_usage` / `served_entities`; the tables need grants from an account admin). B-08 PASS + B-09 PASS (workspace admin, complete classification) = no existing legacy external-model or provisioned-throughput route right now; historical usage is a separate manual review. Otherwise migrate or delete what B-09 lists. The list API only returns endpoints the caller can see, so B-09 is MANUAL for a non-admin caller. B-09's usage history is evidence, not proof: `endpoint_usage` only records endpoints with [usage tracking](https://docs.databricks.com/aws/en/ai-gateway/configure-ai-gateway-endpoints) on, the flag shows today's state only, and records arrive within about an hour.
  2. Turning it on breaks what still depends on legacy endpoints: "Creating AI Search endpoints through Unity Gateway is not yet supported. Knowledge Assistant and Multi-Agent Supervisor agents that depend on AI Search endpoints don't work"; `ai_query` supports only `system.ai` model services; custom agents on model serving endpoints can't use authentication passthrough to model services; Databricks Apps that use a serving endpoint resource stop working until their code queries a model service and the app's service principal has `EXECUTE` on it. Migrate those first (the guide's steps 1–5), then enable.
  3. Defence in depth for the endpoints that remain: a restricted serverless egress network policy (M-02) also applies to "Model Serving endpoints that call external models", and an alert on `createServingEndpoint` in `system.access.audit` (`service_name = 'serverlessRealTimeInference'`) detects new endpoints if the setting is ever turned off.
  * What to expect in a test workspace with the setting on: B-05 is denied for every persona (PASS). A vector index with managed embeddings on a `databricks-*` embedding endpoint, an existing agent endpoint that calls a `databricks-*` chat endpoint, and F-02 (`ai_query` on a `databricks-*` endpoint name) stop working, so V-01, V-02, S-01 (clinician) and F-02 record SKIP with the reason (gap 30).

---

## 6–7 · Cost and audit (deck: Layers 6 and 7)
| Test | Expected |
|---|---|
| C-01 | `statement_timeout` > 0 on the Genie warehouse itself (MANUAL if the API doesn't return the field; the effective `SET STATEMENT_TIMEOUT` value is in the evidence) |
| C-02 | budgets exist (MANUAL; `account_checks.py` lists them as an inventory) |
| O-01 | `genieStartConversationMessage` succeeded on the lab agent for clinician and PII reader (and not for the outsider), in the suite window |
| O-02 | clinician and PII reader usage rows on `lab_chat` in `system.ai_gateway.usage`, in the suite window |
| O-03 | the outsider's `genieStartConversationMessage` on the lab agent with HTTP 403/404 |
| G-04 | `lab_chat` inference rows of the G-01 request for clinician and PII reader, none from the outsider |

Run the evidence job the next day: `databricks bundle run -t <target> lab_evidence` (or pass
`--params evidence_run_id=<suite>`). Each check is correlated with the suite run's time window, this
workspace, the lab's Genie Agent / model service and the persona IDs; it reports **PENDING** until the logs
are in, for up to `log_latency_hours` (24 h) after the run, and FAIL after that, instead of guessing.

---

## Wrap-up
1. `scripts/export_results.sh <target> <warehouse-id> <catalog>` writes `evidence/results-<date>.csv` (latest suite run, including MISSING / PENDING rows).
2. Fill in the [evidence template](evidence-template.md) and attach the CSV and `evidence/account_checks.json`.
3. Agree owners and dates for every FAIL and MANUAL item.
4. Remove the lab when done: first `scripts/teardown.sh --dry-run <target> <warehouse-id> <catalog>` to see exactly what will be deleted (only proven `lab_manifest` items and identities bootstrap recorded for this target and workspace) and what is listed for manual review, then without `--dry-run`. It stops before `bundle destroy` if any deletion fails. Identity cleanup is a separate, retryable step: `scripts/teardown_identities.sh <target> <workspace-id>`.
