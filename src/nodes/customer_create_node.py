"""AgentCore Platform v1.0 — CustomerCreateNode (CMN-C1-595).

The ``main`` slot — the core capability. Provisions a Stripe customer from the
parsed intent: it runs a duplicate lookup by email first and returns the existing
customer on a match (no second record); otherwise it creates the customer via
``POST /v1/customers`` with an idempotency key. A ``lookup`` intent resolves an
existing customer by email or id. ``dry_run`` returns the projected create without
writing. A blocked/invalid upstream state is a valid business outcome (status
SUCCESS), not an execution error — post_process renders it.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.stripe_client import StripeClient, StripeClientError


class CustomerCreateNode(FunctionNode):
    """Guarded Stripe customer provisioning — dedup lookup then idempotent create."""

    # INTERNAL, and it stays INTERNAL. This node creates a customer record in the
    # customer's Stripe account. `run_agent_marketplace()` stamps every caller
    # VERIFIED_EXTERNAL with no surface to raise it, so declaring that level here makes
    # creating records reachable by every Marketplace user. CoE ruled write modes out of
    # scope for this entry point and named the lowering as the prohibited workaround
    # (an internal ruling / the framework contract; (internal reference removed) rejects it as option one).
    #
    # The S-1 gate runs in BaseNode.__call__() BEFORE execute(), so this node cannot make
    # the check itself: it would never run, and the whole invocation would end at status
    # error, which the runner raises on. Graph.add_edges() routes around it instead.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.INTERNAL

    def __init__(self, client: StripeClient | None = None) -> None:
        super().__init__()
        self._client = client or StripeClient()

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        blocked = self._blocked_reason(state)
        if blocked:
            emit_trace_event("customer_create_skipped", {"reason": blocked}, state)
            return {
                "create_result": json.dumps(
                    {"created": False, "matched_existing": False, "dry_run": False, "lookup": False, "reason": blocked},
                    ensure_ascii=False,
                ),
                "status": AgentStatus.SUCCESS.value,
            }

        intent = json.loads(state.get("parsed_intent") or "{}")

        # FAIL-CLOSED: a dry-run is a pure projection (no live call); everything
        # else — lookup, dedup lookup, and the create write — needs the Stripe
        # credential. When no STRIPE_API_KEY is provisioned (e.g. the STG smoke),
        # never attempt a live Stripe call: return a safe "cannot execute" plan-only
        # outcome at SUCCESS so the pipeline completes and post_process renders a
        # graceful notice, instead of MissingSecret propagating to status=error.
        if not intent.get("dry_run") and not self._client.credential_available():
            emit_trace_event("customer_create_no_credential", {"action": intent.get("action", "")}, state)
            params = json.loads(state.get("customer_params") or "{}")
            return {
                "create_result": json.dumps(
                    {
                        "created": False,
                        "matched_existing": False,
                        "dry_run": False,
                        "lookup": intent.get("action") == "lookup",
                        "no_credential": True,
                        "customer_id": intent.get("customer_id", ""),
                        "email": params.get("email", "") or intent.get("email", ""),
                        "name": params.get("name", ""),
                    },
                    ensure_ascii=False,
                ),
                "status": AgentStatus.SUCCESS.value,
            }

        try:
            if intent.get("action") == "lookup":
                return self._do_lookup(intent, state)
            return self._do_create(intent, state)
        except StripeClientError as exc:
            emit_trace_event("customer_create_error", {"reason": str(exc)}, state)
            return {
                "validation_error": f"Stripe operation failed: {exc}",
                "create_result": json.dumps(
                    {
                        "created": False,
                        "matched_existing": False,
                        "dry_run": False,
                        "lookup": intent.get("action") == "lookup",
                        "reason": str(exc),
                    },
                    ensure_ascii=False,
                ),
                "status": AgentStatus.SUCCESS.value,
            }

    def _do_lookup(self, intent: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        customer_id = intent.get("customer_id", "")
        # get_customer() always returns a dict; find_customer_by_email() returns None on
        # a lookup miss, so the union is real -- the `(found or {})` reads below already
        # handle it.
        found: dict[str, str] | None
        if customer_id:
            found = self._client.get_customer(customer_id)
        else:
            found = self._client.find_customer_by_email(intent.get("email", ""))
        emit_trace_event("customer_lookup", {"found": bool(found)}, state)
        result = {
            "created": False,
            "matched_existing": bool(found),
            "dry_run": False,
            "lookup": True,
            "customer_id": (found or {}).get("customer_id", ""),
            "email": (found or {}).get("email", ""),
            "name": (found or {}).get("name", ""),
        }
        return {"create_result": json.dumps(result, ensure_ascii=False), "status": AgentStatus.SUCCESS.value}

    def _do_create(self, intent: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        params = json.loads(state.get("customer_params") or "{}")
        email = params.get("email", "")

        # Dry-run is a pure projection — no lookup, no write (no live Stripe call).
        if intent.get("dry_run"):
            emit_trace_event("customer_create_dry_run", {"email_present": bool(email)}, state)
            return {
                "create_result": json.dumps(
                    {
                        "created": False,
                        "matched_existing": False,
                        "dry_run": True,
                        "lookup": False,
                        "customer_id": "",
                        "email": email,
                        "name": params.get("name", ""),
                    },
                    ensure_ascii=False,
                ),
                "status": AgentStatus.SUCCESS.value,
            }

        # Dedup guard — return the existing customer instead of a duplicate.
        existing = self._client.find_customer_by_email(email)
        if existing:
            emit_trace_event("customer_matched_existing", {"matched": True}, state)
            return {
                "create_result": json.dumps(
                    {
                        "created": False,
                        "matched_existing": True,
                        "dry_run": False,
                        "lookup": False,
                        "customer_id": existing.get("customer_id", ""),
                        "email": existing.get("email", ""),
                        "name": existing.get("name", ""),
                    },
                    ensure_ascii=False,
                ),
                "status": AgentStatus.SUCCESS.value,
            }

        idem = self._client.idempotency_key(params)
        created = self._client.create_customer(params, idempotency_key=idem)
        emit_trace_event("customer_created", {"customer_id_present": bool(created.get("customer_id"))}, state)
        return {
            "create_result": json.dumps(
                {
                    "created": True,
                    "matched_existing": False,
                    "dry_run": False,
                    "lookup": False,
                    "customer_id": created.get("customer_id", ""),
                    "email": created.get("email", ""),
                    "name": created.get("name", ""),
                },
                ensure_ascii=False,
            ),
            "status": AgentStatus.SUCCESS.value,
        }

    @staticmethod
    def _blocked_reason(state: dict[str, Any]) -> str:
        if state.get("status") == AgentStatus.ERROR.value:
            return "input rejected by security gate"
        if state.get("validation_error"):
            # Bound to the declared return type: state is an untyped mapping, so the
            # subscript is Any.
            reason: str = state["validation_error"]
            return reason
        if not state.get("parsed_intent"):
            return "no parsed intent"
        return ""
