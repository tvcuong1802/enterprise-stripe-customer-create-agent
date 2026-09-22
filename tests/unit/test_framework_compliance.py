# CMN-C1-595 — Framework compliance tests (TC-01..08).

import os
import re
import typing

import pytest

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel

from src.nodes.customer_create_node import CustomerCreateNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State

_SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src")


class TestTC01StateContract:
    """TC-01: State is a flat TypedDict extending AgentState — no Pydantic/dataclass."""

    def test_state_is_typeddict_subtype(self):
        from framework.schemas.agent_state import AgentState

        # TypedDicts don't support issubclass — verify inheritance by field inclusion.
        assert set(AgentState.__annotations__).issubset(set(State.__annotations__))
        # All agent-specific annotations are plain `str` (compound values are JSON strings).
        hints = typing.get_type_hints(State)
        for field in (
            "parsed_intent",
            "customer_params",
            "validation_error",
            "output_language",
            "create_result",
            "confirmation_report",
        ):
            assert hints[field] is str, f"{field} must be str (JSON), got {hints[field]}"


class TestTC02InputSecurityGate:
    """TC-02: S-2 gate rejects unsafe input (status ERROR, no raise)."""

    def test_unsafe_input_rejected(self):
        node = PreProcessNode()
        state = {"input_context": {"instruction": "create email a@b.com ../../etc/passwd"}, "error_log": []}
        gated = node._extra_security_gate_input(state)
        assert gated["status"] == AgentStatus.ERROR.value


class TestTC03NoCredentialsInSrc:
    """TC-03: no JWT/API-key literals in src/ (gate-credential-scan mirror)."""

    def test_no_stripe_key_literal_in_src(self):
        pat = re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b")
        for root, _d, files in os.walk(_SRC):
            for f in files:
                if f.endswith(".py"):
                    text = open(os.path.join(root, f)).read()
                    assert not pat.search(text), f"credential-like literal in {f}"


class TestTC04InvocationContextViaConfigurable:
    """TC-04: InvocationContext is never stored in State; read via from_state."""

    def test_from_state_available_and_no_context_stored_in_state(self):
        from framework.schemas.invocation_context import InvocationContext

        assert hasattr(InvocationContext, "from_state")
        # The agent adds no InvocationContext-typed field and no invocation_context field.
        assert "invocation_context" not in State.__annotations__
        hints = typing.get_type_hints(State)
        assert all(v is not InvocationContext for v in hints.values())


class TestTC05AuditLogging:
    """TC-05: each node emits at least one domain event inside execute()."""

    @pytest.mark.parametrize(
        "node_factory,state",
        [
            (PreProcessNode, {"input_context": {"instruction": "create email a@b.com name X"}, "error_log": []}),
            (PostProcessNode, {"create_result": "{}", "output_language": "en", "error_log": []}),
        ],
    )
    def test_emit_called(self, monkeypatch, node_factory, state):
        events = []
        import src.nodes.pre_process_node as pre
        import src.nodes.post_process_node as post

        monkeypatch.setattr(pre, "emit_trace_event", lambda e, p, s: events.append(e))
        monkeypatch.setattr(post, "emit_trace_event", lambda e, p, s: events.append(e))
        node_factory().execute(state)
        assert events, f"{node_factory.__name__} emitted no domain event"

    def test_create_node_emits(self, monkeypatch):
        events = []
        import src.nodes.customer_create_node as cc

        monkeypatch.setattr(cc, "emit_trace_event", lambda e, p, s: events.append(e))
        # blocked path still emits a domain event
        CustomerCreateNode(client=object()).execute({"validation_error": "x", "error_log": []})
        assert events


class TestTC06TC07FinalGatesNonBypassable:
    """TC-06/07: overriding the @final S-2/S-3 gates raises TypeError at class def."""

    def test_cannot_override_input_gate(self):
        with pytest.raises(TypeError):

            class Bad(FunctionNode):  # noqa: F811
                def _security_gate_input(self, state):
                    return state

    def test_cannot_override_output_gate(self):
        with pytest.raises(TypeError):

            class Bad(FunctionNode):  # noqa: F811
                def _security_gate_output(self, result):
                    return result


class TestTC08TrustLevelEnforced:
    """TC-08: an under-trusted caller is refused (S-1 returns ERROR, does not raise)."""

    def test_anonymous_denied(self):
        node = PreProcessNode()
        assert node.required_trust_level == TrustLevel.VERIFIED_EXTERNAL
        out = node(
            {
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
                "correlation_id": "tc08",
                "input_context": {"instruction": "create email a@b.com"},
                "error_log": [],
            }
        )
        assert str(out.get("status")).lower().endswith("error")

    def test_read_nodes_at_verified_external_and_the_writer_at_internal(self):
        """CustomerCreateNode is the one node that must NOT be at VERIFIED_EXTERNAL.

        This test used to assert that level on every node, which pinned exactly the state
        CoE prohibits: VERIFIED_EXTERNAL is what run_agent_marketplace() grants every
        Marketplace user, so declaring it on the node that creates a Stripe customer makes
        that reachable by all of them (an internal ruling / the framework contract; (internal reference removed)).

        The declaration stays honest and Graph.add_edges() routes around the node for a
        caller who cannot reach INTERNAL.
        """
        assert CustomerCreateNode.required_trust_level == TrustLevel.INTERNAL
        for cls in (PreProcessNode, PostProcessNode):
            assert cls.required_trust_level == TrustLevel.VERIFIED_EXTERNAL, cls.__name__
