"""TC-05 / PB-1: every node's execute() emits at least one domain audit event (S-4)."""

from __future__ import annotations

import pytest
import src.nodes.pre_process_node as prm
import src.nodes.main_node as mnm
import src.nodes.post_process_node as ppm
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

_VALID_QUERY = "Find all engineers in the Tokyo cost center"
_VALID_TENANT = "https://wd3-impl-services1.workday.com/api/common/v1/acme"


def _base_state():
    return {
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "natural_language_query": _VALID_QUERY,
        "workday_tenant_url": _VALID_TENANT,
        "validated_query": _VALID_QUERY,
        "validated_tenant_url": _VALID_TENANT,
        "workers_enriched": [
            {"id": "W-001", "name": "Yamada Taro", "title": "Engineer",
             "cost_center": "Tokyo", "hire_date": "2021-04-01", "reporting_chain": []},
        ],
        "result_count": 1,
        "session_id": "tc05-test",
    }


@pytest.mark.parametrize("module,cls_name", [
    (prm, "PreProcessNode"),
    (mnm, "MainNode"),
    (ppm, "PostProcessNode"),
])
def test_node_emits_trace_event(module, cls_name, monkeypatch):
    """TC-05 / PB-1: every node invoked via node(state) must call emit_trace_event at least once."""
    calls = []
    monkeypatch.setattr(module, "emit_trace_event",
                        lambda ev, payload, state=None, _c=calls: _c.append(ev))

    if cls_name == "MainNode":
        monkeypatch.setattr(module, "_call_workday_list_workers", lambda *a, **kw: [])

    state = _base_state()
    getattr(module, cls_name)()(state)

    domain_calls = [ev for ev in calls if ev not in {"node_start", "node_complete", "node_error", "s1_denied"}]
    assert domain_calls, f"{cls_name} must call emit_trace_event at least once for a domain event (TC-05 / PB-1)"

    framework_events = {"node_start", "node_complete", "node_error"}
    duplicates = [ev for ev in domain_calls if ev in framework_events]
    assert not duplicates, (
        f"{cls_name}.execute() must NOT emit framework lifecycle events: {duplicates}"
    )


@pytest.mark.parametrize("module,cls_name", [
    (prm, "PreProcessNode"),
    (mnm, "MainNode"),
    (ppm, "PostProcessNode"),
])
def test_node_s1_rejects_insufficient_trust(module, cls_name):
    """C3/C13-TRUST-GATE: S-1 Trust Gate must reject callers with ANONYMOUS trust level.

    Invokes node(state) — the mandatory BaseNode.__call__ entry point — with
    caller_trust_level=ANONYMOUS and asserts the S-1 gate denies before execute().
    """
    state = _base_state()
    state["caller_trust_level"] = TrustLevel.ANONYMOUS.value

    result = getattr(module, cls_name)()(state)

    assert result.get("status") == AgentStatus.ERROR.value, (
        f"{cls_name}: S-1 Trust Gate must deny ANONYMOUS callers with status=ERROR"
    )
    error_log = result.get("error_log", [])
    assert any("S-1" in entry or "trust" in entry.lower() for entry in error_log), (
        f"{cls_name}: error_log must reference S-1 trust gate denial"
    )
