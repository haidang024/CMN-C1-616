"""PostProcessNode — assemble org_summary; S-3 output gate.

Post-process slot node for CMN-C1-616. Transforms workers_enriched into
a structured org_summary dict for downstream consumption.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from shared.utils.audit_logger import emit_trace_event

_CREDENTIAL_RE = re.compile(
    r"\b(token|bearer|api[_-]?key|access[_-]?token|client[_-]?secret|password)\b", re.IGNORECASE
)


class PostProcessNode(FunctionNode):
    """Post-process slot — assemble org_summary from workers_enriched; S-3 credential scan."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _extra_security_gate_output(self, result: dict[str, Any]) -> dict[str, Any]:
        """S-3: scan org_summary for leaked credential patterns beyond expected HR fields."""
        org_summary = result.get("org_summary") or {}
        summary_str = str(org_summary)
        if _CREDENTIAL_RE.search(summary_str):
            raise SecurityViolationError("ResponseFormatterNode S-3: credential pattern detected in org_summary output")
        formatted = result.get("formatted_output", "") or ""
        if _CREDENTIAL_RE.search(formatted):
            raise SecurityViolationError("ResponseFormatterNode S-3: credential pattern detected in formatted_output")
        return result

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("input_error_message"):
            message = str(state["input_error_message"])
            return {
                "org_summary": {"workers": [], "result_count": 0},
                "result_count": 0,
                "formatted_output": message,
                "result": message,
                "status": AgentStatus.SUCCESS.value,
            }

        error = state.get("error")
        workers_enriched = state.get("workers_enriched") or []
        result_count = state.get("result_count", len(workers_enriched))
        if error:
            emit_trace_event(
                "ResponseFormatter_execute_complete",
                {"result_count": 0, "error": error},
                state,
            )
            return {
                "org_summary": {"workers": [], "result_count": 0, "error": error},
                "result_count": 0,
                "formatted_output": f"Query failed: {error}",
                "status": AgentStatus.SUCCESS.value,
            }

        # Assemble org_summary — primitives only (no Pydantic, no objects)
        summary_workers: list[dict[str, Any]] = []
        for w in workers_enriched:
            if not isinstance(w, dict):
                continue
            summary_workers.append(
                {
                    "id": str(w.get("id") or ""),
                    "name": str(w.get("name") or ""),
                    "title": str(w.get("title") or ""),
                    "cost_center": str(w.get("cost_center") or ""),
                    "hire_date": str(w.get("hire_date") or ""),
                    "reporting_chain": [str(m) for m in (w.get("reporting_chain") or [])],
                }
            )

        query_summary = (
            f"{result_count} worker(s) found matching query" if result_count > 0 else "No workers found matching query"
        )

        org_summary: dict[str, Any] = {
            "workers": summary_workers,
            "result_count": result_count,
            "query_summary": query_summary,
        }

        if result_count > 0:
            names = [str(w.get("name", "")) for w in summary_workers[:3]]
            extra = f" (and {result_count - 3} more)" if result_count > 3 else ""
            formatted_output = f"Found {result_count} worker(s): {', '.join(n for n in names if n)}{extra}"
        else:
            formatted_output = "No workers found matching the query."

        generation_mode = str(state.get("generation_mode") or "deterministic_fallback")
        formatted_output += f"\n\nGeneration mode: {generation_mode}"
        provider_error_message = str(state.get("provider_error_message") or "").strip()
        if provider_error_message:
            formatted_output += f"\nWarning: {provider_error_message}"
        workday_warning_message = str(state.get("workday_warning_message") or "").strip()
        if workday_warning_message:
            formatted_output += f"\nWarning: {workday_warning_message}"

        emit_trace_event(
            "ResponseFormatter_execute_complete",
            {"result_count": result_count, "summary_keys": list(org_summary.keys())},
            state,
        )
        return {
            "org_summary": org_summary,
            "result_count": result_count,
            "formatted_output": formatted_output,
            "result": formatted_output,
            "generation_mode": generation_mode,
            "provider_error_message": provider_error_message or None,
            "workday_warning_message": workday_warning_message or None,
            "status": AgentStatus.SUCCESS.value,
        }


# Backward-compat alias — old domain name → canonical slot name
ResponseFormatterNode = PostProcessNode
