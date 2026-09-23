"""Regression tests for invocation-scoped Azure OpenAI resolution."""

import sys
from types import SimpleNamespace

from framework.schemas.invocation_context import InvocationContext


def test_resolve_azure_llm_uses_invocation_secrets(monkeypatch):
    captured = {}
    values = {"AZURE_OPENAI_API_KEY": "test-key", "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com", "AZURE_OPENAI_DEPLOYMENT": "test-deployment"}
    class Secrets:
        def require(self, key): return values[key]
    class Client:
        def __init__(self, config): captured.update(config)
    monkeypatch.setattr(InvocationContext, "from_state", classmethod(lambda cls, state: type("Ctx", (), {"secrets": Secrets()})()))
    monkeypatch.setitem(
        sys.modules,
        "shared.services.llm.azure_openai_client",
        SimpleNamespace(AzureOpenAIClient=Client),
    )
    from src.services.llm_runtime import resolve_azure_llm
    resolve_azure_llm({}, timeout_s=12.5, max_retry=1)
    assert captured["api_key"] == "test-key"
    assert captured["azure_endpoint"] == values["AZURE_OPENAI_ENDPOINT"]
    assert captured["azure_deployment"] == "test-deployment"
    assert captured["timeout"] == 12.5
    assert captured["max_retries"] == 1
