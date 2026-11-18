"""Clients talking to center agents. HTTP in dev/compose; in-process for tests."""
from __future__ import annotations

import os
import sys

import httpx

from .config import settings


class HttpAgentClient:
    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout

    def meta(self, center_code: str) -> dict:
        url = settings.center_agents[center_code]
        r = httpx.get(f"{url}/meta", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def assay(self, center_code: str) -> dict:
        url = settings.center_agents[center_code]
        r = httpx.get(f"{url}/assay", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def execute(self, center_code: str, job: dict) -> dict:
        url = settings.center_agents[center_code]
        try:
            r = httpx.post(f"{url}/execute", json=job, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            return {"status": "failed", "error": f"agent unreachable: {e}"}


class LocalAgentClient:
    """In-process center agents (pytest). Requires center_agent on sys.path."""

    def __init__(self, data_root: str):
        center_agent_dir = os.path.join(os.path.dirname(__file__), "..", "..", "center_agent")
        if center_agent_dir not in sys.path:
            sys.path.insert(0, os.path.abspath(center_agent_dir))
        from center_agent import executor  # noqa: PLC0415

        self._executor = executor
        self.data_root = data_root

    def _dir(self, center_code: str) -> str:
        return os.path.join(self.data_root, center_code)

    def meta(self, center_code: str) -> dict:
        import csv
        import json

        d = self._dir(center_code)
        with open(os.path.join(d, "registry.csv"), newline="") as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames or []
            n = sum(1 for _ in reader)
        meta_path = os.path.join(d, "meta.json")
        extra = json.load(open(meta_path)) if os.path.exists(meta_path) else {}
        return {"center": center_code, "patients": n, "fields": fields,
                "dx_years": extra.get("dx_years", []), "seed": extra.get("seed")}

    def assay(self, center_code: str) -> dict:
        import json

        path = os.path.join(self._dir(center_code), "assay.json")
        if not os.path.exists(path):
            raise FileNotFoundError(f"no assay.json for {center_code}")
        return json.load(open(path))

    def execute(self, center_code: str, job: dict) -> dict:
        try:
            return self._executor.execute(self._dir(center_code), job)
        except Exception as e:
            return {"status": "failed", "error": f"{type(e).__name__}: {e}"}


_client = None


def get_agent_client():
    global _client
    if _client is None:
        _client = HttpAgentClient()
    return _client


def set_agent_client(client) -> None:
    global _client
    _client = client
