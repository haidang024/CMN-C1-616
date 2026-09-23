# Template Design Specification

## Position in AgentCore Architecture

- **Agent Class**: `src.graph.graph.Graph`
- **L1 Base**: AgentBaseGraph (L1-direct, Cat 1 composite)
- **Three-Layer Separation**:
  - State: flat TypedDict composition (no Pydantic — msgpack incompatible)
  - Node: L1 inheritance (Template Method: `execute(self, state: dict) -> dict` override only)
  - Graph: composition (`register_nodes()` for node substitution)

## Architecture Overview

### Node Configuration

| Node | Responsibility | Input State | Output State | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| initialize | Framework default — sets schema_version, session_id, trust_level | — | session_id, schema_version | InitializeNode (default) |
| pre_process | Validate NL query + Workday tenant URL; S-2 input gate | natural_language_query, workday_tenant_url | validated_query, validated_tenant_url, status | InputValidationNode (PreProcessNode slot) |
| main | LLM NL→params → GET /workers → fan-out GET /workers/{id} | validated_query, validated_tenant_url | workers_list, workers_enriched, status | MainNode (inlines WorkdayAPIQueryNode + WorkerDetailEnrichNode) |
| post_process | Assemble workers_enriched → org_summary; S-3 output gate | workers_enriched, result_count | org_summary, result_count, formatted_output, status | ResponseFormatterNode (PostProcessNode slot) |
| finalize | Framework default — builds response_metadata, total_time_ms | — | response_metadata | FinalizeNode (default) |

### Data Flow

```
START → initialize → pre_process(InputValidationNode)
      → main(MainNode: WorkdayAPIQuery step → WorkerDetailEnrich step)
      → post_process(ResponseFormatterNode) → finalize → END
```

Two-step main decision: WorkdayAPIQueryNode and WorkerDetailEnrichNode are inlined
into a single MainNode (Cat 1 composite pattern). Sub-steps are
private module-level functions; no separate class files.

### State Definition

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| natural_language_query | str | Raw NL query from caller | Yes |
| workday_tenant_url | str | Workday tenant base URL (from config, not a secret) | Yes |
| validated_query | NotRequired[str] | Normalized NL query after validation | No |
| validated_tenant_url | NotRequired[str] | Validated and normalized tenant URL | No |
| workday_api_filters | NotRequired[dict] | Parsed filter params from LLM translation | No |
| workers_list | NotRequired[list] | GET /workers response items (list of dicts) | No |
| workers_enriched | NotRequired[list] | GET /workers/{id} enriched records (list of dicts) | No |
| org_summary | NotRequired[dict] | Structured org summary output | No |
| result_count | NotRequired[int] | Number of workers found | No |
| formatted_output | NotRequired[str] | Human-readable summary string | No |
| error | NotRequired[str] | Error message; triggers short-circuit downstream | No |
| status | NotRequired[str] | AgentStatus enum value | No |

**State Constraints (mandatory):**
- Flat TypedDict only (primitives + JSON-serializable types)
- No JWT, API keys, credentials in State (checkpoint DB leakage)
- InvocationContext reconstructed via `InvocationContext.from_state(state)` at the
  Workday service boundary; the context object itself is never stored in State.
- No Pydantic models, dataclass, arbitrary Python objects (msgpack incompatible)
- workday_tenant_url is a config value (not secret) — safe to store in State

## Framework Utilization

### Shared Components Used
- [x] InvocationContext (`from_state()` reconstruction for correlation, trust, and secrets)
- [x] ConnectionPolicy (retry/timeout strategy)
- [x] SecurityViolationError
- [x] S-2: `_extra_security_gate_input()` — domain-specific input check hook
      (InputValidationNode: NL query empty check, tenant URL format validation,
      prompt-injection pattern scan, input length limit 4096 chars)
- [x] S-3: `_extra_security_gate_output()` — domain-specific output check hook
      (ResponseFormatterNode: scan org_summary for leaked credential patterns and
      unexpected PII beyond expected HR fields; WorkdayAPIQueryNode: scan Workday
      API response for credential patterns before storing in state)
- [x] S-4: `emit_trace_event()` — at least one domain-specific event inside each `execute()`
      (mandatory; do NOT emit `node_start` / `node_complete` / `node_error` —
      `BaseNode.__call__()` emits these automatically; duplicates corrupt audit trail)

