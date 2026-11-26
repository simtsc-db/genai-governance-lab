# GenAI governance lab: controls matrix

Every control in the companion deck, its release status (AWS, as of **24 Nov 2026**), and how this lab covers it.

* **Test ID** (e.g. `D-01`): automated, result in `test_results`.
* **M-xx**: a guided manual step in [manual-steps.md](manual-steps.md), usually a UI or account-console setting with a check.
* **Walk-through**: discussed in the [runbook](runbook.md); no automated test, usually because it needs a client app, another product or customer-specific setup.
* **Inventory**: a script lists the configuration (e.g. `scripts/account_checks.py`) without judging it.
* **Pattern**: the lab tests a related mechanism that shows the principle, not this exact control (the note says which).

Numbering follows the deck. Rows **31** and **78** are intentionally excluded: they have no separate control in this version of the matrix.

Statuses change often for Beta and Preview features. Re-check the linked doc page before relying on a status.


## Layer 1 Platform

| # | Control | Status | Who enables | Lab coverage | Doc |
|---|---|---|---|---|---|
| 1 | SCIM / Automatic Identity Management | GA | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/admin/users-groups/automatic-identity-management/) |
| 2 | Role-based access control (assume a role per session) | GA (19 Aug 2026) | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/security/auth/rbac/) |
| 3 | Scope modifiers for PATs and OAuth | GA (4 Nov 2026) | Customer | P-03 checks PAT on/off and maximum lifetime only; scope modifiers: walk-through | [docs](https://docs.databricks.com/aws/en/dev-tools/auth/scope-modifiers) |
| 4 | OAuth on-behalf-of for service principals | GA (12 Nov 2026) | Default on | Walk-through | [docs](https://docs.databricks.com/aws/en/dev-tools/auth/oauth-m2m#oauth-obo) |
| 5 | Context-based ingress control | GA (Jul/Oct 2026); account-level ingress policy Beta | Customer | M-02 | [docs](https://docs.databricks.com/aws/en/security/network/front-end/context-based-ingress) |
| 6 | IP access lists | GA | Customer | P-02 | [docs](https://docs.databricks.com/aws/en/security/network/front-end/ip-access-list) |
| 7 | Genie One mobile app | Pre-GA (docs imply not yet GA) ⚠ | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/genie-one/mobile-admin) |
| 8 | Genie One desktop apps and Chrome extension | macOS GA, Windows Beta (11 Nov 2026), Chrome extension Beta | Customer (MDM) | Walk-through | [docs](https://docs.databricks.com/aws/en/genie-one/desktop) |
| 9 | Front-end Private Link (workspace) | GA | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/security/network/classic/privatelink) |
| 10 | Front-end Private Link for account-level Genie One | Beta | Customer (Previews) | Walk-through | [docs](https://docs.databricks.com/aws/en/security/network/front-end/front-end-private-connect-account) |
| 11 | Serverless egress control (network policies, NCC) | GA (Enterprise tier) | Customer | P-04 (MANUAL); `account_checks.py` lists network policies (inventory) | [docs](https://docs.databricks.com/aws/en/security/network/serverless-network-security/network-policies) |
| 12 | Private network gateway (serverless to resources in the customer's private network) | PrPr (announced Aug 2026) | 🤝 Ask your Databricks account team | Walk-through | [docs](https://docs.databricks.com/aws/en/release-notes/product/2026/august) |
| 13 | Compliance Security Profile (HIPAA, PCI-DSS, etc.) | GA (add-on) | Customer (commercial add-on) | P-08 | [docs](https://docs.databricks.com/aws/en/security/privacy/security-profile) |
| 14 | Customer-managed keys (CMK) | GA | Customer (Enterprise tier) | Walk-through | [docs](https://docs.databricks.com/aws/en/security/keys/customer-managed-keys) |
| 15 | Designated Services and cross-Geo processing | GA | Customer | P-06, P-07, D-05, R-01 (configuration) | [docs](https://docs.databricks.com/aws/en/resources/designated-services) |
| 16 | System-table retention (30–3,650 days) | GA (18 Nov 2026) | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/admin/system-tables/) |

## Layer 2 Data

| # | Control | Status | Who enables | Lab coverage | Doc |
|---|---|---|---|---|---|
| 17 | UC privileges (SELECT, EXECUTE, USE …) | GA | Customer | D-04 | [docs](https://docs.databricks.com/aws/en/data-governance/unity-catalog/manage-privileges/) |
| 18 | Row filters and column masks | GA | Customer | D-01, D-02 | [docs](https://docs.databricks.com/aws/en/data-governance/unity-catalog/filters-and-masks/) |
| 19 | Governed tags | GA (Apr / Sep 2026) | Customer | D-03, D-08 | [docs](https://docs.databricks.com/aws/en/admin/governed-tags/) |
| 20 | ABAC policies (tag-driven filters and masks) | PuPr (Nov 2025); ⚠ current docs no longer show a preview label and no GA announc | Customer | D-03, D-08 | [docs](https://docs.databricks.com/aws/en/data-governance/unity-catalog/abac/) |
| 21 | ABAC metastore-level policies, DENY policies | Beta (Sep 2026) | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/data-governance/unity-catalog/abac/metastore-policies) |
| 22 | ABAC on views / shared views | Beta | Workspace Previews ("ABAC on Views"), DBR 19+ | Walk-through | [docs](https://docs.databricks.com/aws/en/data-governance/unity-catalog/abac/requirements) |
| 23 | Governed tag propagation via lineage | Beta (Oct 2026) | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/admin/governed-tags/tag-propagation) |
| 24 | Data Classification (auto PII detection) | GA (Apr 2026), incl. automatic tagging; views Beta | Customer | D-05 (MANUAL), D-08, M-03. Asia Geo: needs cross-Geo processing; fallback is manual `class.sg_*` tags | [docs](https://docs.databricks.com/aws/en/data-governance/unity-catalog/data-classification) |
| 25 | Catalog–workspace bindings | GA | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/data-governance/unity-catalog/access-control/workspace-catalog-binding) |
| 26 | Secrets in UC / external secrets (AWS SM) | PuPr / Beta | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/security/secrets/unity-catalog-secrets) |
| 27 | Download and export settings | GA ⚠ (no label) | Customer | M-07 | [docs](https://docs.databricks.com/aws/en/admin/workspace-settings/notebooks) |
| 28 | Upload data UI | GA ⚠ (no label) | Customer | M-07 | [docs](https://docs.databricks.com/aws/en/admin/workspace-settings/disable-upload-data-ui) |
| 29 | Certification and deprecation (`system.certification_status` tag) | GA ⚠ | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/data-governance/unity-catalog/certify-deprecate-data) |

## Layer 3 Models and Unity Gateway

| # | Control | Status | Who enables | Lab coverage | Doc |
|---|---|---|---|---|---|
| 30 | Unity Gateway (model, provider, MCP services as UC securables) | GA (4 Aug 2026); API/TF/SDK GA Sep 2026; bundles Beta | Customer | G-01, G-03, G-04, B-01, B-02, B-06, B-07 | [docs](https://docs.databricks.com/aws/en/ai-gateway/) |
| 31 | Enforce Unity Gateway (workspace setting: disables `databricks-*` pay-per-token endpoints; no **new** external-model or provisioned-throughput serving endpoints outside Unity Gateway; existing ones stay queryable, inventory them via `system.serving.served_entities` or the serving endpoint list) | Not stated ⚠ (Unity Gateway is GA; the setting has no preview label and isn't on the Previews page) | Customer (workspace admin, Settings > Advanced; offered where legacy AI Gateway endpoints exist) | B-08, B-09 (audit; B-08 + B-09 PASS as admin = no existing external-model / PT route right now, usage history reviewed separately), B-05 (demo), M-14 | [docs](https://docs.databricks.com/aws/en/ai-gateway/migrate-to-unity-gateway) |
| 32 | ABAC GRANT policies for models and gateway services | GA (31 Aug 2026); agent services Beta | Customer | Pattern: G-01 tests a direct EXECUTE grant on the service, not a GRANT policy; GRANT policies: walk-through | [docs](https://docs.databricks.com/aws/en/ai-gateway/govern-access-to-models-with-grant-policies) |
| 33 | External model provider services (OpenAI, Anthropic, Bedrock…) | GA | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/ai-gateway/model-provider-services) |
| 34 | AI Functions UC permissions | GA (Oct 2026) | 🤝 Account team enables at account level; then the UC admin s | F-01 (INFO: records who can call), F-02 | [docs](https://docs.databricks.com/aws/en/large-language-models/ai-functions-uc-permissions) |
| 35 | Rate limits (service, per user, per group, per SP; QPM/TPM) | GA ⚠ | Customer | G-03, M-05 | [docs](https://docs.databricks.com/aws/en/ai-gateway/rate-limits) |
| 36 | Guardrails: safety, jailbreak, hallucination (service policies) | Beta | Workspace Previews ("Unity Gateway Service Policies") | G-02, M-04 | [docs](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/) |
| 37 | Guardrails: sensitive data detection (block or redact PII; log vs enforce) | Beta | Workspace Previews ("Unity Gateway Service Policies"). The A | G-02 (intended policy named in the response), M-04 | [docs](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/detect-sensitive-data) |
| 38 | Custom guardrails (UDF, LLM-as-judge), external partner guardrails, request-tag policies | Beta (Oct 2026) | Customer (Previews) | G-02 (LLM-as-a-judge), M-04 | [docs](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/python-policies) |
| 39 | Streaming-output and embedding-input policies | Beta (18 Nov 2026) | Customer (Previews) | M-04 | [docs](https://docs.databricks.com/aws/en/release-notes/product/2026/november) |
| 40 | Fallbacks and traffic split | GA | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/ai-gateway/configure-traffic-splitting) |
| 41 | Serving endpoint ACLs (CAN QUERY/MANAGE) | GA | Customer | S-01 | [docs](https://docs.databricks.com/aws/en/machine-learning/model-serving/manage-serving-endpoints) |
| 42 | Consumer-access users querying model services | GA (Oct 2026) | Customer | M-09 | [docs](https://docs.databricks.com/aws/en/ai-gateway/query-model-services) |
| 43 | Smart Routing (coding agents) | Beta | Account Previews (Enhanced Unity Gateway) | Walk-through | [docs](https://docs.databricks.com/aws/en/ai-gateway/coding-agent-model-selection) |

## Layer 4a Genie Agents

| # | Control | Status | Who enables | Lab coverage | Doc |
|---|---|---|---|---|---|
| 44 | Entitlements (Databricks SQL to author; Consumer access to use) | GA | Customer | setup (personas) | [docs](https://docs.databricks.com/aws/en/security/auth/entitlements) |
| 45 | Agent ACLs: CAN VIEW / RUN / EDIT / MANAGE; folder inheritance | GA | Customer | A-03 | [docs](https://docs.databricks.com/aws/en/genie-agents/set-up) |
| 46 | Per-user UC enforcement (incl. filters/masks); only attached sources used (≤50 tables) | GA | Default | A-01, A-02, A-04 | [docs](https://docs.databricks.com/aws/en/genie-agents/set-up) |
| 47 | Embedded warehouse credentials (compute only) | GA | Default | Pattern: A-01 shows per-user masks despite the embedded warehouse credential | [docs](https://docs.databricks.com/aws/en/genie-agents/set-up) |
| 48 | File upload in Genie Agents and Genie One | No label ⚠ | Customer | M-07 | [docs](https://docs.databricks.com/aws/en/genie-agents/file-upload) |
| 49 | SQL is read-only; tools can write | — | — | Walk-through |  |
| 50 | Certification (Certified/Deprecated) and governed tags on Genie Agents | GA (tags GA Sep 2026) | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/genie-agents/set-up) |
| 51 | Monitoring page (queries, users, feedback), benchmarks | GA ⚠ | Customer | M-08 | [docs](https://docs.databricks.com/aws/en/genie-agents/monitor) |
| 52 | Genie Conversation API, import/export | GA (Apr 2026) | Customer | A-01–A-04 (used by the probes) | [docs](https://docs.databricks.com/aws/en/genie-agents/conversation-api) |
| 53 | Genie in Slack / Microsoft Teams | GA (9 Nov 2026) | Customer (+ Slack/M365 admin approval) | Walk-through | [docs](https://docs.databricks.com/aws/en/genie-one/genie-slack) |
| 54 | Approve Genie tool actions in Slack/Teams | PuPr (Nov 2026) | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/genie-agents/external-tools) |
| 55 | Genie with Microsoft Copilot Studio / Copilot Cowork | PuPr / Beta | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/integrations/msft-power-platform/copilot-studio) |
| 56 | Share Genie Agents externally via OpenSharing | Beta | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/opensharing/share-genie-agent) |
| 57 | Kill switch: partner-powered AI off disables Genie Agents chat and agent mode | GA | Customer (see the warning in the companion deck) | Inventory: P-05 records the partner-powered AI setting; the kill-switch behaviour isn't tested | [docs](https://docs.databricks.com/aws/en/databricks-ai/partner-powered) |

## Layer 4b Genie One and Genie Code

| # | Control | Status | Who enables | Lab coverage | Doc |
|---|---|---|---|---|---|
| 58 | Account-level Genie One and chat in Genie One | PuPr (per AI/BI release notes; docs page shows no label) | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/genie-one/) |
| 59 | Account-level Genie One kill switch: *Settings → Feature enablement → Genie* (also stops metadata going to US-based services) | Available | Customer | M-06 | [docs](https://docs.databricks.com/aws/en/genie-one/) |
| 60 | Workspace-level Genie One needs any entitlement; Consumer access is least privilege | GA | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/genie-one/) |
| 61 | Account-only users (no workspace assignment; needs AIM/SCIM) | Beta | Customer (account setting) | Walk-through | [docs](https://docs.databricks.com/aws/en/genie-one/account-only-user-access) |
| 62 | No separate permission model: UC privileges, filters/masks and catalog bindings enforced, SQL runs as user | GA | Default | Pattern: A-01 … A-04 probe a Genie **Agent** through the Conversation API, not Genie One | [docs](https://docs.databricks.com/aws/en/genie-one/) |
| 63 | MCP connectors use the user's own OAuth, authorized per tool call via Unity Gateway | GA (Genie One MCP server GA Sep 2026) | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/genie-one/external-sources) |
| 64 | User-aware workspace instructions (per group) | Not stated ⚠ | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/genie-one/) |
| 65 | Web search in Genie One | Beta | Customer (Previews) | Walk-through | [docs](https://docs.databricks.com/aws/en/release-notes/product/2026/september) |
| 66 | Genie Ontology (business context from UC semantics plus learned knowledge) | GA (9 Nov 2026). Users only see snippets derived from assets they can access. Sn | Default | Walk-through | [docs](https://docs.databricks.com/aws/en/genie/genie-ontology/) |
| 67 | Genie Search (index Google Drive, Jira, Confluence, Workday into Genie One) | GA (9 Nov 2026). Account admin connects each source; source permissions rechecke | Customer (account admin, opt-in) | Walk-through | [docs](https://docs.databricks.com/aws/en/genie/genie-ontology/search-index) |
| 68 | Genie tracing to the unified trace table (prompts, responses, tool results) | Beta | Workspace admin | Walk-through (X-01 covers MLflow experiment permissions, not Genie tracing) | [docs](https://docs.databricks.com/aws/en/ai-gateway/genie-traces) |
| 69 | CSP workspaces are excluded from account-level aggregation | GA | Default | Walk-through | [docs](https://docs.databricks.com/aws/en/genie-one/) |
| 70 | Runs under the user's UC permissions | GA | Default | M-10 | [docs](https://docs.databricks.com/aws/en/genie-code/) |
| 71 | Agentic mode needs partner-powered AI; when off, non-agentic Genie Code runs on a Databricks-hosted model in CSP workspaces: HIPAA (all regions), FedRAMP Moderate (US), PCI-DSS and HITRUST (US, ANZ only; not Singapore) | GA | Customer | Inventory: P-05 records the partner-powered AI setting; agentic / non-agentic Genie Code behaviour isn't tested | [docs](https://docs.databricks.com/aws/en/databricks-ai/partner-powered) |
| 72 | Tool-action approval: per-action prompt vs. auto-approve. AI classifier, "not a security boundary". Auto-approve is the default and always on in Genie Code job tasks | GA | Customer (policy: per-action approval for prod; no Genie Cod | M-10 | [docs](https://docs.databricks.com/aws/en/genie-code/features-capabilities) |
| 73 | Workspace instructions, workspace skills, admin-curated MCP servers | GA | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/genie-code/instructions) |
| 74 | Web search | Beta, off by default | Workspace Previews | Walk-through | [docs](https://docs.databricks.com/aws/en/genie-code/web-search) |
| 75 | Genie Code CLI (model access via Unity Gateway) | GA (18 Nov 2026); needs cross-Geo in Singapore | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/genie-code/genie-code-cli) |
| 76 | Genie Code Jobs tasks / scheduled tasks | GA (Sep/Oct 2026) | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/jobs/tasks/genie-code) |
| 77 | Audit: `system.access.assistant_events` (user-initiated interactions only since May 2026) | PuPr | Customer | Walk-through (O-01 reads `aibiGenie` events in `system.access.audit`, not `assistant_events`) | [docs](https://docs.databricks.com/aws/en/admin/system-tables/assistant) |

## Layer 5 Agents, MCP and Apps

| # | Control | Status | Who enables | Lab coverage | Doc |
|---|---|---|---|---|---|
| 79 | MCP servers governed in Unity Gateway (tool filtering, service policies) | GA as part of Unity Gateway (GRANT policies for MCP services GA Aug 2026); servi | Customer | Pattern: T-01/T-02 show UC grants on managed MCP servers; tool filtering and MCP service policies: walk-through | [docs](https://docs.databricks.com/aws/en/ai-gateway/govern-mcp-service) |
| 80 | Managed MCP: Genie One MCP (`system.ai.genie_one_mcp`) | GA (Sep 2026) | Customer | Walk-through (T-02 uses the DBSQL MCP, not the Genie One MCP) | [docs](https://docs.databricks.com/aws/en/agents/mcp-tools/genie-mcp) |
| 81 | Databricks-provided SaaS connectors (Slack, M365, Google, GitHub, Atlassian, Asana) | No label ⚠ (GA announced as coming) | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/agents/mcp-tools/built-in-mcp-services) |
| 82 | External agents reaching Genie (ChatGPT, Claude, Copilot) | Not stated | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/release-notes/product/2026/november) |
| 83 | Managed MCP: `system.ai.dbsql`, `web_search`, `sandbox` (no egress) | Beta | Account Previews | T-02 | [docs](https://docs.databricks.com/aws/en/agents/mcp-tools/built-in-mcp-services) |
| 84 | Managed MCP (ungoverned): Genie Agent, AI Search, UC functions | PuPr | Workspace Previews | T-01 | [docs](https://docs.databricks.com/aws/en/agents/mcp-tools/genie-agent) |
| 85 | External MCP via UC connections + managed OAuth flows | GA ⚠ | Customer; MCPs hosted on Databricks Apps can't be registered | B-03, B-04, B-04b, M-13 | [docs](https://docs.databricks.com/aws/en/connect/uc-connections) |
| 86 | Jailbreak / prompt-injection screening of MCP tool calls and results | Beta (Oct 2026) | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/) |
| 87 | Default OAuth apps for external agents (Claude, ChatGPT, Codex, Cursor) | GA (Oct 2026) | On by default; account admins can switch off "AI assistant O | Walk-through | [docs](https://docs.databricks.com/aws/en/agents/mcp-tools/authentication-networking) |
| 88 | Unity Gateway for coding agents (`ug` CLI, central config, MDM provisioning) | Not stated ⚠ / Skills Beta | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/ai-gateway/coding-agent-configure-govern) |
| 89 | Agent auth: app service principal (default) vs. on-behalf-of-user with scopes | GA ⚠ | Customer; workspace admin allowlists OBO scopes | M-11 | [docs](https://docs.databricks.com/aws/en/agents/custom-agents/author-agent) |
| 90 | Databricks Apps permissions (CAN USE/MANAGE), ingress (IP ACL, Private Link), egress (network policies) | GA | Customer | M-11 | [docs](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/permissions) |
| 91 | Apps telemetry (OTel to UC) | GA per Sep 2026 release note; ⚠ the Apps monitor page still labels it Beta | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/observability) |
| 92 | Agent Bricks CLI, managed memory and sessions | Beta | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/agents/custom-agents/agent-bricks-cli) |
| 93 | AI/Vector Search: UC privileges on indexes, endpoint ACLs, CMK | GA | Customer | V-01, V-02 | [docs](https://docs.databricks.com/aws/en/ai-search/ai-search) |

## Layer 6–7 Cost and audit

| # | Control | Status | Who enables | Lab coverage | Doc |
|---|---|---|---|---|---|
| 94 | Budgets (alerts, block usage, webhook); per-user thresholds and group overrides | GA (Jul 2026); webhook and external-model spend Beta | Customer | C-02 (MANUAL); `account_checks.py` lists budgets (inventory) | [docs](https://docs.databricks.com/aws/en/ai-gateway/budgets) |
| 95 | Budget roles (Budget Manager/Viewer), Spend Status API | Not stated ⚠ | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/admin/account-settings/budget-permissions) |
| 96 | Daily per-user budget thresholds | Released 12 Nov 2026 (no label ⚠) | Customer | C-02 (MANUAL, inventory) | [docs](https://docs.databricks.com/aws/en/admin/account-settings/budgets) |
| 97 | Serverless rate limits | Beta (11 Nov 2026) | Customer (Previews) | Walk-through | [docs](https://docs.databricks.com/aws/en/compute/serverless/manage-serverless-compute#set-rate-limits-on-serverless-compute) |
| 98 | Warehouse-level statement timeouts | GA (20 Nov 2026) | Customer | C-01 | [docs](https://docs.databricks.com/aws/en/compute/sql-warehouse/warehouse-behavior#statement-timeout) |
| 99 | Serverless usage policies (formerly budget policies) for tags | PuPr | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/admin/usage/budget-policies) |
| 100 | Unity Gateway usage: `system.ai_gateway.usage` | PuPr; `external_model_spend` column Beta | Customer | O-02 (correlated with the suite run) | [docs](https://docs.databricks.com/aws/en/ai-gateway/usage-tracking) |
| 101 | Inference tables | GA ⚠ | Customer | G-04 (lab_evidence; PENDING until rows arrive) | [docs](https://docs.databricks.com/aws/en/ai-gateway/inference-tables) |
| 102 | Unified trace table (all gateway traffic, OTel) | Beta (Aug 2026) | Customer | Walk-through (X-01 covers MLflow experiment permissions, not the unified trace table) | [docs](https://docs.databricks.com/aws/en/ai-gateway/unified-trace-table) |
| 103 | Unity Gateway Insights | Beta (17 Nov 2026); needs Enhanced Unity Gateway beta | Customer (account Previews) | Walk-through | [docs](https://docs.databricks.com/aws/en/ai-gateway/insights) |
| 104 | Audit logs `system.access.audit`: `aibiGenie`, `aiGatewaySkills`, etc. | PuPr (audit log system table page, updated 19 Nov 2026; earlier versions of this | Customer | O-01, O-03 (correlated with the suite run; PENDING until logs arrive) | [docs](https://docs.databricks.com/aws/en/admin/system-tables/audit-logs) |
| 105 | Lakewatch (security log ingestion, detection rules, alert triage) | Beta (9 Nov 2026; CLI 19 Nov) | Customer (workspace Previews) | Walk-through | [docs](https://docs.databricks.com/aws/en/lakewatch/) |
| 106 | `system.billing.usage` | GA | Customer | Walk-through | [docs](https://docs.databricks.com/aws/en/admin/system-tables/billing) |
| 107 | Unity Gateway usage dashboard; Governance Hub (AI usage and spend) | Dashboard v1.0; Governance Hub Beta | Account Previews | M-08 | [docs](https://docs.databricks.com/aws/en/ai-gateway/cost-observability) |
