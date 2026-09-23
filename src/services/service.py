"""WorkdayService — Workday Common REST API HTTP client.

Encapsulates all Workday API HTTP calls. Accepts tenant URL + OAuth2 Bearer token
as constructor args (secrets injected by nodes via ctx.secrets.require()).
Imports only Python stdlib (urllib, json). No Level 0 SDK imports.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import re
from typing import Any


_MAX_REPORTING_DEPTH_DEFAULT = 3
_REQUEST_TIMEOUT_S = 10
_WORKDAY_PATH_RE = re.compile(r"^/api/(?:common|staffing)/v[0-9]+/[^/]+$")
_LIST_PARAMETER_NAMES = {"limit", "offset", "search", "view"}


def validate_workday_base_url(value: str) -> str:
    """Validate and normalize a credential-bearing Workday REST base URL."""
    raw = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        raise ValueError("Workday base URL must use HTTPS")
    if not host.endswith((".workday.com", ".myworkday.com")):
        raise ValueError("Workday base URL host must be a workday.com or myworkday.com host")
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("Workday base URL must not contain credentials or a non-standard port")
    if parsed.query or parsed.fragment or not _WORKDAY_PATH_RE.fullmatch(parsed.path):
        raise ValueError("Workday base URL must be /api/<service>/<version>/<tenant>")
    return raw


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevent a Workday response from redirecting a bearer token elsewhere."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class WorkdayService:
    """Workday REST API HTTP client.

    Accepts tenant URL + OAuth2 Bearer token from calling nodes (injected via
    ctx.secrets.require() — never os.environ, never stored in State).
    """

    def __init__(self, tenant_url: str, bearer_token: str) -> None:
        self._base_url = validate_workday_base_url(tenant_url)
        self._bearer_token = bearer_token
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._bearer_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def list_workers(self, filters: dict[str, Any]) -> list[dict[str, Any]]:
        """GET /workers with filter params. Returns list of worker summary dicts.

        Returns empty list if no workers match (not an error).
        Raises urllib.error.HTTPError on 4xx/5xx.
        """
        params = urllib.parse.urlencode(
            {k: v for k, v in filters.items() if k in _LIST_PARAMETER_NAMES and v is not None}
        )
        url = f"{self._base_url}/workers"
        if params:
            url = f"{url}?{params}"

        req = urllib.request.Request(url, headers=self._headers())
        try:
            with self._opener.open(req, timeout=_REQUEST_TIMEOUT_S) as resp:
                data = json.loads(resp.read().decode())
                if isinstance(data, dict):
                    workers = data.get("data", [])
                elif isinstance(data, list):
                    workers = data
                else:
                    raise ValueError("Workday workers response must be a JSON object or array")
                if not isinstance(workers, list):
                    raise ValueError("Workday workers response data must be an array")
                return [_safe_worker_summary(w) for w in workers]
        except urllib.error.HTTPError:
            raise
        except Exception as exc:
            raise ConnectionError(f"Network error querying Workday workers: {exc}") from exc

    def get_worker_detail(
        self, worker_id: str, max_reporting_depth: int = _MAX_REPORTING_DEPTH_DEFAULT
    ) -> dict[str, Any]:
        """GET /workers/{id}. Returns enriched worker dict.

        Traversal depth guard: reporting_chain is bounded to max_reporting_depth hops.
        Raises urllib.error.HTTPError on 4xx/5xx.
        """
        url = f"{self._base_url}/workers/{urllib.parse.quote(worker_id, safe='')}"
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with self._opener.open(req, timeout=_REQUEST_TIMEOUT_S) as resp:
                data = json.loads(resp.read().decode())
                return _safe_worker_detail(data, max_reporting_depth)
        except urllib.error.HTTPError:
            raise
        except Exception as exc:
            raise ConnectionError(f"Network error fetching worker {worker_id}: {exc}") from exc


def _safe_worker_summary(raw: dict[str, Any]) -> dict[str, Any]:
    """Extract JSON-serializable worker summary fields (no Pydantic, no objects)."""
    return {
        "id": str(raw.get("workerId") or raw.get("id") or ""),
        "name": str(raw.get("descriptor") or raw.get("workerName") or raw.get("name") or ""),
        "title": str(raw.get("businessTitle") or raw.get("jobTitle") or raw.get("title") or ""),
        "cost_center": _descriptor(raw.get("primarySupervisoryOrganization"))
        or str(raw.get("costCenter") or raw.get("cost_center") or ""),
    }


def _safe_worker_detail(raw: dict[str, Any], max_depth: int) -> dict[str, Any]:
    """Extract JSON-serializable worker detail fields with bounded reporting chain."""
    worker_data = raw.get("data", raw) if isinstance(raw, dict) else raw

    reporting_chain = _extract_reporting_chain(worker_data, max_depth)

    return {
        "id": str(worker_data.get("workerId") or worker_data.get("id") or ""),
        "name": str(worker_data.get("descriptor") or worker_data.get("workerName") or worker_data.get("name") or ""),
        "title": str(worker_data.get("businessTitle") or worker_data.get("jobTitle") or worker_data.get("title") or ""),
        "cost_center": _descriptor(worker_data.get("primarySupervisoryOrganization"))
        or str(worker_data.get("costCenter") or worker_data.get("cost_center") or ""),
        "hire_date": str(worker_data.get("hireDate") or worker_data.get("hire_date") or ""),
        "reporting_chain": reporting_chain,
    }


def _descriptor(value: Any) -> str:
    return str(value.get("descriptor") or "") if isinstance(value, dict) else ""


def _extract_reporting_chain(worker_data: dict[str, Any], max_depth: int) -> list[str]:
    """Extract bounded reporting chain (manager names). Max depth = max_depth hops."""
    chain: list[str] = []
    current = worker_data
    for _ in range(max_depth):
        manager = current.get("manager") or current.get("reportsTo") or {}
        if not manager or not isinstance(manager, dict):
            break
        manager_name = str(manager.get("workerName") or manager.get("name") or "")
        if not manager_name:
            break
        chain.append(manager_name)
        current = manager
    return chain
