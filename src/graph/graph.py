"""CMN-C1-616 — graph (Cat 1 composite Workday worker query agent).

AgentBaseGraph 5-node backbone; the `main` slot holds `MainNode`
(a FunctionNode sequencing WorkdayAPIQuery -> WorkerDetailEnrich steps).

    START -> initialize -> pre_process(PreProcessNode) -> main(MainNode)
           -> post_process(PostProcessNode) -> finalize -> END
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, ClassVar, cast

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.utils.config_loader import load_config

from src.nodes.main_node import MainNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State


_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
_STG_MOCK_TENANT_URL = "https://stg-mock.workday.com/api/common/v1/acme"
logger = logging.getLogger(__name__)


class Graph(AgentBaseGraph):
    """Cat 1 composite graph for CMN-C1-616 Workday worker query."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        if config is None:
            config = load_config(str(_CONFIG_PATH)) if _CONFIG_PATH.exists() else {}
        super().__init__(config=config)
        tenant_url = os.environ.get("WORKDAY_TENANT_URL", "").strip()
        if not tenant_url:
            tenant_url = str(self.config.get("workday_tenant_url", "")).strip()
        if not tenant_url and os.environ.get("STG_MOCK_MODE", "").lower() in {"1", "true", "yes"}:
            tenant_url = _STG_MOCK_TENANT_URL
        self._default_tenant_url = tenant_url

    @property
    def name(self) -> str:
        return "CMN-C1-616"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode
        self._nodes["pre_process"] = PreProcessNode(default_tenant_url=self._default_tenant_url)
        self._nodes["main"] = MainNode()
        self._nodes["main"].configure(self.config)
        self._nodes["post_process"] = PostProcessNode()

    def get_output(self, state: dict[str, Any]) -> dict[str, Any]:
        output = cast(dict[str, Any], super().get_output(state))
        message = state.get("input_error_message")
        if message:
            if state.get("input_setup_response"):
                lines = [str(message)]
            else:
                lines = ["Workday worker query could not be processed.", "", f"Reason: {message}"]
            guidance = state.get("input_error_guidance")
            if isinstance(guidance, list) and guidance:
                lines.extend(["", "How to continue:"])
                lines.extend(f"- {item}" for item in guidance)
            output["output"] = "\n".join(lines)
            # Marketplace treats every non-success status as an infrastructure
            # failure and discards the agent's readable output. Input/provider
            # failures are complete terminal responses, not successful domain
            # queries, but must use the transport-success status to be visible.
            output["status"] = AgentStatus.SUCCESS.value
            return output

        if output.get("status") != AgentStatus.SUCCESS.value or output.get("output") is None:
            output["output"] = _safe_graph_failure_message(state)
            output["status"] = AgentStatus.SUCCESS.value
        return output

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Invoke with a final safe response boundary for unexpected failures."""
        try:
            return cast(
                dict[str, Any],
                super().invoke(user_input=user_input, session_id=session_id, ctx=ctx, input_context=input_context),
            )
        except Exception as exc:
            # Never expose exception text because provider errors can contain
            # URLs, request metadata, or credential-adjacent values.
            logger.error("CMN-C1-616 graph invocation failed: %s", type(exc).__name__)
            return {
                "output": (
                    "The Workday worker query stopped because of an unexpected execution problem.\n\n"
                    "How to continue:\n"
                    "- Retry the query once.\n"
                    "- If the problem continues, verify the Workday and Azure OpenAI configuration."
                ),
                "status": AgentStatus.SUCCESS.value,
                "trace_id": "",
                "correlation_id": ctx.correlation_id if ctx is not None else "",
                "node_history": [],
            }


def _safe_graph_failure_message(state: dict[str, Any]) -> str:
    """Map internal terminal states to a stable, non-sensitive user message."""
    node_history = state.get("node_history") or []
    completed_nodes = [str(node) for node in node_history if str(node) != "FinalizeNode"]
    failed_after = completed_nodes[-1] if completed_nodes else ""
    error_text = " ".join(str(item) for item in [state.get("error"), *(state.get("error_log") or [])] if item).lower()
    if "authentication" in error_text or "401" in error_text:
        reason = "Workday authentication was rejected."
        action = "Refresh the configured Workday credential and start a new execution."
    elif "access denied" in error_text or "403" in error_text:
        reason = "Workday denied access to the requested worker data."
        action = "Verify that the configured Workday account has permission to read worker profiles."
    elif "timeout" in error_text or "network" in error_text or "connection" in error_text:
        reason = "A required provider could not be reached within the allowed time."
        action = "Verify provider availability and Marketplace network access, then retry."
    elif (
        "validation" in error_text
        or "security" in error_text
        or "s-2" in error_text
        or "cannot be processed safely" in error_text
        or failed_after == "PreProcessNode"
        or (not error_text and "PreProcessNode" in completed_nodes)
    ):
        reason = "The request could not pass input or output validation."
        action = "Review the query for unsupported or sensitive content and retry."
    else:
        reason = "The graph could not complete the Workday worker query."
        action = "Retry once; if it continues, verify the Workday and Azure OpenAI configuration."
    return f"{reason}\n\nHow to continue:\n- {action}"
