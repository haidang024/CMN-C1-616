"""PreProcessNode — validate and normalize NL query + Workday tenant URL.

S-2 input gate: empty query check, URL format validation, prompt-injection scan,
input length limit. Pre-process slot node for CMN-C1-616.
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from shared.utils.audit_logger import emit_trace_event
from src.services.service import validate_workday_base_url

_MAX_QUERY_LEN = 4096
_URL_CANDIDATE_RE = re.compile(r"https://[^\s<>\"']+")
_INJECTION_RE = re.compile(
    r"\b(ignore\s+(previous|all)\s+instructions?|reveal\s+(your\s+)?system\s+prompt|"
    r"output\s+your\s+initial\s+instructions?|disregard\s+(all\s+)?previous)\b",
    re.IGNORECASE,
)


class PreProcessNode(FunctionNode):
    """Pre-process slot — validate NL query and Workday tenant URL. S-2 domain input gate."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, default_tenant_url: str = "") -> None:
        super().__init__()
        self._default_tenant_url = default_tenant_url.strip()

    def _tenant_url(self, state: dict[str, Any]) -> str:
        """Resolve trusted configuration, then a Marketplace user setup message.

        Chat setup is allowed only on the Marketplace path (identified by its
        conversation_history context), only from user-role messages, and only
        after the strict Workday URL validator accepts the destination. The
        standalone HTTP adapter still cannot select the destination.
        """
        configured = str(state.get("workday_tenant_url", "") or self._default_tenant_url)
        return configured or self._marketplace_tenant_url(state)

    @staticmethod
    def _raw_message(state: dict[str, Any]) -> str:
        input_context = state.get("input_context") or {}
        if isinstance(input_context, dict):
            raw = input_context.get("raw")
            if isinstance(raw, str) and raw:
                return raw
            history = input_context.get("conversation_history")
            if isinstance(history, list):
                for entry in reversed(history):
                    if not isinstance(entry, dict) or entry.get("role") != "user":
                        continue
                    content = entry.get("content")
                    if isinstance(content, str) and content:
                        return content
        return str(state.get("natural_language_query", "") or state.get("user_input", "") or "")

    @staticmethod
    def _json_envelope(text: str) -> dict[str, Any] | None:
        if not text.lstrip().startswith("{"):
            return None
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    @classmethod
    def _raw_query(cls, state: dict[str, Any]) -> str:
        raw = cls._raw_message(state)
        envelope = cls._json_envelope(raw)
        if envelope is not None:
            query = envelope.get("input") or envelope.get("natural_language_query")
            if isinstance(query, str):
                return query
        return raw

    @classmethod
    def _url_candidate_in_text(cls, text: str) -> str:
        envelope = cls._json_envelope(text)
        if envelope is not None:
            tenant_url = envelope.get("workday_tenant_url")
            if isinstance(tenant_url, str) and tenant_url.strip():
                return tenant_url.strip()
        for match in _URL_CANDIDATE_RE.finditer(text):
            return match.group(0).rstrip(".,;:!?)]}")
        return ""

    def _marketplace_tenant_url_candidate(self, state: dict[str, Any]) -> str:
        context = state.get("input_context")
        if not isinstance(context, dict):
            return ""
        history = context.get("conversation_history")
        if not isinstance(history, list):
            return ""
        current_candidate = self._url_candidate_in_text(self._raw_message(state))
        if current_candidate:
            return current_candidate
        for entry in reversed(history):
            if not isinstance(entry, dict) or entry.get("role") != "user":
                continue
            content = entry.get("content")
            if isinstance(content, str):
                candidate = self._url_candidate_in_text(content)
                if candidate:
                    return candidate
        return ""

    def _marketplace_tenant_url(self, state: dict[str, Any]) -> str:
        candidate = self._marketplace_tenant_url_candidate(state)
        if not candidate:
            return ""
        try:
            return validate_workday_base_url(candidate)
        except ValueError:
            # Preserve the rejected candidate so the input gate can report an
            # invalid URL instead of silently downgrading it to "missing".
            return candidate

    def _query_without_marketplace_url(self, state: dict[str, Any]) -> str:
        query = self._raw_query(state)
        context = state.get("input_context")
        if not isinstance(context, dict) or not isinstance(context.get("conversation_history"), list):
            return query
        for match in list(_URL_CANDIDATE_RE.finditer(query)):
            candidate = match.group(0).rstrip(".,;:!?)]}")
            try:
                validate_workday_base_url(candidate)
            except ValueError:
                continue
            query = query.replace(match.group(0), " ")
        return " ".join(query.split())

    def _extra_security_gate_input(self, state: dict[str, Any]) -> dict[str, Any]:
        """S-2: domain-specific input checks (HR query PII / prompt-injection / length / URL)."""
        # §9-ZD: read from input_context["raw"] first (bypasses PII masking of user_input)
        raw_query = self._raw_query(state)
        query = self._query_without_marketplace_url(state)
        tenant_url = self._tenant_url(state)
        marketplace_candidate = self._marketplace_tenant_url_candidate(state)

        if not raw_query.strip():
            out = dict(state)
            out["input_error_message"] = "No Workday worker query was provided."
            out["input_error_guidance"] = [
                "Describe the workers or organisation data you want to find.",
            ]
            out["status"] = AgentStatus.SUCCESS.value
            return out

        if not query.strip() and tenant_url:
            out = dict(state)
            out["input_error_message"] = "Workday tenant URL accepted."
            out["input_setup_response"] = True
            out["input_error_guidance"] = ["Now send the worker query you want to run."]
            out["status"] = AgentStatus.SUCCESS.value
            return out

        if len(query) > _MAX_QUERY_LEN:
            raise SecurityViolationError(
                f"InputValidationNode S-2: query exceeds maximum length ({len(query)} > {_MAX_QUERY_LEN})"
            )

        if _INJECTION_RE.search(query):
            raise SecurityViolationError(
                "InputValidationNode S-2: prompt-injection pattern detected in natural_language_query"
            )

        # Validate any destination explicitly supplied in Marketplace input,
        # even when deployment configuration already provides a trusted
        # default. Previously the configured URL won first and an invalid chat
        # URL was ignored, allowing the request to enter LLM/Workday calls and
        # appear to think indefinitely instead of reporting the input error.
        if marketplace_candidate:
            try:
                validate_workday_base_url(marketplace_candidate)
            except ValueError:
                out = dict(state)
                out["input_error_message"] = (
                    "The Workday tenant URL is not a valid Workday HTTPS REST service base URL."
                )
                out["input_error_guidance"] = [
                    "Use https://<workday-host>/api/<service>/<version>/<tenant> with no query or fragment.",
                    "Example: https://tenant.myworkday.com/api/common/v1/my-tenant.",
                ]
                out["status"] = AgentStatus.SUCCESS.value
                return out

        if not tenant_url.strip():
            out = dict(state)
            out["input_error_message"] = "Workday connection setup is required."
            out["input_setup_response"] = True
            out["input_error_guidance"] = [
                "Paste the full Workday REST service base URL into this chat, or set WORKDAY_TENANT_URL.",
                "Example: https://tenant.myworkday.com/api/common/v1/my-tenant.",
            ]
            out["status"] = AgentStatus.SUCCESS.value
            return out

        try:
            validate_workday_base_url(tenant_url)
        except ValueError:
            out = dict(state)
            out["input_error_message"] = "The Workday tenant URL is not a valid Workday HTTPS REST service base URL."
            out["input_error_guidance"] = [
                "Use https://<workday-host>/api/<service>/<version>/<tenant> with no query or fragment.",
                "Example: https://tenant.myworkday.com/api/common/v1/my-tenant.",
            ]
            out["status"] = AgentStatus.SUCCESS.value
            return out

        return dict(state)

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("input_error_message"):
            return {
                "input_error_message": state["input_error_message"],
                "input_error_guidance": state.get("input_error_guidance", []),
                "input_setup_response": state.get("input_setup_response", False),
                "status": AgentStatus.SUCCESS.value,
            }

        if state.get("error"):
            emit_trace_event("InputValidationNode_skipped", {"reason": "error_pre_set"}, state)
            return {}

        query = self._query_without_marketplace_url(state)
        tenant_url = self._tenant_url(state)

        if not query.strip():
            emit_trace_event("InputValidationNode_validation_failed", {"reason": "empty_query"}, state)
            return {
                "error": "natural_language_query is empty or missing",
                "status": AgentStatus.ERROR.value,
            }

        try:
            validated_tenant_url = validate_workday_base_url(tenant_url)
        except ValueError:
            emit_trace_event("InputValidationNode_validation_failed", {"reason": "invalid_tenant_url"}, state)
            return {
                "error": f"Invalid workday_tenant_url format: '{tenant_url}'",
                "status": AgentStatus.ERROR.value,
            }

        validated_query = query.strip()
        emit_trace_event(
            "InputValidationNode_execute_complete",
            {"query_length": len(validated_query), "tenant_url_host": validated_tenant_url.split("/")[2]},
            state,
        )
        return {
            "natural_language_query": query,
            "workday_tenant_url": tenant_url,
            "validated_query": validated_query,
            "validated_tenant_url": validated_tenant_url,
            "status": AgentStatus.SUCCESS.value,
        }


# Backward-compat alias — old domain name → canonical slot name
InputValidationNode = PreProcessNode
