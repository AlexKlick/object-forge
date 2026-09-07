"""Loopback-only HTTP client for the optional Forge styling sidecar."""
from __future__ import annotations

import ipaddress
import json
import math
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class StyleError(ValueError):
    pass


class StyleBusy(StyleError):
    def __init__(self, free_mb, needed_mb):
        self.free_mb = free_mb
        self.needed_mb = needed_mb
        super().__init__(f"gpu busy free={free_mb} needed={needed_mb}")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise StyleError("Style redirects are not allowed.")


class StyleClient:
    def __init__(self, url: str, timeout_s: float = 600):
        parts = urlsplit(url)
        try:
            local = ipaddress.ip_address(parts.hostname or "").is_loopback
            parts.port
        except ValueError:
            local = False
        if (parts.scheme != "http" or not local or parts.username or parts.password
                or parts.query or parts.fragment or parts.path not in {"", "/"}):
            raise ValueError("FORGE_STYLE_URL must be an HTTP loopback IP origin.")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("FORGE_STYLE_TIMEOUT_S must be finite and positive.")
        self.url = url.rstrip("/") + "/v1/style"
        self.timeout_s = timeout_s
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    def _request(self, path, body=None):
        try:
            data = None if body is None else json.dumps(body, allow_nan=False).encode()
            req = Request(self.url + path, data=data, headers={"Content-Type": "application/json"},
                          method="GET" if body is None else "POST")
            try:
                with self.opener.open(req, timeout=self.timeout_s) as response:
                    result = json.loads(response.read())
            except HTTPError as exc:
                try:
                    result = json.loads(exc.read())
                finally:
                    exc.close()
                if exc.code == 503 and isinstance(result, dict) and result.get("error") == "gpu_busy":
                    raise StyleBusy(result.get("free_mb"), result.get("needed_mb")) from exc
                raise StyleError(f"Style HTTP {exc.code}: {result}") from exc
            if not isinstance(result, dict):
                raise StyleError("Style response must be an object.")
            return result
        except StyleError:
            raise
        except Exception as exc:
            raise StyleError(f"Style request failed: {exc}") from exc

    def status(self) -> dict:
        return self._request("/status")

    def render(self, request: dict) -> dict:
        return self._request("/render", request)