> **S-2/S-3 gate behaviour by node type (ADR-017):**
> - `FunctionNode` subclass → framework `@final` gate always runs automatically;
>   extend via `_extra_security_gate_input()` / `_extra_security_gate_output()` only

### Composition Pattern

- **Pattern**: Standalone (Cat 1 L1-direct, no inner subgraph)
- **Composition target**: N/A — all sub-steps inlined in MainNode
- **Error propagation strategy**: propagate (error in state → short-circuit downstream nodes)

## External Integrations

### Workday Common REST API
- Base URL: `https://<host>/api/common/v1/<tenant>`
- `GET {tenant_url}/workers` — list workers using documented `search`, `limit`, and `offset` parameters
- `GET {tenant_url}/workers/{id}` — get the Workday worker profile
- Auth: OAuth2 Bearer token via `ctx.secrets.require("WORKDAY_TOKEN")`
- Traversal depth guard: max 3 hops for reporting-chain traversal

### Config / Secrets
- `workday_tenant_url` — deployment-configured Workday service base URL, stored
  in State as non-secret; external callers cannot override it
- `WORKDAY_TOKEN` — OAuth2 Bearer token via `ctx.secrets.require()` — NOT os.environ, NOT in State

### LLM Injection Boundary

The main node resolves `AzureOpenAIClient` at invocation time through
`InvocationContext`, keeping credentials invocation-scoped. Provider failures
degrade to bounded Workday collection defaults.

### Provisional STG Boundary

`STG_MOCK_MODE=true` bypasses Workday credentials and HTTP calls only for the local
Stage 5 deployment probe. Normal invocations reconstruct `InvocationContext` and
require `WORKDAY_TOKEN` through the bound secret provider.

## RBAC / Field-Visibility Decision

Open design question from proposal: Workday RBAC controls which fields are visible per
API caller. Decision: the template passes all fields returned by the Workday API through
to the org_summary. Field-visibility filtering is delegated to the Workday RBAC layer
(the OAuth2 token scope determines what GET /workers returns). The template does not
implement application-layer field masking — that is a Cat 2 concern.

## Error Handling

- 0-result: returns empty `workers_list = []`, `result_count = 0`, `org_summary = {"workers": [], "result_count": 0}`, status SUCCESS (not ERROR)
- Workday 4xx (400, 401, 403, 404, 422): raise domain exception; agent returns error state
- Workday 5xx: raise domain exception; agent returns error state
- Partial failures in fan-out: `GET /workers/{id}` returning 404 for some IDs → log and continue, do not fail entire batch
- Pagination: out of scope per proposal (v1.0 returns first page only)

## Security Notes

- HR data classification: workers_enriched contains PII (name, hire date, cost center) — S-3 output gate verifies no credential leak
- emit_trace_event() called in every node execute() — mandatory S-4
- No credentials in state at any point
- WORKDAY_TOKEN accessed via ctx.secrets.require() only

## Import Isolation Confirmation
- [x] Template does not import agenticstar-platform SDK (Level 0)
- [x] Import targets: framework/ and shared/ only (no agents/base/ required)

## EU AI Act Art.13 Design-Time Evidence

The proposal declares this bounded worker-directory retrieval agent **Not in scope**
for Annex III, so Art.13 evidence is not required. The design nevertheless documents
its intended purpose, supported Workday fields, three-hop traversal limit, OAuth-scope
field visibility, and absence of employment decision-making.

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | AgentBaseGraph | Cat 1 — single fixed pipeline, no autonomous loop |
| Two-step main | Separate node classes (WorkdayAPIQueryNode + WorkerDetailEnrichNode) | Inline in MainNode | Inline in MainNode | Cat 1 composite pattern — the same Cat 1 composite pattern; sub-steps are private functions in main_node.py |
| Composition pattern | Standalone (flat pipeline) | GraphNode (inner subgraph) | Standalone | Cat 1 does not require inner BaseGraph; all steps inlined |
| Workday HTTP client | urllib (stdlib) | requests/httpx | urllib (stdlib) | No extra dependency; urllib available in Python 3.11 std |
| Field-visibility / RBAC | App-layer masking | Delegate to Workday RBAC | Delegate to Workday RBAC | Token scope determines visible fields; app masking is Cat 2 |
