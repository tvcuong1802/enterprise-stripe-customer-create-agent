"""AgentCore Platform v1.0 — State (CMN-C1-595 StripeCustomerCreateAgent)."""

# ADR-005: State must be a flat TypedDict — never a Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects cause silent
# corruption. Extend AgentState with agent-specific fields only.
#
# SAFETY CONTRACT (CMN-C1-595):
# - All complex objects are stored as JSON-serialized str (never raw dict/list) so
#   the checkpoint stays msgpack-safe.
# - No Stripe API key / credential in state (checkpoint DB leakage) — the key is
#   fetched at call time via current_secrets().require("STRIPE_API_KEY") and never
#   persisted.
# - InvocationContext is accessed via config["configurable"] / InvocationContext
#   .from_state(state) only, never stored in state.

from __future__ import annotations

from typing import NotRequired

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Stripe customer-create state for CMN-C1-595.

    Shared fields (user_input, validated_input, status, session_id, node_history,
    error_log, formatted_output, result, caller_trust_level, hitl_*, etc.) are
    inherited from AgentState and are NOT redeclared here.

    Field ownership:
        PreProcessNode      -> parsed_intent, customer_params, validation_error, output_language
        CustomerCreateNode  -> create_result, (validation_error on API error)
        PostProcessNode     -> confirmation_report, formatted_output (inherited; surfaced by get_output)
    """

    # The language the model decided this reader wants, carried to the trailer.
    # Declared because LangGraph merges only declared fields -- an undeclared key is
    # dropped between nodes and the decision would be computed and lost.
    answer_language: str

    # ── PreProcessNode outputs ─────────────────────────────────────────────
    # JSON: {"action": "create"|"lookup", "email": str, "name": str,
    #        "metadata": {..}, "customer_id": str, "dry_run": bool}
    parsed_intent: NotRequired[str]  # default ""
    # JSON: validated create params {"email": str, "name": str, "metadata": {..}}
    customer_params: NotRequired[str]  # default ""
    # Non-empty string signals a business-invalid input; downstream nodes
    # short-circuit and post_process renders a refusal/error report.
    validation_error: NotRequired[str]  # default ""
    # Output rendering language: "en" | "ja" | "bilingual" (default "en").
    output_language: NotRequired[str]  # default "en"

    # ── CustomerCreateNode (main slot) output ──────────────────────────────
    # JSON: {"created": bool, "matched_existing": bool, "dry_run": bool,
    #        "lookup": bool, "customer_id": str, "email": str, "name": str}
    create_result: NotRequired[str]  # default ""

    # ── PostProcessNode output ─────────────────────────────────────────────
    # Final Markdown confirmation/refusal report. S-3 gate verifies no Stripe key
    # value crosses this boundary. `formatted_output` (inherited from AgentState)
    # carries the same report and is what get_output() surfaces.
    confirmation_report: NotRequired[str]  # default ""
