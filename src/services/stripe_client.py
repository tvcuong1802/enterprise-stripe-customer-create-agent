"""AgentCore Platform v1.0"""

# StripeClient — CMN-C1-595
# Look up and create Stripe customers over the Stripe REST API.
# - The API key comes from the bound secret provider ONLY
#   (current_secrets().require("STRIPE_API_KEY")) — never from state or os.environ,
#   and never cached on the instance.
# - S-3 egress guard: only a *.stripe.com host is permitted, so a misconfiguration
#   cannot exfiltrate the key off-platform.
# Constructor-injected into CustomerCreateNode. MUST NOT be built inside execute().

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlsplit

import requests

from framework.secrets.context import current_secrets

_ALLOWED_HOST_SUFFIX = ".stripe.com"
_DEFAULT_BASE = "https://api.stripe.com"
_TIMEOUT = 15  # seconds


class StripeClientError(RuntimeError):
    """Raised on a non-recoverable Stripe API error."""


class StripeClient:
    """Thin Stripe REST client for customer lookup + creation.

    ``base_url`` defaults to ``https://api.stripe.com``; the S-3 egress guard rejects
    any host that is not a ``*.stripe.com`` host so the key cannot leak off-platform.
    """

    def __init__(self, base_url: str = _DEFAULT_BASE, timeout: int = _TIMEOUT) -> None:
        self._base_url = (base_url or _DEFAULT_BASE).rstrip("/")
        self._timeout = timeout
        self._guard_egress(self._base_url)

    @staticmethod
    def _guard_egress(url: str) -> None:
        host = urlsplit(url).hostname or ""
        if not (host == "stripe.com" or host.endswith(_ALLOWED_HOST_SUFFIX)):
            raise StripeClientError(f"S-3 egress guard: host '{host}' is not permitted (only *{_ALLOWED_HOST_SUFFIX}).")

    @staticmethod
    def credential_available() -> bool:
        """True when the Stripe API key is provisioned in the bound secret provider.

        Non-raising probe (``get`` returns ``None`` on a miss) used by the caller to
        FAIL-CLOSED before any live HTTP call when no credential is present — e.g.
        the STG smoke, which has no ``STRIPE_API_KEY`` and must not attempt a real
        Stripe call. The key is never cached on the instance; this only checks
        presence.
        """
        return bool(current_secrets().get("STRIPE_API_KEY"))

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        # Key fetched per call from the bound provider; never cached on self.
        key = current_secrets().require("STRIPE_API_KEY")
        headers = {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        }
        if extra:
            headers.update(extra)
        return headers

    def _url(self, path: str) -> str:
        url = f"{self._base_url}{path}"
        self._guard_egress(url)
        return url

    # ── Read ──────────────────────────────────────────────────────────────
    def find_customer_by_email(self, email: str) -> dict[str, str] | None:
        """Dedup lookup — return the first customer matching ``email`` or None."""
        url = self._url("/v1/customers")
        # Annotated at the binding: a bare literal with mixed str/int values infers
        # dict[str, object], which requests' stubs reject for `params`.
        query: dict[str, str | int] = {"email": email, "limit": 1}
        resp = requests.get(
            url,
            headers=self._headers(),
            params=query,
            timeout=self._timeout,
        )
        if not resp.ok:
            raise StripeClientError(f"GET customers?email failed ({resp.status_code}).")
        data = resp.json().get("data") or []
        return self.normalize_customer(data[0]) if data else None

    def get_customer(self, customer_id: str) -> dict[str, str]:
        """Retrieve a customer by id."""
        url = self._url(f"/v1/customers/{customer_id}")
        resp = requests.get(url, headers=self._headers(), timeout=self._timeout)
        if not resp.ok:
            raise StripeClientError(f"GET customer {customer_id} failed ({resp.status_code}).")
        return self.normalize_customer(resp.json())

    # ── Write ─────────────────────────────────────────────────────────────
    def create_customer(self, params: dict[str, Any], idempotency_key: str) -> dict[str, str]:
        """Create a customer via POST /v1/customers with an idempotency key.

        ``params`` = {"email", "name", "metadata": {..}} (form-encoded; metadata is
        expanded to Stripe's ``metadata[key]=value`` bracket notation).
        """
        url = self._url("/v1/customers")
        form = self._encode_params(params)
        resp = requests.post(
            url,
            headers=self._headers(
                {
                    "Idempotency-Key": idempotency_key,
                    "Content-Type": "application/x-www-form-urlencoded",
                }
            ),
            data=form,
            timeout=self._timeout,
        )
        if not resp.ok:
            raise StripeClientError(f"POST /v1/customers failed ({resp.status_code}).")
        return self.normalize_customer(resp.json())

    # ── Pure helpers (unit-testable without HTTP) ───────────────────────────
    @staticmethod
    def _encode_params(params: dict[str, Any]) -> dict[str, str]:
        """Flatten create params into Stripe form fields (metadata -> brackets)."""
        form: dict[str, str] = {}
        if params.get("email"):
            form["email"] = str(params["email"])
        if params.get("name"):
            form["name"] = str(params["name"])
        for k, v in (params.get("metadata") or {}).items():
            form[f"metadata[{k}]"] = str(v)
        return form

    @staticmethod
    def idempotency_key(params: dict[str, Any]) -> str:
        """Deterministic key from the validated request — a retried create returns
        the same customer instead of a duplicate."""
        basis = json.dumps(
            {
                "email": params.get("email", ""),
                "name": params.get("name", ""),
                "metadata": params.get("metadata") or {},
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return "cmn-c1-595-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()

    @staticmethod
    def normalize_customer(raw: dict[str, Any]) -> dict[str, str]:
        """Map a Stripe customer object to our canonical dimensions."""
        return {
            "customer_id": str(raw.get("id", "")),
            "email": str(raw.get("email", "") or ""),
            "name": str(raw.get("name", "") or ""),
        }
