"""Invocation-scoped Azure OpenAI access shared by agent nodes."""

from __future__ import annotations

from typing import Any

from framework.schemas.invocation_context import InvocationContext
from src.services.progress_events import emit_progress


def resolve_azure_llm(
    state: dict[str, Any],
    injected: Any | None = None,
    *,
    max_tokens: int = 1200,
    timeout_s: float = 20.0,
    max_retry: int = 0,
) -> Any:
    if injected is not None:
        return injected
    from shared.services.llm.azure_openai_client import AzureOpenAIClient

    ctx = InvocationContext.from_state(state)
    return AzureOpenAIClient(
        {
            "api_key": ctx.secrets.require("AZURE_OPENAI_API_KEY"),
            "azure_endpoint": ctx.secrets.require("AZURE_OPENAI_ENDPOINT"),
            "azure_deployment": ctx.secrets.require("AZURE_OPENAI_DEPLOYMENT"),
            "temperature": 0.1,
            "max_tokens": max_tokens,
            "timeout": timeout_s,
            "max_retries": max_retry,
        }
    )


def complete_text(
    state: dict[str, Any],
    messages: list[dict[str, str]],
    injected: Any | None = None,
    *,
    max_tokens: int = 1200,
    timeout_s: float = 20.0,
    max_retry: int = 0,
) -> str:
    emit_progress(
        "Preparing Azure OpenAI request.",
        {"provider": "azure_openai", "stage": "llm_setup"},
    )
    client = resolve_azure_llm(
        state,
        injected,
        max_tokens=max_tokens,
        timeout_s=timeout_s,
        max_retry=max_retry,
    )
    emit_progress(
        "Contacting Azure OpenAI.",
        {"provider": "azure_openai", "stage": "llm_request"},
    )
    if hasattr(client, "complete"):
        response = client.complete(messages)
    elif hasattr(client, "generate"):
        system = next((item["content"] for item in messages if item.get("role") == "system"), "")
        user = "\n\n".join(item["content"] for item in messages if item.get("role") == "user")
        response = client.generate(system_prompt=system, user_prompt=user)
    else:
        raise TypeError("LLM client must provide complete() or generate()")
    content = response.get("content", "") if isinstance(response, dict) else response
    text = str(content).strip()
    if not text:
        raise RuntimeError("Azure OpenAI returned an empty response")
    emit_progress(
        "Azure OpenAI response received.",
        {"provider": "azure_openai", "stage": "llm_request"},
    )
    from shared.security import detect_credentials

    if detect_credentials(text):
        raise RuntimeError("Azure OpenAI response failed credential safety validation")
    return text
