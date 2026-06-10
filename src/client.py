"""ServiceTitan OAuth2 client.

Loads credentials from the master vault, fetches a Tenant-scoped access token,
caches it on disk until shortly before expiry, and exposes a `get()` helper that
auto-injects the Authorization and ST-App-Key headers plus handles 429 backoff.

Reads (never prints) these env vars:
    SERVICETITAN_CLIENT_ID
    SERVICETITAN_CLIENT_SECRET
    SERVICETITAN_APP_KEY          (header: ST-App-Key)
    SERVICETITAN_TENANT_ID        (numeric tenant aka "Tenant ID" in ST API portal)
    ST_ENVIRONMENT                production | integration  (default: production)
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

CACHE = Path(__file__).resolve().parent.parent / "data" / "token_cache.json"

ENVIRONMENTS = {
    "production": {
        "auth": "https://auth.servicetitan.io/connect/token",
        "api":  "https://api.servicetitan.io",
    },
    "integration": {
        "auth": "https://auth-integration.servicetitan.io/connect/token",
        "api":  "https://api-integration.servicetitan.io",
    },
}


@dataclass
class STConfig:
    client_id: str
    client_secret: str
    app_key: str
    tenant_id: str
    env: str = "production"

    @classmethod
    def from_env(cls) -> "STConfig":
        missing = [k for k in (
            "SERVICETITAN_CLIENT_ID",
            "SERVICETITAN_CLIENT_SECRET",
            "SERVICETITAN_APP_KEY",
            "SERVICETITAN_TENANT_ID",
        ) if not os.environ.get(k)]
        if missing:
            raise RuntimeError(f"Missing required env vars: {missing}")
        env = os.environ.get("ST_ENVIRONMENT", "production").lower()
        if env not in ENVIRONMENTS:
            raise ValueError(f"ST_ENVIRONMENT must be one of {list(ENVIRONMENTS)}, got {env!r}")
        return cls(
            client_id=os.environ["SERVICETITAN_CLIENT_ID"],
            client_secret=os.environ["SERVICETITAN_CLIENT_SECRET"],
            app_key=os.environ["SERVICETITAN_APP_KEY"],
            tenant_id=os.environ["SERVICETITAN_TENANT_ID"],
            env=env,
        )

    @property
    def auth_url(self) -> str: return ENVIRONMENTS[self.env]["auth"]
    @property
    def api_base(self) -> str: return ENVIRONMENTS[self.env]["api"]


class ServiceTitanClient:
    def __init__(self, cfg: STConfig | None = None, *, timeout: int = 30):
        self.cfg = cfg or STConfig.from_env()
        self.timeout = timeout
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._load_cache()

    # -- token mgmt --------------------------------------------------------
    def _load_cache(self) -> None:
        if not CACHE.exists():
            return
        try:
            data = json.loads(CACHE.read_text())
            if data.get("env") == self.cfg.env and data.get("expires_at", 0) > time.time() + 60:
                self._token = data["access_token"]
                self._expires_at = data["expires_at"]
        except Exception:
            pass  # corrupt cache, ignore

    def _save_cache(self) -> None:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps({
            "env": self.cfg.env,
            "access_token": self._token,
            "expires_at": self._expires_at,
        }))
        CACHE.chmod(0o600)

    def _refresh(self) -> None:
        r = requests.post(
            self.cfg.auth_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.cfg.client_id,
                "client_secret": self.cfg.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise RuntimeError(f"OAuth token request failed: {r.status_code} {r.text[:300]}")
        body = r.json()
        self._token = body["access_token"]
        self._expires_at = time.time() + int(body.get("expires_in", 900)) - 30
        self._save_cache()

    @property
    def token(self) -> str:
        if not self._token or time.time() >= self._expires_at:
            self._refresh()
        assert self._token
        return self._token

    # -- request -----------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": self.token,
            "ST-App-Key": self.cfg.app_key,
            "Accept": "application/json",
        }

    def get(self, path: str, params: dict[str, Any] | None = None, *,
            max_retries: int = 4) -> requests.Response:
        """GET {api_base}{path}. Path may include {tenant} placeholder."""
        path = path.replace("{tenant}", self.cfg.tenant_id)
        url = f"{self.cfg.api_base}{path}"
        attempt = 0
        while True:
            r = requests.get(url, headers=self._headers(), params=params, timeout=self.timeout)
            if r.status_code == 429 and attempt < max_retries:
                wait = int(r.headers.get("Retry-After", "5"))
                time.sleep(wait)
                attempt += 1
                continue
            if r.status_code == 401 and attempt < 1:
                # token may have been revoked; force refresh once
                self._token = None
                attempt += 1
                continue
            return r
