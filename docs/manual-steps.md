# GenAI governance lab: manual steps

Some controls are set in the UI or the account console only, or have no API to read them back.
Each step below says **where**, **what to do**, and **how to check** it, so the effect can still be
proven. Record the outcome in the [evidence template](evidence-template.md).

| ID | Control | Who |
|---|---|---|
| M-01 | Residency: Enforce data processing within workspace Geography | Account admin |
| M-02 | Context-based ingress and serverless egress | Account admin |
| M-03 | Data Classification availability, or manual `class.sg_*` tags | Workspace admin |
| M-04 | Service policy (guardrail) on a Unity Gateway service | Workspace admin, `MANAGE` on the service |
| M-05 | Rate limits on a Unity Gateway service | Workspace admin |
| M-06 | Account-level Genie One switch and partner-powered AI enforcement | Account admin |
| M-07 | File upload and download/export settings | Workspace admin |
| M-08 | Genie Agent Monitor tab and Governance Hub | Genie Agent manager / account admin |
| M-09 | Playground shows only model services you may EXECUTE | any user |
| M-10 | Genie Code runs as the user; tool approval | any user, workspace admin |
| M-11 | Databricks Apps: app identity vs on-behalf-of-user | app owner |
| M-12 | Agent Bricks Knowledge Assistant: permissions and residency | workspace admin |
| M-13 | Who can create model, provider and MCP services | metastore admin |
| M-14 | Enforce Unity Gateway (no new external-model or provisioned-throughput endpoints outside the gateway; existing ones stay queryable) | Workspace admin |

---

