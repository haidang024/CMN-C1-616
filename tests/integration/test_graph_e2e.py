"""Integration: end-to-end graph pipeline (mocked Workday API)."""

from __future__ import annotations

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

_VALID_QUERY = "Find all engineers in the Tokyo cost center"
_VALID_TENANT = "https://wd3-impl-services1.workday.com/api/common/v1/acme"
_VERIFIED_TRUST = TrustLevel.VERIFIED_EXTERNAL.value


class TestGraphE2E:
    """E2E integration tests for CMN-C1-616 graph via node sequence."""

    def _run_pipeline(self, monkeypatch, workers, detail_fn=None):
        """Run the full 3-node pipeline with mocked Workday API."""
        import src.nodes.main_node as mod

        monkeypatch.setattr(mod, "_call_workday_list_workers", lambda *a, **kw: workers)
        if detail_fn:
            monkeypatch.setattr(mod, "_call_workday_get_worker_detail", detail_fn)
        else:
            monkeypatch.setattr(
                mod,
                "_call_workday_get_worker_detail",
                lambda t, tok, wid: {
                    "id": wid,
                    "name": f"Name-{wid}",
                    "title": "Eng",
                    "cost_center": "Tokyo",
                    "hire_date": "2021-01-01",
                    "reporting_chain": [],
                },
            )

        from src.nodes.pre_process_node import InputValidationNode
        from src.nodes.main_node import MainNode
        from src.nodes.post_process_node import ResponseFormatterNode

        state = {
            "caller_trust_level": _VERIFIED_TRUST,
            "natural_language_query": _VALID_QUERY,
            "workday_tenant_url": _VALID_TENANT,
        }
        state.update(InputValidationNode()(state) or {})
        state.update(MainNode()(state) or {})
        state.update(ResponseFormatterNode()(state) or {})
        return state

    def test_e2e_success_2_workers(self, monkeypatch):
        """Full pipeline with 2 workers -> org_summary has 2 records, SUCCESS."""
        workers = [
            {"id": "W-001", "name": "Yamada Taro"},
            {"id": "W-002", "name": "Suzuki Hanako"},
        ]
        state = self._run_pipeline(monkeypatch, workers)
        assert state["status"] == AgentStatus.SUCCESS.value
        assert state["result_count"] == 2
        assert state["org_summary"]["result_count"] == 2
        assert len(state["org_summary"]["workers"]) == 2

    def test_e2e_zero_result(self, monkeypatch):
        """Full pipeline with 0 workers -> org_summary has 0 records, SUCCESS (not ERROR)."""
        state = self._run_pipeline(monkeypatch, [])
        assert state["status"] == AgentStatus.SUCCESS.value
        assert state["result_count"] == 0
        assert state.get("error") is None

    def test_e2e_empty_query_does_not_crash(self, monkeypatch):
        """Full pipeline with empty query -> does not crash; produces a valid response."""
        import src.nodes.main_node as mod

        monkeypatch.setattr(mod, "_call_workday_list_workers", lambda *a, **kw: [])

        from src.nodes.pre_process_node import InputValidationNode
        from src.nodes.main_node import MainNode
        from src.nodes.post_process_node import ResponseFormatterNode

        state = {
            "caller_trust_level": _VERIFIED_TRUST,
            "natural_language_query": "",
            "workday_tenant_url": _VALID_TENANT,
        }
        state.update(InputValidationNode()(state) or {})
        state.update(MainNode()(state) or {})
        state.update(ResponseFormatterNode()(state) or {})
        # Pipeline must complete without crashing; error is surfaced or 0-result response returned
        assert state.get("status") in (AgentStatus.SUCCESS.value, AgentStatus.ERROR.value)

    def test_e2e_state_json_serializable(self, monkeypatch):
        """PB-2: full pipeline state is JSON-serializable."""
        import json

        workers = [{"id": "W-001", "name": "Yamada"}]
        state = self._run_pipeline(monkeypatch, workers)
        json.dumps({k: v for k, v in state.items() if not k.startswith("_")})

    def test_e2e_no_credential_in_final_state(self, monkeypatch):
        """TC-03: no credential appears in any state field after full pipeline."""
        import json

        workers = [{"id": "W-001", "name": "Yamada"}]
        state = self._run_pipeline(monkeypatch, workers)
        state_str = json.dumps({k: v for k, v in state.items() if not k.startswith("_")}, default=str)
        assert "mock-bearer-token" not in state_str

    def test_graph_register_nodes(self):
        """Graph registers all required slots: pre_process, main, post_process."""
        from src.graph.graph import Graph

        g = Graph(config={})
        g.compile()
        assert "pre_process" in g._nodes
        assert "main" in g._nodes
        assert "post_process" in g._nodes
        assert "initialize" in g._nodes
        assert "finalize" in g._nodes

    def test_graph_name_and_state_schema(self):
        """Graph.name and state_schema are correct."""
        from src.graph.graph import Graph
        from src.schemas.state import State

        g = Graph(config={})
        assert g.name == "CMN-C1-616"
        assert g.state_schema is State

    def test_graph_default_constructor_loads_runtime_config(self):
        from src.graph.graph import Graph

        graph = Graph()
        assert graph.config["max_reporting_depth"] == 3
        assert "workday_tenant_url" in graph.config
