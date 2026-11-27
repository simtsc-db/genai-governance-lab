# How configuration changes what the lab observes

The lab's synthetic healthcare fixture is one example, not a recommended configuration. The
observations below describe what the lab can see in a test workspace. Compare observations from
your workspace with your targets; the availability and trade-offs of each option depend on your setup.

## Compliance Security Profile (CSP)

CSP adds controls for selected compliance standards and changes some feature defaults, including
role-based access control (off by default with CSP when RBAC is enabled at account level).
In a test workspace with CSP on, P-08 records that state, D-07 skips `ai_mask` 2.0 and P-07
falls back to manual evidence (M-03). With CSP off, P-08 records that state and D-07 can try
2.0 if the preview and Geography prerequisites are met. CSP can't be removed once the
workspace has processed regulated data; otherwise, support can receive a removal request.
[Compliance Security Profile](https://docs.databricks.com/aws/en/security/privacy/security-profile) ·
[CSP settings](https://docs.databricks.com/aws/en/security/privacy/enhanced-security-compliance#disable) ·
[RBAC defaults](https://docs.databricks.com/aws/en/security/auth/rbac/get-started) ·
[Feature availability](https://docs.databricks.com/aws/en/resources/feature-region-support)

## Genie Agent mode

Agent mode enables multi-step analysis and can use attached tools, in contrast to Chat.
A-05 is a MANUAL UI test. In a test workspace where Agent mode is available, a signed-in test
user repeats the data access checks and records the results. Where Agent mode is off or
unavailable, the manual evidence records that state and the reason if shown. A-01 through A-04
use Chat and do not test Agent mode.
[Genie Agent mode](https://docs.databricks.com/aws/en/genie-agents/concepts#agent-mode) ·
[Partner-powered AI](https://docs.databricks.com/aws/en/databricks-ai/partner-powered)

## Preview availability

Account and workspace preview switches, region and eligibility determine whether a feature is
available. In a test workspace with the relevant prerequisite available, the lab can run the probe
or manual check. With it off or unavailable, the lab records the missing prerequisite rather than
a control failure: for example, D-07 or T-02 can report SKIP, and G-02 can defer to M-04.
[Manage previews](https://docs.databricks.com/aws/en/admin/workspace-settings/manage-previews)

## Singapore identifiers: prompts and data at rest

The built-in prompt guardrail has no Singapore NRIC/FIN class. In a test workspace with an
enforcing custom policy attached to the model service, G-02 looks for a denial from that named
policy; without it, G-02 doesn't establish that the NRIC prompt was blocked by the intended policy.
M-04 can also distinguish enforcement from logging. Service policies apply to gateway traffic,
not Genie's own model calls.

At rest, Data Classification can tag NRIC, FIN and work permit columns in Singapore-region
workspaces. In a test workspace in the Asia Geo with cross-Geo processing allowed, D-05/M-03 can
exercise automatic classification if enabled. With in-Geo enforcement on, that scan is unavailable;
manual `class.sg_*` tags let D-08 assess the same ABAC masking pattern. The lab labels its
hand-tagged fixture SIMULATED; D-08 records INFO if no matching tags are present.
[Sensitive data detection](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/detect-sensitive-data) ·
[Classification tags](https://docs.databricks.com/aws/en/data-governance/unity-catalog/data-classification-tags) ·
[Designated Services](https://docs.databricks.com/aws/en/resources/designated-services)

## Custom guardrails and serverless egress

Custom SQL policy functions compile to CEL, which rejects SQL expressions such as `RLIKE` and
`regexp_like`. Python policy functions can use regex (`nric_guard_py` is the lab example). In
an eligible test workspace without restricted serverless internet egress, the Python policy
can evaluate requests. With restricted egress, an attached Python policy cannot run: Enforce
mode denies each request it evaluates, while Log mode records the would-be denial and lets
the request proceed. An LLM-as-a-judge policy is another option that doesn't depend on a
Python policy. M-04 identifies the policy type and G-02 looks for a denial from the expected
policy, not just a failed request.
[Service policies](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/) ·
[Python policies](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/python-policies) ·
[Serverless network policies](https://docs.databricks.com/aws/en/security/network/serverless-network-security/network-policies)

## Schema-level versus service-level EXECUTE

Schema-level `EXECUTE` is inherited by model services in that schema. In a test workspace
with schema-level `EXECUTE`, a caller with the required `USE` privileges can query an inherited
service without a direct grant. Without that inherited grant, access can be assigned per service.
G-01 and M-09 exercise the latter pattern in the sandbox: `USE SCHEMA` at schema level and
`EXECUTE` on the service for selected example roles.
[Model service privileges](https://docs.databricks.com/aws/en/ai-gateway/model-services)

## AI Functions and `system.ai` defaults

By default, `account users` has `EXECUTE` on `system.ai` for Databricks-provided models and MCP
services. With AI Functions Unity Catalog permissions enabled at account level, broad schema
grants can allow calls across roles in a test workspace, while group-specific grants on
individual functions can produce allowed or denied calls by role. With the feature off, a
`system.ai` grant inventory alone does not establish who can call AI Functions. F-01 probes
actual calls; B-07 records schema grants. A metastore admin can take `MANAGE` on `system.ai`
to change grants (M-13). Access to a function does not grant access to data: F-02 tests the data
visible to the caller.
[AI Functions permissions](https://docs.databricks.com/aws/en/large-language-models/ai-functions-uc-permissions) ·
[Model service privileges](https://docs.databricks.com/aws/en/ai-gateway/model-services) ·
[MCP governance](https://docs.databricks.com/aws/en/ai-gateway/govern-mcp-service)

## `ai_query` and model-service support

The documented Unity Gateway SQL route supports Databricks-provided `system.ai` models.
In a test workspace where `ai_query` reports that custom model services are unsupported,
F-03 records INFO `not supported for this route` with the error. If the call succeeds, F-03
records INFO `allowed: ai_query reached the custom model service` and a response excerpt.
A permission denial is recorded separately from an unsupported route. G-01 through G-03
test the gateway API route separately. For batch `ai_query`, Unity Gateway usage tracking
applies, not rate limits, guardrails or inference tables.
[Query model services](https://docs.databricks.com/aws/en/ai-gateway/query-model-services)

## Classic serving and Enforce Unity Gateway

In a test workspace with *Enforce Unity Gateway* off, a user with *Workspace access* can create
a classic external-model serving endpoint without a Unity Catalog `CREATE SERVICE` grant. With
it on, B-05 looks for a refusal that names the probe endpoint and the enforcement; a generic
403 does not identify that control. New external-model and provisioned-throughput endpoints
outside Unity Gateway are blocked, and `databricks-*` pay-per-token endpoints are disabled.
The setting is offered where legacy AI Gateway endpoints exist. B-08 and M-14 record the
setting; the UI is the source when the best-effort API read is unavailable.

Existing external-model and provisioned-throughput endpoints stay queryable. B-09 inventories
both types and other AI endpoints. A complete workspace-admin inventory with neither legacy
type present, combined with enforcement on, is evidence that neither route is present or
creatable at the time of the check. The endpoint list is permission-filtered, and the optional
usage history only covers endpoints with usage tracking on. Restricted serverless egress
(M-02) can also limit outbound model-serving calls; `createServingEndpoint` audit events can
show new endpoint creation independently of the setting.
[Migration guide](https://docs.databricks.com/aws/en/ai-gateway/migrate-to-unity-gateway) ·
[Usage tracking on endpoints](https://docs.databricks.com/aws/en/ai-gateway/configure-ai-gateway-endpoints) ·
[Serverless network policies](https://docs.databricks.com/aws/en/security/network/serverless-network-security/network-policies)

## Workloads that still use legacy endpoints

With enforcement off, the lab can exercise configured legacy dependencies if they are available.
In a test workspace with enforcement on, a vector index with managed embeddings on a
`databricks-*` endpoint, an agent that calls a `databricks-*` chat endpoint and `ai_query` using a
`databricks-*` endpoint name can stop working. V-01, V-02, S-01 and F-02 record SKIP when the
lab recognizes those unavailable dependencies. The Unity Gateway routes in G-01 through G-04,
B-01 through B-04b, T-01 and T-02 do not depend on those legacy endpoints.

Unity Gateway does not yet support creating AI Search endpoints, so Knowledge Assistant and
Multi-Agent Supervisor workloads that depend on AI Search can be affected. R-01's default
inventory does not invoke the Knowledge Assistant or measure this effect. With enforcement on,
`ai_query` supports only `system.ai` services; custom agents on Model Serving cannot use
authentication passthrough to model services. Apps using a serving endpoint resource can lose
that route; a model-service route uses `EXECUTE` for the app's service principal.
[Migration guide](https://docs.databricks.com/aws/en/ai-gateway/migrate-to-unity-gateway) ·
[App migration details](https://kb.databricks.com/unity-catalog/migration-guide-moving-to-unity-ai-gateway)
