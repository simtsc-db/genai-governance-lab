# GenAI governance lab: manual steps

These manual tests cover controls observed in the UI or account console, including settings the
lab can't read back through an API. Record the state or behavior in the
[evidence template](evidence-template.md); PASS or FAIL in your review is a comparison with
**your targets**. The healthcare data and roles are synthetic examples you can map to your own
roles. Example demo outcomes follow from the lab's sandbox configuration. See the
[configuration notes](configuration-notes.md) for settings that change those outcomes.

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
* **Do:** record whether Geography enforcement is on or off for the workspace (P-06).
* **Check:** P-07 records whether a cross-Geo feature probe ran or was refused. A refusal naming Geography or cross-Geo processing identifies the enforcement reason; "not available in this region" is INFO. In the Asia Geo, M-03 can record whether Data Classification is available.
* **Note:** the setting and the probe show configuration and feature availability. No log records which country or model processed a given Genie request. For in-country Genie previews, the account team can confirm enrolment per workspace.
* [Designated Services](https://docs.databricks.com/aws/en/resources/designated-services)

## M-02 · Context-based ingress and serverless egress
* **Where:** Account console > Security > Network policies / Context-based ingress.
* **Do:** record the attached serverless network policy, if any, and the ingress rules. Note which clients can connect, including Slack, Teams or Power BI where they are in scope.
* **Check:** `scripts/account_checks.py` lists serverless network policies (P-04); the console shows the workspace attachment and context-based ingress configuration.
* [Context-based ingress](https://docs.databricks.com/aws/en/security/network/front-end/context-based-ingress) · [Serverless network policies](https://docs.databricks.com/aws/en/security/network/serverless-network-security/network-policies)

## M-03 · Data Classification availability, or manual Singapore identifier tags
* **Background:** automatic Data Classification can tag `class.sg_nric`, `class.sg_fin` and `class.sg_work_permit_number` in Singapore-region workspaces. In the Asia Geo, it uses cross-Geo processing and is unavailable with Geography enforcement on. Manual tags are another input to the same masking policy.
* **Where:** Catalog > *your catalog* > Details > Data Classification.
* **Do:**
  1. Record whether Data Classification is available and enabled (D-05 evidence), including the reason if the UI reports one.
  2. Where a scan is part of the test, record the tags it applies, including any NRIC, FIN, work permit or phone tags. The synthetic phone numbers use a reserved level and the scan may not tag them; the sandbox sets `class.phone_number` itself.
  3. To exercise manual tagging, a user with `APPLY TAG` can run `ALTER TABLE <t> ALTER COLUMN <c> SET TAGS ('class.sg_nric')`. The sandbox labels its manually tagged `sg_identifiers_simulated` column SIMULATED.
* **Check:** D-08 records the `class.sg_*` columns and whether an ABAC mask covers them; no matching tags is INFO. In the sandbox `lab_mask_sg_identifiers` demonstrates this pattern. The tagged fixture is separate from `patient_visits.nric`, which already has a classic column mask; applying two different masks to the same column can cause a conflict.
* [Data Classification](https://docs.databricks.com/aws/en/data-governance/unity-catalog/data-classification) · [Classification tags](https://docs.databricks.com/aws/en/data-governance/unity-catalog/data-classification-tags) · [Designated Services](https://docs.databricks.com/aws/en/resources/designated-services)

## M-04 · Service policies (guardrails) on the lab model service
* **Prerequisite for the UI example:** the **Unity Gateway Service Policies** preview (workspace Previews page). Record if the preview isn't offered or is off.
* **Where:** Unity Gateway > Models > `<prefix>_chat` (default `lab_chat`) > **Policies** > New policy > Service policy.
* **Do (sandbox example):**
  1. **Built-in Sensitive Data Detection**, input and output, action **Deny**, classes *Email address* and *Phone number*. Applies to all account users.
  2. **Custom > LLM-as-a-judge** (evaluator: a `system.ai` model service), with a prompt that flags Singapore NRIC/FIN numbers (`[STFGM]` + 7 digits + letter) and ignores masked values. Rank 2.
* **Check:** set the bundle variable `expected_policy` to the judge policy's name and rerun the persona jobs. G-02 counts the synthetic prompt as blocked by the named policy only if a `databricks_service_policy` decision names it with enforced `action: deny` and the response contains no successful completion. A block without an attributable enforced decision, including one from a different policy or without `expected_policy`, is INFO with the decision records. Without an attached policy, G-02 can defer to this manual check. In this example, the built-in policy blocks a synthetic email prompt and a clean prompt is allowed.
* **Options:** Log records a policy decision without blocking the request; Enforce applies it. The [configuration notes](configuration-notes.md#custom-guardrails-and-serverless-egress) describe SQL, Python and LLM-as-a-judge trade-offs, including restricted egress. Prompt guardrails act on Unity Gateway traffic; M-03 exercises at-rest masking, including for data read through Genie.
* [Service policies](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/) · [Python policies](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/python-policies)

## M-05 · Rate limits
* **Where:** Unity Gateway > *your model service* > Rate limits.
* **Do:** record the configured limit and scope. The sandbox's `<prefix>_chat` sets 2 requests per minute for the clinicians group in `resources/demo/lab_chat.model_service.yml`.
* **Check:** G-03 makes three calls after a reset. With the sandbox grants and limits, the sequence is *allowed, allowed, rate_limited* for clinicians (HTTP 429), all allowed for PII readers and all denied for outsiders. A 429 for another role is recorded separately from the example group limit.
* [Rate limits](https://docs.databricks.com/aws/en/ai-gateway/rate-limits)

## M-06 · Account-level Genie One switch and partner-powered AI enforcement
* **Where:** Account console > Settings > Feature enablement.
* **Do:** record the Genie switch, the partner-powered AI value and whether the account enforces it. When partner-powered AI is off, Genie Agents Chat and Agent modes, Genie One chat and Genie Code agentic mode are unavailable. Genie One can still open data assets without chat.
* **Check:** compare the account value and enforcement in `scripts/account_checks.py` with the workspace value in P-05. The Genie switch itself is checked in the account console.
* [Partner-powered AI](https://docs.databricks.com/aws/en/databricks-ai/partner-powered)

## M-07 · File upload and download/export
* **Where:** Workspace settings > Advanced / Genie.
* **Do:** record whether users can upload files into Genie Agents and Genie One (on by default) and whether results can be downloaded or exported.
* **Check:** as a signed-in test user in the example role, record whether the paperclip is visible in the lab Genie Agent and whether the result export action is available on the surface you're testing.
* [File upload](https://docs.databricks.com/aws/en/genie-agents/file-upload) · [Download settings](https://docs.databricks.com/aws/en/admin/workspace-settings/notebooks)

## M-08 · Monitor tab and Governance Hub
* **Where:** the lab Genie Agent > Monitor; Account console > Governance > AI / Cost.
* **Do:** after the persona runs, open the Monitor tab to record the visible questions, statuses and any ratings.
* **Check:** compare the questions with the fixture in `30_persona_probe.py`; record what AI usage or cost information is visible in Governance Hub.
* [Genie monitoring](https://docs.databricks.com/aws/en/genie-agents/monitor) · [Governance Hub](https://docs.databricks.com/aws/en/admin/governance-hub/ai)

## M-09 · Playground
* **Where:** AI/ML > Playground, signed in as test users with and without `EXECUTE` on the sandbox model service, directly or through an inherited grant.
* **Check:** record who can see and query `genie_governance_lab...lab_chat` (G-01 exercises the API route). Prompts go through any service policies attached to that model service.

## M-10 · Genie Code
* **Where:** a notebook on the lab schema, Genie Code panel.
* **Do:** ask it to "show 5 rows of patient_visits with NRIC".
* **Check:** record the returned values. In the sandbox, the non-PII role sees masked values because Genie Code runs as the signed-in user. Where agent mode is available, record the tool-approval mode (per action or auto-approve).
* [Genie Code](https://docs.databricks.com/aws/en/genie-code/)

## M-11 · Databricks Apps
* **Where:** Compute > Apps > an app > Authorization.
* **Check:** record which identity the app uses for data access. By default the app uses its own service principal and its permissions; on-behalf-of-user uses admin-approved scopes and applies Unity Catalog filters and masks for the signed-in user.
* [App authorization](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/auth)

## M-12 · Knowledge Assistant (Agent Bricks)
* **Where:** AI/ML > Agents > the Knowledge Assistant > Permissions.
* **Check:** record who has `CAN QUERY` and the endpoint state (R-01); compare access with your targets. Designated Services lists Knowledge Assistant as requiring cross-Geo processing in the Asia Geo. R-01 inventories without invocation by default. To include a synthetic question, tag a Knowledge Assistant over **synthetic** content `lab_synthetic=true` and set `ka_invoke=true`; R-01 then records only the HTTP status and correlation headers.
* [Designated Services](https://docs.databricks.com/aws/en/resources/designated-services)

## M-13 · Who can create model, provider and MCP services
* **Where:** Catalog Explorer > each catalog and schema > Permissions; Unity Gateway > Models / MCPs > **+**.
* **Do:** record which principals hold `CREATE SERVICE` and `CREATE CONNECTION` and at what scope. A dedicated schema and builders group scope creation more narrowly than a catalog grant to `account users`. Also record whether `EXECUTE` on `system.ai` is broad (the default) or scoped by group; a metastore admin can take `MANAGE` on `system.ai` to edit the grants.
* **Check:** B-06 inventories visible grants, including principals outside `approved_builders`; claiming that no other grants exist requires metastore-admin visibility. B-07 records `system.ai` grants. B-01 … B-05 exercise the sandbox roles; B-04b shows registration with both `CREATE SERVICE` and `USE CONNECTION`. For the UI check, use a test user to observe whether **+ Model** / **+ MCP** can create in the selected schema. B-05 tests a classic serving route governed by a workspace setting rather than these grants (M-14).
* [Model services: privileges](https://docs.databricks.com/aws/en/ai-gateway/model-services) · [Model provider services](https://docs.databricks.com/aws/en/ai-gateway/model-provider-services) · [Register an MCP](https://docs.databricks.com/aws/en/ai-gateway/register-mcp-service) · [Govern an MCP](https://docs.databricks.com/aws/en/ai-gateway/govern-mcp-service)

## M-14 · Enforce Unity Gateway
* **Where:** workspace **Settings > Advanced > Enforce Unity Gateway** (workspace admin), not the Previews page. The setting is offered where legacy AI Gateway endpoints exist. B-08 uses the undocumented settings metadata key `enforce_ai_gateway_v3_only` on a best-effort basis. If the key changes or can't be read, B-08 is MANUAL and the UI is the source.
* **Inventory first:** run audit mode and review B-09 before changing the setting. Record which endpoints are external-model, provisioned-throughput or other AI endpoints. An incomplete classification or a non-admin inventory can't establish the absence of a route; a workspace admin can review any unclassified endpoints in the serving endpoint list. Record whether enforcement is on, off or not offered with B-08.
* **Review usage separately:** the migration guide calls for account-admin access to `system.serving.endpoint_usage` and `system.serving.served_entities`. Usage records typically arrive within about an hour, sometimes longer for new workspaces. [Usage tracking](https://docs.databricks.com/aws/en/ai-gateway/configure-ai-gateway-endpoints) limits coverage; no rows is not evidence that an untracked endpoint is unused.
* **Migration option:** for a workspace moving to enforcement, use the inventory and usage history to identify dependencies. Move each workload to a model service or external model provider as applicable, validate that requests reach the new route, then check that legacy request counts have dropped to zero and that the last legacy request pre-dates the migration. Enabling enforcement is the final step in that rollout. The account team can confirm availability if the setting is not offered after migration.
* **Check:** with enforcement on, B-05 looks for `Creating external model endpoint '<name>' is disabled for this workspace. Please use Unity Gateway.` with the probe's endpoint name. A query to a disabled `databricks-*` pay-per-token endpoint receives `PERMISSION_DENIED`. With enforcement off, the classic creation probe can succeed. Existing external-model and provisioned-throughput endpoints stay queryable with either setting. Compare these observations with your targets.
* **Audit evidence:** to review or alert on serving endpoint creation, filter `system.access.audit` on `service_name = 'serverlessRealTimeInference' AND action_name = 'createServingEndpoint'`. This evidence is separate from B-09's historical usage.
* **Trade-offs:** the [configuration notes](configuration-notes.md#workloads-that-still-use-legacy-endpoints), [migration guide](https://docs.databricks.com/aws/en/ai-gateway/migrate-to-unity-gateway) and [Apps migration note](https://kb.databricks.com/unity-catalog/migration-guide-moving-to-unity-ai-gateway) describe compatibility effects when comparing legacy and model-service routes.
* [Migrate to Unity Gateway](https://docs.databricks.com/aws/en/ai-gateway/migrate-to-unity-gateway) · [System tables (`system.serving.*`, Public Preview)](https://docs.databricks.com/aws/en/admin/system-tables/) · [Serverless egress for Model Serving](https://docs.databricks.com/aws/en/security/network/serverless-network-security/network-policies)
