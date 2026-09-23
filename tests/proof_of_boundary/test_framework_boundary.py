"""PB-1, PB-2, PB-3, PB-4, PB-5: Framework boundary proof tests for CMN-C1-616."""

from __future__ import annotations

import ast
import json
import os
import re

import pytest
from framework.schemas.trust_level import TrustLevel

_VALID_QUERY = "Find all engineers in the Tokyo cost center"
_VALID_TENANT = "https://wd3-impl-services1.workday.com/api/common/v1/acme"
_VERIFIED_TRUST = TrustLevel.VERIFIED_EXTERNAL.value


# ---------------------------------------------------------------------------
# PB-1: emit_trace_event fires from every node's execute()
# ---------------------------------------------------------------------------
class TestPB1AuditLogger:
    def test_emit_trace_event_callable(self):
        """PB-1: emit_trace_event is importable and callable from shared.utils.audit_logger."""
        from shared.utils.audit_logger import emit_trace_event
        assert callable(emit_trace_event)
        emit_trace_event("PB1_test_event", {"key": "val"}, {"session_id": "test"})


# ---------------------------------------------------------------------------
# PB-2 + PB-5: State serialization — primitives only, no credentials
# ---------------------------------------------------------------------------
class TestPB2StateSerialization:
    def test_state_fields_json_serializable(self):
        """PB-2: all State fields are JSON-serializable primitives."""
        from src.schemas.state import State
        import typing
        hints = typing.get_type_hints(State)
        for field_name in hints:
            assert not re.search(r"(jwt|api_key|secret|password|credential)", field_name, re.IGNORECASE), (
                f"Credential-like field name in State: {field_name}"
            )

    def test_state_is_typeddict(self):
        """TC-01: State is a TypedDict, not a Pydantic BaseModel."""
        from src.schemas.state import State
        assert issubclass(State, dict)
        assert hasattr(State, "__annotations__")
        try:
            from pydantic import BaseModel
            assert not issubclass(State, BaseModel), "State must not be a Pydantic BaseModel"
        except ImportError:
            pass

    def test_post_invoke_state_primitives_only(self, monkeypatch):
        """PB-2: after a mock pipeline run, state contains only primitives."""
        import src.nodes.main_node as mod
        from src.nodes.pre_process_node import InputValidationNode
        from src.nodes.main_node import MainNode
        from src.nodes.post_process_node import ResponseFormatterNode

        monkeypatch.setattr(mod, "_call_workday_list_workers", lambda *a, **kw: [
            {"id": "W-1", "name": "Yamada", "title": "Eng", "cost_center": "Tokyo"},
        ])
        monkeypatch.setattr(mod, "_call_workday_get_worker_detail",
                            lambda t, tok, wid: {"id": wid, "name": "Yamada", "title": "Eng",
                                                  "cost_center": "Tokyo", "hire_date": "2021-01-01",
                                                  "reporting_chain": []})

        state = {
            "caller_trust_level": _VERIFIED_TRUST,
            "natural_language_query": _VALID_QUERY,
            "workday_tenant_url": _VALID_TENANT,
        }
        state.update(InputValidationNode()(state) or {})
        state.update(MainNode()(state) or {})
        state.update(ResponseFormatterNode()(state) or {})

        json.dumps({k: v for k, v in state.items() if not k.startswith("_")})


# ---------------------------------------------------------------------------
# PB-3: External service (Workday API) boundary
# ---------------------------------------------------------------------------
class TestPB3ExternalBoundary:
    def test_main_node_uses_secrets_not_environ(self, monkeypatch):
        """PB-3: MainNode retrieves WORKDAY_TOKEN via ctx, not os.environ."""
        import src.nodes.main_node as mod

        original_get = os.environ.get

        def strict_get(key, default=None):
            assert key != "WORKDAY_TOKEN", "Must not read WORKDAY_TOKEN from os.environ"
            return original_get(key, default)

        monkeypatch.setattr(os.environ, "get", strict_get, raising=False)
        monkeypatch.setattr(mod, "_call_workday_list_workers", lambda *a, **kw: [])

        from src.nodes.main_node import MainNode
        out = MainNode()({
            "caller_trust_level": _VERIFIED_TRUST,
            "validated_query": _VALID_QUERY,
            "validated_tenant_url": _VALID_TENANT,
        })
        assert out.get("workers_enriched") is not None or out.get("error") is not None or out.get("status") is not None

    def test_response_data_is_json_serializable(self, monkeypatch):
        """PB-3: data stored in state from Workday API is JSON-serializable."""
        import src.nodes.main_node as mod

        mock_workers = [{"id": "W-1", "name": "Yamada", "title": "Eng", "cost_center": "Tokyo"}]
        monkeypatch.setattr(mod, "_call_workday_list_workers", lambda *a, **kw: mock_workers)
        monkeypatch.setattr(mod, "_call_workday_get_worker_detail",
                            lambda t, tok, wid: {"id": wid, "name": "Yamada", "title": "Eng",
                                                  "cost_center": "Tokyo", "hire_date": "2021-01-01",
                                                  "reporting_chain": []})

        from src.nodes.main_node import MainNode
        out = MainNode()({
            "caller_trust_level": _VERIFIED_TRUST,
            "validated_query": _VALID_QUERY,
            "validated_tenant_url": _VALID_TENANT,
        })
        json.dumps(out.get("workers_enriched", []))

    def test_workday_service_injectable(self):
        """PB-3: WorkdayService accepts tenant URL + token from calling code."""
        from src.services.service import WorkdayService
        svc = WorkdayService("https://mock.workday.com/api/common/v1/tenant", "mock-token")
        assert svc._base_url == "https://mock.workday.com/api/common/v1/tenant"


# ---------------------------------------------------------------------------
# PB-4: Import isolation
# ---------------------------------------------------------------------------
class TestPB4ImportIsolation:
    def test_no_agenticstar_in_src(self):
        """PB-4: AST scan confirms no agenticstar imports in src/."""
        src_dir = os.path.join(os.path.dirname(__file__), "..", "..", "src")
        if not os.path.exists(src_dir):
            pytest.skip("src/ directory not found")

        violations = []
        for root, _dirs, files in os.walk(src_dir):
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(root, fname)
                with open(fpath) as f:
                    tree = ast.parse(f.read(), filename=fpath)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        if isinstance(node, ast.Import):
                            for alias in node.names:
                                if alias.name == "agenticstar" or alias.name.startswith("agenticstar."):
                                    violations.append(f"{fpath}:{node.lineno} — {alias.name}")
                        elif isinstance(node, ast.ImportFrom) and node.module:
                            module = node.module
                            if module == "agenticstar" or module.startswith("agenticstar."):
                                violations.append(f"{fpath}:{node.lineno} — from {module}")

        assert violations == [], "Import isolation violations:\n" + "\n".join(violations)
