"""MainNode — composite main node sequencing WorkdayAPIQuery -> WorkerDetailEnrich.

The main backbone slot orchestrates two steps in sequence:
  Step 1 (WorkdayAPIQuery): LLM NL->filter translation + GET /workers
  Step 2 (WorkerDetailEnrich): fan-out GET /workers/{id} with bounded depth
Short-circuits on state["error"] set by any step.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import urllib.error
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from shared.utils.audit_logger import emit_trace_event
from src.services.llm_runtime import complete_text
from src.services.progress_events import emit_progress

_MAX_REPORTING_DEPTH = 3
_CREDENTIAL_RE = re.compile(r"\b(bearer|api[_-]?key|access[_-]?token|client[_-]?secret)\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Workday client helpers
# ---------------------------------------------------------------------------


def _get_workday_client(state: dict[str, Any]) -> tuple[str, str]:
    """Extract tenant URL and OAuth2 Bearer token via InvocationContext. Never os.environ."""
    tenant_url = state.get("validated_tenant_url") or state.get("workday_tenant_url") or ""
    from framework.schemas.invocation_context import InvocationContext

    ctx = InvocationContext.from_state(state)
    return str(tenant_url), str(ctx.secrets.require("WORKDAY_TOKEN"))


def _stg_mock_mode() -> bool:
    """Return the non-secret switch used by the provisional Stage 5 probe."""
    return os.environ.get("STG_MOCK_MODE", "").lower() in {"1", "true", "yes"}


def _translate_nl_to_filters(
    query: str,
    state: dict[str, Any],
    injected_llm: Any = None,
    *,
    timeout_s: float = 20.0,
    max_retry: int = 0,
) -> dict[str, Any]:
    """Translate a query to parameters supported by Workday GET /workers."""
    content = complete_text(
        state,
        [
            {
                "role": "system",
                "content": (
                    "Translate the Workday worker query into one JSON object for GET /workers. "
                    "Allowed keys are search, limit, and offset. The search field is only a worker name prefix "
                    "of at least 3 characters. Use limit 20 and offset 0 unless explicitly requested. "
                    "Return JSON only and omit unsupported filters such as title, location, cost center, or manager."
                ),
            },
            {"role": "user", "content": query},
        ],
        injected_llm,
        max_tokens=300,
        timeout_s=timeout_s,
        max_retry=max_retry,
    )
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE)
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("LLM filter translation must return a JSON object")
    allowed_keys = {"search", "limit", "offset"}
    filters = {
        str(key): value
        for key, value in parsed.items()
        if key in allowed_keys and isinstance(value, (str, int, float, bool))
    }
    state["generation_mode"] = "azure_openai"
    state["provider_error_message"] = None
    return _normalise_workday_filters(filters)


def _normalise_workday_filters(filters: dict[str, Any]) -> dict[str, Any]:
    """Bound the documented GET /workers collection parameters."""
    output: dict[str, Any] = {"limit": 20, "offset": 0}
    search = filters.get("search")
    if isinstance(search, str) and len(search.strip()) >= 3:
        output["search"] = search.strip()[:100]
    limit = filters.get("limit")
    if isinstance(limit, (int, float)) and not isinstance(limit, bool):
        output["limit"] = max(1, min(int(limit), 100))
    offset = filters.get("offset")
    if isinstance(offset, (int, float)) and not isinstance(offset, bool):
        output["offset"] = max(0, int(offset))
    return output


_STG_MOCK_WORKERS: list[dict[str, Any]] = [
    {"id": "stg-worker-001", "name": "Jane Smith", "title": "Engineer", "cost_center": "ENG-001"},
]


def _call_workday_list_workers(
    tenant_url: str,
    bearer_token: str,
    filters: dict[str, Any],
) -> list[dict[str, Any]]:
    """Call GET /workers. Returns list of worker summaries. Raises HTTPError on 4xx/5xx."""
    if _stg_mock_mode():
        return list(_STG_MOCK_WORKERS)
    from src.services.service import WorkdayService

    svc = WorkdayService(tenant_url, bearer_token)
    return svc.list_workers(filters)


_STG_MOCK_WORKER_DETAIL: dict[str, Any] = {
    "id": "stg-worker-001",
    "name": "Jane Smith",
    "title": "Engineer",
    "cost_center": "ENG-001",
    "hire_date": "2020-01-15",
    "reporting_chain": ["John Doe"],
}


def _call_workday_get_worker_detail(
    tenant_url: str,
    bearer_token: str,
    worker_id: str,
) -> dict[str, Any]:
    """Call GET /workers/{id}. Returns enriched worker dict. Raises HTTPError on 4xx/5xx."""
    if _stg_mock_mode():
        return dict(_STG_MOCK_WORKER_DETAIL)
    from src.services.service import WorkdayService

    svc = WorkdayService(tenant_url, bearer_token)
    return svc.get_worker_detail(worker_id, max_reporting_depth=_MAX_REPORTING_DEPTH)


# ---------------------------------------------------------------------------
# Main composite node
# ---------------------------------------------------------------------------


class MainNode(FunctionNode):
    """Main slot — sequences WorkdayAPIQuery -> WorkerDetailEnrich steps (inlined)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, llm: Any = None) -> None:
        super().__init__()
        self._llm = llm
        self._node_config: dict[str, Any] = {}
        self._max_enrichment_workers = 8
        self._max_enrichment_concurrency = 4
        self._enrichment_timeout_s = 25.0

    def configure(self, node_config: dict[str, Any]) -> None:
        self._node_config = dict(node_config)
        self._max_enrichment_workers = max(1, min(int(node_config.get("max_enrichment_workers", 8)), 20))
        self._max_enrichment_concurrency = max(
            1,
            min(int(node_config.get("max_enrichment_concurrency", 4)), self._max_enrichment_workers),
        )
        self._enrichment_timeout_s = max(1.0, min(float(node_config.get("enrichment_timeout_s", 25)), 60.0))

    def _extra_security_gate_output(self, result: dict[str, Any]) -> dict[str, Any]:
        """S-3: scan workers_list for credential patterns before storing in state."""
        workers_list = result.get("workers_list") or []
        for worker in workers_list:
            worker_str = str(worker)
            if _CREDENTIAL_RE.search(worker_str):
                from framework.errors import SecurityViolationError

                raise SecurityViolationError("MainNode S-3: credential pattern detected in Workday API response")
        return result

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("input_error_message"):
            return {"status": AgentStatus.SUCCESS.value}

        if state.get("error"):
            emit_trace_event("MainNode_skipped", {"reason": "upstream_error"}, state)
            return {}

        working = dict(state)
        deltas: dict[str, Any] = {}

        # ---------- Step 1: WorkdayAPIQuery ----------
        validated_query = working.get("validated_query") or working.get("natural_language_query") or ""
        validated_tenant_url = working.get("validated_tenant_url") or working.get("workday_tenant_url") or ""

        try:
            if _stg_mock_mode():
                tenant_url, bearer_token = str(validated_tenant_url), "stg-mock-bearer-token"
            else:
                tenant_url, bearer_token = _get_workday_client(working)
        except Exception as exc:
            # Secret resolution happens before any LLM or Workday request. It
            # must become a normal terminal response; allowing it to escape
            # makes the graph skip PostProcessNode and return output=None.
            emit_trace_event(
                "WorkdayAPIQuery_configuration_error",
                {"error_type": type(exc).__name__},
                working,
            )
            emit_progress(
                "Workday authentication is not configured. Preparing guidance.",
                {"stage": "workday_setup", "outcome": "configuration_error"},
            )
            return {
                "input_error_message": "Workday authentication setup is required.",
                "input_error_guidance": [
                    "Configure the required Workday OAuth credential in Marketplace Agent Settings.",
                    "Start a new execution after the deployment has restarted with the updated setting.",
                ],
                "input_setup_response": True,
                "status": AgentStatus.SUCCESS.value,
            }
        # validated_tenant_url is already read by _get_workday_client from state
        if validated_tenant_url:
            tenant_url = validated_tenant_url

        # LLM: translate NL query -> Workday filter params. A provider failure
        # is terminal: never issue a broader Workday query with guessed filters.
        try:
            filters = _translate_nl_to_filters(
                validated_query,
                working,
                self._llm,
                timeout_s=float(self._node_config.get("timeout_s", 20.0)),
                max_retry=int(self._node_config.get("max_retry", 0)),
            )
        except Exception as exc:
            emit_trace_event("WorkdayAPIQuery_llm_error", {"error_type": type(exc).__name__}, working)
            emit_progress(
                "Azure OpenAI could not process the query. Preparing guidance.",
                {"stage": "llm_request", "outcome": "provider_error"},
            )
            return {
                "input_error_message": "Azure OpenAI could not translate the Workday worker query.",
                "input_error_guidance": [
                    "Verify the Azure OpenAI endpoint, deployment, and access settings in Marketplace.",
                    "Retry after the deployment has restarted, or retry later if the provider is unavailable.",
                ],
                "generation_mode": "azure_openai_error",
                "provider_error_message": "Azure OpenAI filter translation failed or timed out.",
                "status": AgentStatus.SUCCESS.value,
            }

        deltas.update(
            {
                "generation_mode": working.get("generation_mode"),
                "provider_error_message": working.get("provider_error_message"),
                "workday_api_filters": filters,
            }
        )
        emit_trace_event(
            "WorkdayAPIQuery_filters_translated",
            {"filter_params": list(filters.keys()), "query_length": len(validated_query)},
            working,
        )
        # Call GET /workers
        emit_progress(
            "Querying the Workday worker directory.",
            {"stage": "workday_list", "filter_count": len(filters)},
        )
        try:
            workers_list = _call_workday_list_workers(tenant_url, bearer_token, filters)
            emit_trace_event(
                "WorkdayAPIQuery_workers_fetched",
                {"result_count": len(workers_list), "filters": list(filters.keys())},
                working,
            )
            emit_progress(
                f"Workday returned {len(workers_list)} worker record(s).",
                {"stage": "workday_list", "result_count": len(workers_list)},
            )
        except urllib.error.HTTPError as exc:
            emit_trace_event("WorkdayAPIQuery_fetch_error", {"http_code": exc.code}, working)
            return _workday_terminal_response(exc.code)
        except Exception as exc:
            emit_trace_event("WorkdayAPIQuery_fetch_error", {"error_type": type(exc).__name__}, working)
            emit_progress(
                "Workday could not be reached. Preparing a readable error response.",
                {"stage": "workday_list", "outcome": "network_error"},
            )
            return {
                "input_error_message": "The Workday worker directory could not be reached within the allowed time.",
                "input_error_guidance": [
                    "Verify the configured Workday tenant URL and Marketplace network access.",
                    "Retry after confirming that the Workday service is available.",
                ],
                "status": AgentStatus.SUCCESS.value,
            }

        step = {
            "workers_list": workers_list,
            "result_count": len(workers_list),
            "status": AgentStatus.SUCCESS.value,
        }
        working.update(step)
        deltas.update(step)

        workers_list = working.get("workers_list") or []

        # 0-result: not an error — return early with empty enriched list
        if not workers_list:
            emit_trace_event(
                "WorkerDetailEnrich_enrichment_complete",
                {"enriched_count": 0, "failed_count": 0, "reason": "empty_worker_list"},
                working,
            )
            step = {"workers_enriched": [], "result_count": 0, "status": AgentStatus.SUCCESS.value}
            working.update(step)
            deltas.update(step)
            emit_trace_event(
                "MainNode_pipeline_complete",
                {"result_count": 0, "enriched_count": 0, "error": None},
                state,
            )
            return deltas

        # ---------- Step 2: WorkerDetailEnrich ----------
        selected_workers = workers_list[: self._max_enrichment_workers]
        omitted_count = max(0, len(workers_list) - len(selected_workers))
        worker_ids = [str(worker.get("id") or worker.get("workerId") or "") for worker in selected_workers]
        failed_count = sum(1 for worker_id in worker_ids if not worker_id)
        indexed_ids = [(index, worker_id) for index, worker_id in enumerate(worker_ids) if worker_id]
        details_by_index: dict[int, dict[str, Any]] = {}
        timed_out_count = 0

        emit_progress(
            f"Enriching up to {len(indexed_ids)} Workday worker record(s).",
            {
                "stage": "workday_enrichment",
                "worker_count": len(indexed_ids),
                "concurrency": self._max_enrichment_concurrency,
            },
        )
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=self._max_enrichment_concurrency)
        futures = {
            executor.submit(_call_workday_get_worker_detail, tenant_url, bearer_token, worker_id): index
            for index, worker_id in indexed_ids
        }
        try:
            done, not_done = concurrent.futures.wait(futures, timeout=self._enrichment_timeout_s)
            for future in done:
                try:
                    details_by_index[futures[future]] = future.result()
                except Exception as exc:
                    failed_count += 1
                    emit_trace_event(
                        "WorkerDetailEnrich_worker_failed",
                        {"error_type": type(exc).__name__},
                        working,
                    )
            timed_out_count = len(not_done)
            failed_count += timed_out_count
            for future in not_done:
                future.cancel()
        finally:
            # Do not wait indefinitely for a provider call after the graph-level
            # enrichment deadline has elapsed. Individual requests also have a
            # short transport timeout in WorkdayService.
            executor.shutdown(wait=False, cancel_futures=True)

        workers_enriched = [details_by_index[index] for index in sorted(details_by_index)]
        warning_parts: list[str] = []
        if failed_count:
            warning_parts.append(f"{failed_count} worker detail request(s) could not be completed")
        if timed_out_count:
            warning_parts.append("the enrichment deadline was reached")
        if omitted_count:
            warning_parts.append(f"{omitted_count} additional record(s) were not enriched to keep execution bounded")
        workday_warning_message = "; ".join(warning_parts)

        if indexed_ids and not workers_enriched:
            emit_progress(
                "Workday worker details could not be retrieved. Preparing guidance.",
                {"stage": "workday_enrichment", "outcome": "no_details"},
            )
            return {
                "input_error_message": "Workday returned workers, but their details could not be retrieved in time.",
                "input_error_guidance": [
                    "Verify Workday access permissions for individual worker profiles.",
                    "Retry after confirming Workday availability.",
                ],
                "status": AgentStatus.SUCCESS.value,
            }

        emit_trace_event(
            "WorkerDetailEnrich_enrichment_complete",
            {"enriched_count": len(workers_enriched), "failed_count": failed_count},
            working,
        )
        step = {
            "workers_enriched": workers_enriched,
            "result_count": len(workers_enriched),
            "workday_warning_message": workday_warning_message or None,
            "status": AgentStatus.SUCCESS.value,
        }
        working.update(step)
        deltas.update(step)

        emit_trace_event(
            "MainNode_pipeline_complete",
            {"result_count": len(workers_enriched), "enriched_count": len(workers_enriched), "error": None},
            state,
        )
        emit_progress(
            f"Workday processing completed with {len(workers_enriched)} enriched record(s).",
            {
                "stage": "workday_enrichment",
                "enriched_count": len(workers_enriched),
                "failed_count": failed_count,
            },
        )
        return deltas


def _workday_terminal_response(code: int) -> dict[str, Any]:
    """Return a Marketplace-displayable and credential-safe Workday failure."""
    if code == 401:
        message = "Workday authentication was rejected."
        guidance = ["Refresh the configured Workday credential and start a new execution."]
    elif code == 403:
        message = "Workday denied access to the worker directory."
        guidance = ["Verify that the configured Workday account can read worker profiles."]
    elif code == 429:
        message = "Workday is temporarily rate limiting requests."
        guidance = ["Wait briefly and retry the query."]
    elif code >= 500:
        message = "Workday is temporarily unavailable."
        guidance = ["Retry after the Workday service has recovered."]
    else:
        message = "Workday rejected the worker query."
        guidance = ["Verify the tenant configuration and refine the query before retrying."]
    emit_progress(
        f"{message} Preparing guidance.",
        {"stage": "workday_list", "outcome": "http_error", "http_code": code},
    )
    return {
        "input_error_message": message,
        "input_error_guidance": guidance,
        "status": AgentStatus.SUCCESS.value,
    }
