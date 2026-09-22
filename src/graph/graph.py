"""AgentCore Platform v1.0 — Graph (CMN-C1-595 StripeCustomerCreateAgent).

Cat 1, L1-direct AgentBaseGraph. Single capability: provision a Stripe customer
from a natural-language instruction, behind an extract → validate → dedup →
idempotent-create → audit envelope. The three fixed slots are:

    START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END

  pre_process = PreProcessNode        (IntentParse + FieldExtract + validation, S-2)
  main        = CustomerCreateNode    (dedup lookup + idempotent create — the write)
  post_process= PostProcessNode       (confirmation, S-3 egress + S-4 audit, EN/JA)

`main` is a single FunctionNode (Cat 1 — no inner graph, no GraphNode). The default
AgentBaseGraph pipeline is used unchanged (no add_edges override).

Services are constructor-injected here (stateless, shared) — never built inside
execute(). Config (e.g. stripe_api_base) comes from config/agent.yaml.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START

from framework.graph.agent_base_graph import AgentBaseGraph
from shared.utils.audit_logger import emit_trace_event

from src.nodes.customer_create_node import CustomerCreateNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State
from src.services.intent_parser_service import CustomerIntentParserService
from src.services.stripe_client import StripeClient


class StripeCustomerCreateAgent(AgentBaseGraph):
    """Dedup-guarded, idempotent Stripe customer-create pipeline."""

    @property
    def name(self) -> str:
        return "cmn_c1_595"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode

        # Constructor DI — services are stateless + shared; never built in execute().
        parser = CustomerIntentParserService()
        base_url = self.config.get("stripe_api_base", "") or "https://api.stripe.com"
        client = StripeClient(base_url=base_url)

        self._nodes["pre_process"] = PreProcessNode(parser=parser)
        self._nodes["main"] = CustomerCreateNode(client=client)
        self._nodes["post_process"] = PostProcessNode()

    def add_edges(self: Any) -> None:
        """The backbone, with one conditional edge in front of the writer.

        Overridden ONLY to add the write gate. `main` creates a Stripe customer and
        declares INTERNAL; a caller who cannot reach that level goes straight to
        post_process, which reports which action was not performed. Skipping is what keeps
        the declaration honest without ending the run at status error -- the S-1 gate runs
        before execute(), so the node cannot refuse for itself.
        """
        self._sg.add_edge(START, "initialize")
        self._sg.add_edge("initialize", "pre_process")
        self._sg.add_conditional_edges("pre_process", self._write_route)
        self._sg.add_conditional_edges("main", self.route)
        self._sg.add_edge("post_process", "finalize")
        self._sg.add_edge("finalize", END)

    def _write_route(self: Any, state: Any) -> str:
        """`main` for a caller permitted to write, `post_process` for everyone else."""
        from src.services.write_scope import caller_may_write  # noqa: PLC0415

        if caller_may_write(state):
            return "main"
        emit_trace_event("customer_write_refused", {"reason": "write_not_available_on_this_channel"}, state)
        return "post_process"

    def get_output(self, state: Any) -> dict[str, Any]:
        """The framework envelope, with a reader-facing payload and a trailer.

        The envelope shape is kept: BaseGraph.invoke() hands this straight to the
        Marketplace runner and to the Stage-5 evidence script, and both read it as a
        dict. Only `output` changes -- from the framework value to the Markdown a chat
        reader can actually read. The structured payload stays under its own key,
        because the HTTP adapter and the boundary tests index into it.
        """
        from src.services.agent_scope import SCOPE_EN, SCOPE_JA  # noqa: PLC0415
        from src.services.output_envelope import with_disclaimer  # noqa: PLC0415

        return with_disclaimer(
            dict(super().get_output(state)),
            state,
            scope_en=SCOPE_EN,
            scope_ja=SCOPE_JA,
            rendered=_render_payload(state),
            preserve_as="formatted_output",
        )


def _render_payload(state: Any) -> str:
    """This agent's Markdown for the reader, or "" when there is nothing to render.

    The structured result stays in State: the HTTP adapter and anything downstream read
    it as data, and rendering it in the node would break them. Only the PAYLOAD changes.
    """
    from src.services.output_envelope import render_markdown  # noqa: PLC0415

    if not isinstance(state, dict):
        return ""
    payload = state.get("formatted_output")
    if isinstance(payload, str):
        return payload.strip()
    return render_markdown(payload)
