"""CMN-C1-616 — agent state (flat TypedDict, ADR-005).

Flat TypedDict extending AgentState. LangGraph checkpoints use msgpack serialization;
all fields must be primitives or JSON-serializable types. No credentials, no PII persisted
as keys. Shared fields (user_input, status, session_id, node_history, error_log, hitl_*,
etc.) are inherited from AgentState.
"""

from __future__ import annotations

from typing import Any

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """CMN-C1-616 Workday worker query agent state (Cat 1 composite)."""

    # -- pre_process: InputValidationNode ------------------------------------
    natural_language_query: str  # raw NL query from caller
    workday_tenant_url: str  # Deployment-configured Workday REST base URL (not secret)
    validated_query: str | None  # normalized NL query after validation
    validated_tenant_url: str | None  # validated and normalized tenant URL

    # -- main step 1: WorkdayAPIQuery ----------------------------------------
    workday_api_filters: dict[str, Any] | None  # parsed filter params from LLM translation
    workers_list: list[dict[str, Any]] | None  # GET /workers response items

    # -- main step 2: WorkerDetailEnrich ------------------------------------
    workers_enriched: list[dict[str, Any]] | None  # enriched worker records

    # -- post_process: ResponseFormatterNode --------------------------------
    org_summary: dict[str, Any] | None  # structured org summary output
    result_count: int | None  # number of workers found
    formatted_output: str | None  # human-readable summary string

    # -- error propagation (any node) ---------------------------------------
    # Never set error to "" — use None / absent for no-error state.
    error: str | None  # error message; triggers short-circuit downstream
    input_error_message: str | None
    input_error_guidance: list[str] | None
    input_setup_response: bool | None
    generation_mode: str | None
    provider_error_message: str | None
    workday_warning_message: str | None
