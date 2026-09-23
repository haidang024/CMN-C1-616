"""Shared test-only dependency injection for Workday credentials."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def inject_workday_credentials(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep unit tests explicit without a production fake-token fallback."""
    if request.node.name in {
        "test_tc04_invocation_context_via_configurable",
        "test_get_workday_client_fails_closed_without_invocation_context",
    }:
        return

    import src.nodes.main_node as main_node

    def resolve(state: dict[str, object]) -> tuple[str, str]:
        tenant_url = state.get("validated_tenant_url") or state.get("workday_tenant_url") or ""
        return str(tenant_url), "unit-test-token"

    def complete(_state, messages, injected=None, **_kwargs):
        if injected is not None:
            response = injected.complete(messages)
            return response.get("content", "") if isinstance(response, dict) else str(response)
        return '{"limit": 20, "offset": 0}'

    monkeypatch.setattr(main_node, "_get_workday_client", resolve)
    monkeypatch.setattr(main_node, "complete_text", complete)
