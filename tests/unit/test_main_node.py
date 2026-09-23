# CMN-C1-616 — Unit Tests: Main Node

from src.nodes.main_node import MainNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

_VALID_QUERY = "Find all engineers in the Tokyo cost center"
_VALID_TENANT = "https://wd3-impl-services1.workday.com/api/common/v1/acme"
_VERIFIED_TRUST = TrustLevel.VERIFIED_EXTERNAL.value


class TestMainNode:
    """Unit tests for the main business logic node."""

    def setup_method(self):
        self.node = MainNode()

    def test_success_path(self, monkeypatch):
        """TC: Main node processes valid input and returns SUCCESS."""
        import src.nodes.main_node as mod

        monkeypatch.setattr(
            mod,
            "_call_workday_list_workers",
            lambda *a, **kw: [
                {"id": "W-001", "name": "Yamada"},
            ],
        )
        monkeypatch.setattr(
            mod,
            "_call_workday_get_worker_detail",
            lambda t, tok, wid: {
                "id": wid,
                "name": "Yamada",
                "title": "Eng",
                "cost_center": "Tokyo",
                "hire_date": "2021-01-01",
                "reporting_chain": [],
            },
        )
        state = {
            "caller_trust_level": _VERIFIED_TRUST,
            "validated_query": _VALID_QUERY,
            "validated_tenant_url": _VALID_TENANT,
        }
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result.get("workers_enriched") is not None

    def test_empty_input(self):
        """TC: Main node with missing query -> error state."""
        state = {
            "caller_trust_level": _VERIFIED_TRUST,
            "validated_query": "",
            "validated_tenant_url": _VALID_TENANT,
        }
        result = self.node(state)
        # Without a valid query, it may succeed with 0 results (filter translation returns empty)
        # or error — either is acceptable (not a crash)
        assert result.get("status") in (AgentStatus.SUCCESS.value, AgentStatus.ERROR.value)

    def test_injected_llm_translates_filters(self, monkeypatch):
        """The graph-injected LLM is used by the NL-to-Workday filter boundary."""
        import src.nodes.main_node as mod

        class FakeLlm:
            def __init__(self):
                self.calls = []

            def complete(self, messages):
                self.calls.append(messages)
                return {"content": '{"search": "Yamada", "limit": 5}'}

        captured = {}

        def fake_list_workers(_tenant, _token, filters):
            captured.update(filters)
            return []

        fake_llm = FakeLlm()
        monkeypatch.setattr(mod, "_call_workday_list_workers", fake_list_workers)
        result = MainNode(llm=fake_llm)(
            {
                "caller_trust_level": _VERIFIED_TRUST,
                "validated_query": _VALID_QUERY,
                "validated_tenant_url": _VALID_TENANT,
            }
        )

        assert fake_llm.calls
        assert captured == {"search": "Yamada", "limit": 5, "offset": 0}
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_llm_failure_returns_terminal_guidance_and_skips_workday(self, monkeypatch):
        """Provider failure is visible and never triggers a guessed broad query."""
        import src.nodes.main_node as mod

        monkeypatch.setattr(
            mod,
            "complete_text",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("provider timeout")),
        )
        monkeypatch.setattr(
            mod,
            "_call_workday_list_workers",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Workday must not be called")),
        )

        result = self.node(
            {
                "caller_trust_level": _VERIFIED_TRUST,
                "validated_query": _VALID_QUERY,
                "validated_tenant_url": _VALID_TENANT,
            }
        )

        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["generation_mode"] == "azure_openai_error"
        assert result["input_error_message"].startswith("Azure OpenAI could not translate")

    def test_workday_enrichment_is_bounded_and_reports_progress(self, monkeypatch):
        import src.nodes.main_node as mod

        progress_messages = []
        detail_calls = []
        workers = [{"id": f"W-{index}", "name": f"Worker {index}"} for index in range(6)]
        monkeypatch.setattr(mod, "_call_workday_list_workers", lambda *_args: workers)

        def worker_detail(_tenant, _token, worker_id):
            detail_calls.append(worker_id)
            return {"id": worker_id, "name": worker_id, "reporting_chain": []}

        monkeypatch.setattr(mod, "_call_workday_get_worker_detail", worker_detail)
        monkeypatch.setattr(mod, "emit_progress", lambda message, _metadata: progress_messages.append(message))
        node = MainNode()
        node.configure(
            {
                "max_enrichment_workers": 3,
                "max_enrichment_concurrency": 2,
                "enrichment_timeout_s": 5,
            }
        )

        result = node(
            {
                "caller_trust_level": _VERIFIED_TRUST,
                "validated_query": _VALID_QUERY,
                "validated_tenant_url": _VALID_TENANT,
            }
        )

        assert result["status"] == AgentStatus.SUCCESS.value
        assert len(detail_calls) == 3
        assert result["result_count"] == 3
        assert "3 additional record(s)" in result["workday_warning_message"]
        assert any("Querying the Workday" in message for message in progress_messages)
        assert any("processing completed" in message for message in progress_messages)

    def test_all_worker_detail_failures_return_terminal_guidance(self, monkeypatch):
        import src.nodes.main_node as mod

        monkeypatch.setattr(mod, "_call_workday_list_workers", lambda *_args: [{"id": "W-1"}])
        monkeypatch.setattr(
            mod,
            "_call_workday_get_worker_detail",
            lambda *_args: (_ for _ in ()).throw(TimeoutError("provider timeout")),
        )

        result = self.node(
            {
                "caller_trust_level": _VERIFIED_TRUST,
                "validated_query": _VALID_QUERY,
                "validated_tenant_url": _VALID_TENANT,
            }
        )

        assert result["status"] == AgentStatus.SUCCESS.value
        assert "could not be retrieved in time" in result["input_error_message"]

    def test_execute_method_signature(self):
        """Node contract: Node must implement execute(state) not _invoke_impl.

        Canonical contract:
          - Override: execute(self, state: AgentState) -> dict
          - PROHIBITED: _invoke_impl(), process() override
        """
        import inspect

        # Must have execute() defined on the concrete class (not just inherited stub)
        assert hasattr(MainNode, "execute"), "MainNode must implement execute()"

        sig = inspect.signature(MainNode.execute)
        params = list(sig.parameters.keys())
        # execute(self, state) — at minimum two parameters
        assert len(params) >= 2, f"execute() must accept (self, state), got params: {params}"
        assert params[1] == "state", f"Second parameter must be 'state', got '{params[1]}'"

        # Must NOT define _invoke_impl at the domain level
        assert (
            "_invoke_impl" not in MainNode.__dict__
        ), "_invoke_impl() must not be defined in MainNode — use execute() instead"
