# The standalone adapter must construct without resolving invocation-scoped
# Azure OpenAI credentials at import time.

import importlib


class TestServerBootsWithoutAzureKey:
    def test_server_imports_and_app_constructs_with_no_key(self, monkeypatch):
        monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)

        import src.api.server as server

        importlib.reload(server)

        assert server.app is not None
        assert server.agent is not None


class TestServerConstructsLlmWithKey:
    def test_azure_llm_is_resolved_at_invocation_time(self, monkeypatch):
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "dummy-test-key")
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
        monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "test-deployment")

        import src.api.server as server

        importlib.reload(server)

        assert server._secrets_provider.get("AZURE_OPENAI_API_KEY") == "dummy-test-key"
        assert server.agent._nodes["main"]._llm is None


class TestStandaloneTrustPromotion:
    def test_external_bearer_never_promotes_to_internal(self):
        import src.api.server as server
        from framework.schemas.trust_level import TrustLevel

        assert server._resolve_standalone_trust(
            TrustLevel.ANONYMOUS, "Bearer external", "external", "runner"
        ) is TrustLevel.VERIFIED_EXTERNAL

    def test_runner_bearer_promotes_to_internal(self):
        import src.api.server as server
        from framework.schemas.trust_level import TrustLevel

        assert server._resolve_standalone_trust(
            TrustLevel.ANONYMOUS, "Bearer runner", "external", "runner"
        ) is TrustLevel.INTERNAL

    def test_wrong_or_missing_bearer_is_rejected_when_auth_is_enabled(self):
        import pytest
        import src.api.server as server
        from fastapi import HTTPException
        from framework.schemas.trust_level import TrustLevel

        for authorization in ("", "Bearer wrong"):
            with pytest.raises(HTTPException) as exc:
                server._resolve_standalone_trust(TrustLevel.ANONYMOUS, authorization, "external", "runner")
            assert exc.value.status_code == 401


def test_invoke_request_rejects_caller_selected_workday_url():
    import pytest
    from pydantic import ValidationError
    from src.api.server import InvokeRequest

    with pytest.raises(ValidationError):
        InvokeRequest.model_validate(
            {
                "input": "Find workers",
                "workday_tenant_url": "https://attacker.example/collect",
            }
        )