## M-01 · Enforce data processing within workspace Geography
* **Where:** Account console > Settings > (Feature enablement / Geography).
* **Do:** confirm the setting is **on** for each workspace that must keep processing in its Geo.
* **Check:** run audit mode; P-07 should report a cross-Geo-only feature as *refused* naming Geography / cross-Geo processing (PASS; a plain "not available in this region" is INFO), and M-03 should show Data Classification as unavailable.
* **Note:** this proves the configuration and its effect. No log records which country or model processed a given Genie request. If you're enrolled in an in-country Genie preview, confirm the enrolment per workspace with your account team.
* [Designated Services](https://docs.databricks.com/aws/en/resources/designated-services)

## M-02 · Context-based ingress and serverless egress
* **Where:** Account console > Security > Network policies / Context-based ingress.
* **Do:** confirm a restricted serverless network policy is attached to the workspace and that ingress rules match your access model (including partner platforms such as Slack, Teams or Power BI if you use them).
* **Check:** `scripts/account_checks.py` lists the policies (P-04).
* [Context-based ingress](https://docs.databricks.com/aws/en/security/network/front-end/context-based-ingress) · [Serverless network policies](https://docs.databricks.com/aws/en/security/network/serverless-network-security/network-policies)

## M-03 · Data Classification availability, or manual Singapore identifier tags
* **Background:** automatic Data Classification detects Singapore identifiers and tags the columns `class.sg_nric`, `class.sg_fin` and `class.sg_work_permit_number` (only in workspaces in SG regions). In the Asia Geo, Data Classification **needs cross-Geo processing** (Designated Services), so with "Enforce data processing within workspace Geography" on it is **unavailable**. The documented fallback is to apply the same tags by hand.
* **Where:** Catalog > *your catalog* > Details > Data Classification.
* **Do:**
  1. Try to enable it. With in-Geo enforcement on in Singapore it should be unavailable (D-05 evidence).
  2. Where it is available (cross-Geo processing allowed), run a scan and confirm it tags NRIC / FIN / work permit columns, plus `class.phone_number` on phone columns (the lab's synthetic phone numbers use a reserved level, so a scan may not tag them; the lab sets that tag itself).
  3. Where it isn't, tag the columns that hold NRIC, FIN or work permit numbers by hand: `ALTER TABLE <t> ALTER COLUMN <c> SET TAGS ('class.sg_nric')` (needs `APPLY TAG`). The lab does this on one column of `sg_identifiers_simulated` and labels it SIMULATED.
* **Check:** audit mode D-08 lists every column with a `class.sg_*` tag and the ABAC mask policy that covers it (PASS when all are covered, INFO when no column is tagged). In demo mode the schema policy `lab_mask_sg_identifiers` covers them; a column that also has a classic column mask (like `patient_visits.nric`) shouldn't get the tag too, or the two masks conflict.
* [Data Classification](https://docs.databricks.com/aws/en/data-governance/unity-catalog/data-classification) · [Classification tags](https://docs.databricks.com/aws/en/data-governance/unity-catalog/data-classification-tags) · [Designated Services](https://docs.databricks.com/aws/en/resources/designated-services)

## M-04 · Service policies (guardrails) on the lab model service
* **Prerequisite:** the **Unity Gateway Service Policies** preview (workspace Previews page). Attaching policies is UI-only.
* **Where:** Unity Gateway > Models > `<prefix>_chat` (default `lab_chat`) > **Policies** > New policy > Service policy.
* **Do (reference configuration):**
  1. **Built-in Sensitive Data Detection**, input and output, action **Deny**, classes *Email address* and *Phone number*. Applies to all account users.
  2. **Custom > LLM-as-a-judge** (evaluator: a `system.ai` model service), with a prompt that flags Singapore NRIC/FIN numbers (`[STFGM]` + 7 digits + letter) and ignores masked values. Rank 2.
* **Check:** set the bundle variable `expected_policy` to the judge policy's name and rerun the persona jobs. G-02 turns from MANUAL to PASS ("blocked") only if one `databricks_service_policy` decision record names that policy with an **enforced** `action: deny` (not log / monitor) and the response contains no successful completion; any other block (unnamed, another policy, log mode, no expected_policy set) is INFO with the decision records. An email prompt is blocked by the built-in policy; a clean prompt still passes.
* **Good to know:**
  * The built-in guardrail classes cover Global, US, UK and India identifiers. There is **no Singapore NRIC/FIN class for prompts**. (At rest it's different: Data Classification has `class.sg_nric` / `class.sg_fin` / `class.sg_work_permit_number`; see M-03.)
  * Custom **SQL** policy functions are compiled to CEL: regular expressions (`RLIKE`, `regexp_like`, `to_json`) are rejected ("cannot be transpiled to CEL").
  * Custom **Python** policy functions support regex (`nric_guard_py` in the lab schema), **but** they are not available where the workspace's serverless network policy restricts internet access, and then the policy **denies every request** (fail closed). Use the LLM-as-a-judge guardrail in such workspaces.
* **Remember:** service policies cover traffic through Unity Gateway. They don't sit in front of Genie's own model calls, which is why the lab protects patient data with Unity Catalog first.
* [Service policies](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/) · [Python policies](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/python-policies)

## M-05 · Rate limits
* **Where:** Unity Gateway > *your model service* > Rate limits.
* **Do:** confirm the per-group limit (the lab's `<prefix>_chat` sets 2 requests per minute for the clinicians group in `resources/demo/lab_chat.model_service.yml`), or set one on your own service.
* **Check:** G-03. After a pause for the window to reset, the clinician makes 3 calls and must see exactly *allowed, allowed, rate_limited* (HTTP 429 in the evidence); PII readers see 3 × *allowed*, outsiders 3 × *denied*. A 429 for anyone but the clinicians isn't the lab limit and is reported as such.
* [Rate limits](https://docs.databricks.com/aws/en/ai-gateway/rate-limits)

## M-06 · Account-level Genie One switch and partner-powered AI enforcement
* **Where:** Account console > Settings > Feature enablement.
* **Do:** record the Genie switch and the partner-powered AI value and whether it is **enforced**. Agree any change with your account team first: turning partner-powered AI off disables Genie Agents chat and agent mode, Genie One chat and Genie Code agentic mode.
* **Check:** `scripts/account_checks.py` (P-05) and audit mode P-05 show the same value.
* [Partner-powered AI](https://docs.databricks.com/aws/en/databricks-ai/partner-powered)

## M-07 · File upload and download/export
* **Where:** Workspace settings > Advanced / Genie.
* **Do:** decide whether users may upload files into Genie Agents and Genie One (on by default) and whether results may be downloaded.
* **Check:** as a clinician test user, the paperclip in the lab Genie Agent is shown or hidden accordingly.
* [File upload](https://docs.databricks.com/aws/en/genie-agents/file-upload) · [Download settings](https://docs.databricks.com/aws/en/admin/workspace-settings/notebooks)

## M-08 · Monitor tab and Governance Hub
* **Where:** the lab Genie Agent > Monitor; Account console > Governance > AI / Cost.
* **Do:** after the persona runs, open the Monitor tab: each persona's questions, status and ratings appear.
* **Check:** the questions match the ones in `30_persona_probe.py`.
* [Genie monitoring](https://docs.databricks.com/aws/en/genie-agents/monitor) · [Governance Hub](https://docs.databricks.com/aws/en/admin/governance-hub/ai)

## M-09 · Playground
* **Where:** AI/ML > Playground, signed in as a test user in `lab_clinicians` and then as one who isn't.
* **Check:** `genie_governance_lab...lab_chat` appears only for users with EXECUTE on it (G-01 shows the API side). Prompts there go through the same guardrails.

## M-10 · Genie Code
* **Where:** a notebook on the lab schema, Genie Code panel.
* **Do:** ask it to "show 5 rows of patient_visits with NRIC".
* **Check:** the generated query returns masked values for a non-PII user, because Genie Code runs code as the user. In agent mode, check the tool-approval setting (per action vs auto-approve) in workspace settings.
* [Genie Code](https://docs.databricks.com/aws/en/genie-code/)

## M-11 · Databricks Apps
* **Where:** Compute > Apps > an app > Authorization.
* **Check:** the app runs as its own service principal by default; on-behalf-of-user needs scopes that a workspace admin approves. Patient-level apps should use on-behalf-of-user so UC filters and masks apply per user.
* [App authorization](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/auth)

## M-12 · Knowledge Assistant (Agent Bricks)
* **Where:** AI/ML > Agents > the Knowledge Assistant > Permissions.
* **Check:** CAN QUERY only for intended groups (R-01 lists who can query and the endpoint state; it doesn't call the endpoint). Residency: Designated Services lists Knowledge Assistant as needing cross-Geo processing in the Asia Geo. Confirm with your account team before using it for in-country data. To also send one synthetic question, tag a Knowledge Assistant over **synthetic** content `lab_synthetic=true` and set `ka_invoke=true`; R-01 then records only the HTTP status and correlation headers.
* [Designated Services](https://docs.databricks.com/aws/en/resources/designated-services)

## M-13 · Who can create model, provider and MCP services
* **Where:** Catalog Explorer > each catalog and schema > Permissions; Unity Gateway > Models / MCPs > **+**.
* **Do:** confirm `CREATE SERVICE` and `CREATE CONNECTION` are held only by a named builders group, on dedicated schemas, and not by `account users` on any catalog. Decide whether every user should keep `EXECUTE` on `system.ai` (the default).
* **Check:** audit mode B-06 lists every such grant: FAIL for grants to all users or to anyone outside `approved_builders`, PASS when there are none and the audit ran as a metastore admin, otherwise MANUAL (it can only see grants visible to the caller). B-07 records the `system.ai` grants. In demo mode B-01 … B-05 test the effect per persona (B-05: creating new classic serving endpoints with external models is blocked by a setting, not a grant, and existing ones stay queryable: M-14); B-04b shows the allowed path (a builder with `CREATE SERVICE` and `USE CONNECTION` on a granted connection registers an MCP service). Signed in as a test user without `CREATE SERVICE`, creating a model or MCP from **+ Model** / **+ MCP** in a schema fails with a permission error.
* [Model services: privileges](https://docs.databricks.com/aws/en/ai-gateway/model-services) · [Model provider services](https://docs.databricks.com/aws/en/ai-gateway/model-provider-services) · [Register an MCP](https://docs.databricks.com/aws/en/ai-gateway/register-mcp-service) · [Govern an MCP](https://docs.databricks.com/aws/en/ai-gateway/govern-mcp-service)

## M-14 · Enforce Unity Gateway
* **Where:** workspace **Settings > Advanced > Enforce Unity Gateway** (workspace admin); the UI is authoritative. Not on the Previews page. B-08 reads the settings v2 key `enforce_ai_gateway_v3_only` (**undocumented; discovered from workspace settings metadata; best effort; may change without notice; the UI is authoritative**). If the key changes or disappears, B-08 is MANUAL and this UI check decides.
* **Do:** run audit mode and read **B-09** first (legacy AI endpoint inventory). B-08 PASS + B-09 PASS (workspace admin, complete classification) = no existing legacy external-model or provisioned-throughput route right now; historical usage is a separate manual review. MANUAL means the list may be incomplete (non-admin caller) or some endpoint couldn't be classified, so review the endpoints by hand; FAIL lists external-model / provisioned-throughput endpoints that would stay queryable; INFO lists other AI endpoints that break or need migrating. Review historical usage separately: B-09 attaches it as evidence, but `endpoint_usage` only records endpoints with [usage tracking](https://docs.databricks.com/aws/en/ai-gateway/configure-ai-gateway-endpoints) on, so absence of traffic is not proof. Then follow the [migration guide](https://docs.databricks.com/aws/en/ai-gateway/migrate-to-unity-gateway): find legacy traffic in `system.serving.endpoint_usage` and `system.serving.served_entities` (account admin), move each workload to a model service or external model provider, check that legacy traffic has dropped to zero. Then turn the setting on.
* **Know before you enable:** Databricks-provided pay-per-token endpoints (`databricks-*`) are disabled, and new external-model and provisioned-throughput serving endpoints can't be created outside Unity Gateway; existing ones stay queryable, so inventory them (`system.serving.served_entities` or the serving endpoint list). AI Search endpoints can't be created through Unity Gateway yet, and Knowledge Assistant and Multi-Agent Supervisor agents that depend on AI Search don't work; `ai_query` supports only `system.ai` model services; custom agents on model serving endpoints can't use authentication passthrough; Databricks Apps that use a serving endpoint resource stop working until their code queries a model service and the app's service principal has `EXECUTE` on it ([KB](https://kb.databricks.com/unity-catalog/migration-guide-moving-to-unity-ai-gateway)). The setting is offered only in workspaces that have legacy AI Gateway endpoints; if it's missing, ask your account team after migrating.
* **Check:** audit mode B-08 reads the setting (PASS on, FAIL off, MANUAL if not offered or not readable). PASS covers only new endpoints and the `databricks-*` pay-per-token endpoints, not existing external-model endpoints: B-09 covers those. In demo mode B-05 is denied for every persona with the error "Creating external model endpoint '<name>' is disabled for this workspace. Please use Unity Gateway." A query to a `databricks-*` pay-per-token endpoint returns `PERMISSION_DENIED` "… is disabled for this workspace. Please use Unity Gateway."
* [Migrate to Unity Gateway](https://docs.databricks.com/aws/en/ai-gateway/migrate-to-unity-gateway) · [System tables (`system.serving.*`, Public Preview)](https://docs.databricks.com/aws/en/admin/system-tables/) · [Serverless egress for Model Serving](https://docs.databricks.com/aws/en/security/network/serverless-network-security/network-policies)
