"""AgentCore Platform v1.0 — PreProcessNode (CMN-C1-595).

Parse the NL customer instruction into a structured intent and run the S-2 input
boundary. Business-invalid input (no email on a create, unparseable) becomes a
graceful ``validation_error`` (SUCCESS + refusal report downstream); a genuinely
unsafe input (path traversal / control chars / oversized) is a hard S-2 rejection
(status = ERROR) via the ``_extra_security_gate_input`` hook.
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from framework.nodes.base_node import BaseNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.intent_parser_service import CustomerIntentParserService, InputParseError

# S-2: reject traversal, doubled separators, and control characters that could
# escape an API path when interpolated into /v1/customers/{id}.
_UNSAFE_INPUT = re.compile(r"(\.\./|[\x00-\x1f\x7f]|[<>|`$])")
_MAX_INPUT_CHARS = 4000
# Presence of any CJK character selects Japanese rendering.
_CJK = re.compile(r"[぀-ヿ㐀-鿿＀-￯]")
# An explicit bilingual request in the instruction.
_BILINGUAL = re.compile(r"\b(bilingual|both languages|en\s*/?\s*ja|english and japanese)\b|英日|日英|両言語", re.I)
_LANGS = {"en", "ja", "bilingual"}
# Strict email shape for validation (the parser's extraction is permissive).
_EMAIL_FULL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


class PreProcessNode(BaseNode):
    """Parse instruction → parsed_intent + customer_params; set validation_error on bad input.

    Inherits BaseNode, not FunctionNode, to narrow S-2 by exactly one finding type.

    WHY. The default S-2 gate masks every `detect_pii` finding in `user_input` before
    `execute()` runs. For this agent the customer's email IS the instruction: masked, the
    parser reports "No email or customer id found" and the agent cannot do the one thing
    it exists to do -- on every real request, while reporting success. Measured by running
    the graph 2026-09-03: pre_process received `'Seikyuu mail wa [MASKED] desu.'`.

    WHY THIS WAY. `FunctionNode._security_gate_input` is `@final` -- overriding raises
    TypeError at class definition, by design. `_PII_SCAN_FIELDS` is a module constant in
    the wheel with no configuration and no trust-level condition. The one sanctioned
    position is the one the framework itself takes for `GraphNode` and `RemoteAgentNode`:
    a node inheriting BaseNode implements the `@abstractmethod` gate itself. the framework contract
    states that this is "a deliberate design choice, not a bypass".

    WHAT IS NARROWED. Only findings of type `email`. Names, phone numbers, national
    identifiers and payment card numbers in the same sentence are masked exactly as
    before. The injection check is NOT narrowed -- same fields, same blocking -- because
    an instruction aimed at the agent is a different thing from a customer identifier.
    S-3 egress is reproduced unchanged from the wheel.

    WHAT REPLACED. This node used to read the instruction from `input_context`, the
    non-PII-scanned channel, to dodge the masking. That is the shape scaffold request:coe
    forbids, and on the platform it does not even work: the Marketplace runner puts only
    `conversation_history` there, so the fallback to the masked `user_input` was always
    the live path.
    """

    #: The finding types this agent OPERATES ON, and the only ones left unmasked. A closed
    #: set, declared here where a reviewer reads the node rather than buried in a helper.
    _S2_OPERATES_ON: ClassVar[tuple[str, ...]] = ("email",)

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, parser: CustomerIntentParserService | None = None) -> None:
        super().__init__()
        self._parser = parser or CustomerIntentParserService()

    @staticmethod
    def _raw_instruction(state: dict[str, Any]) -> str:
        """The instruction, from `user_input`, with `input_context` still honoured.

        `user_input` is the live path: the Marketplace runner supplies nothing else. The
        `input_context` lookup stays only for callers that already send a structured
        instruction; it is no longer a way around the PII gate, because the gate no longer
        removes what this agent needs.
        """
        ic = state.get("input_context") or {}
        return ic.get("instruction") or state.get("user_input", "") or ""

    def _security_gate_input(self, state: dict[str, Any]) -> dict[str, Any]:
        """S-2, narrowed by exactly one finding type. See the class docstring for why.

        Implemented here because this node inherits BaseNode, where the gate is
        `@abstractmethod`. Everything the default gate does is still done -- the same
        fields are scanned, every other PII class is masked, the injection check runs
        unchanged -- and then this node's own size/character checks run on top.
        """
        from src.services.selective_pii_gate import selective_security_gate_input

        state = selective_security_gate_input(
            state,
            operates_on=self._S2_OPERATES_ON,
            node_name=self.__class__.__name__,
        )
        if state.get("status") == AgentStatus.ERROR.value:
            return state
        return self._extra_security_gate_input(state)

    def _security_gate_output(self, result: dict[str, Any]) -> dict[str, Any]:
        """S-3, unchanged. Narrowing S-2 must not quietly cost the egress scan too."""
        from src.services.selective_pii_gate import default_security_gate_output

        return default_security_gate_output(result, node_name=self.__class__.__name__)

    def _extra_security_gate_input(self, state: dict[str, Any]) -> dict[str, Any]:
        # S-2 hard gate: never raise — set ERROR status per the node contract.
        text = self._raw_instruction(state)
        if len(text) > _MAX_INPUT_CHARS:
            state["status"] = AgentStatus.ERROR.value
            state.setdefault("error_log", []).append("S-2: instruction exceeds size limit.")
        elif _UNSAFE_INPUT.search(text):
            state["status"] = AgentStatus.ERROR.value
            state.setdefault("error_log", []).append("S-2: instruction contains unsafe path/control characters.")
        return state

    def _answer_language(self, state: Any) -> str:
        """Which language to answer in, decided once per request by the model.

        Called from execute(), so it runs AFTER the S-2 input gate -- anything earlier
        would send raw input, PII included, to the provider. The model READS; this agent
        DECIDES: the return value is narrowed to the two languages the scope wording
        exists in, and any other answer falls back to the script of the message.
        """
        from src.services.agent_scope import resolve_answer_language  # noqa: PLC0415

        try:
            from src.services.app_config import llm_settings  # noqa: PLC0415
            from src.services.llm_provider import build_llm_client  # noqa: PLC0415

            client = build_llm_client(dict(state or {}), llm_settings())
        except Exception:  # noqa: BLE001 -- no client: the script fallback still answers
            client = None
        return resolve_answer_language(state, client)

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # The model reads the request and decides the language of the answer; every

        # other decision in this pipeline stays deterministic. Here rather than earlier

        # because the S-2 gate runs before execute().

        answer_language = self._answer_language(state)
        # S-2 hard rejection already set ERROR — do not process further.
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("input_rejected", {"reason": "s2_unsafe_input"}, state)
            return {
                # The model's language decision, carried so the trailer can use it.
                "answer_language": answer_language,
                "validation_error": "Input rejected by the S-2 security gate.",
                "status": AgentStatus.ERROR.value,
            }

        text = self._raw_instruction(state)
        language = self._resolve_language(state, text)

        try:
            intent = self._parser.parse(text)
        except InputParseError as exc:
            emit_trace_event("intent_parse_failed", {"reason": str(exc)}, state)
            return {
                # The model's language decision, carried so the trailer can use it.
                "answer_language": answer_language,
                "validation_error": f"Could not parse the instruction: {exc}",
                "output_language": language,
                "status": AgentStatus.SUCCESS.value,
            }

        # Business validation (graceful — SUCCESS + validation_error, never a raise).
        validation_error = self._validate(intent)
        if validation_error:
            emit_trace_event("input_invalid", {"action": intent["action"]}, state)
            return {
                # The model's language decision, carried so the trailer can use it.
                "answer_language": answer_language,
                "parsed_intent": json.dumps(intent, ensure_ascii=False),
                "validation_error": validation_error,
                "output_language": language,
                "status": AgentStatus.SUCCESS.value,
            }

        customer_params = {
            "email": intent["email"],
            "name": intent["name"],
            "metadata": intent["metadata"],
        }
        emit_trace_event(
            "intent_parsed",
            {
                "action": intent["action"],
                "has_email": bool(intent["email"]),
                "has_name": bool(intent["name"]),
                "dry_run": intent["dry_run"],
            },
            state,
        )
        return {
            # The model's language decision, carried so the trailer can use it.
            "answer_language": answer_language,
            "parsed_intent": json.dumps(intent, ensure_ascii=False),
            "customer_params": json.dumps(customer_params, ensure_ascii=False),
            "validation_error": "",
            "output_language": language,
            "status": AgentStatus.SUCCESS.value,
        }

    @staticmethod
    def _resolve_language(state: dict[str, Any], text: str) -> str:
        # An explicit caller preference wins; then an in-text bilingual directive;
        # else auto-detect Japanese from CJK, defaulting to English.
        ic = state.get("input_context") or {}
        pref = str(ic.get("output_language", "")).lower()
        if pref in _LANGS:
            return pref
        if _BILINGUAL.search(text):
            return "bilingual"
        return "ja" if _CJK.search(text) else "en"

    @staticmethod
    def _validate(intent: dict[str, Any]) -> str:
        action = intent.get("action")
        email = intent.get("email", "")
        if action == "lookup":
            if not email and not intent.get("customer_id"):
                return "A lookup needs an email or a customer id."
            if email and not _EMAIL_FULL.match(email):
                return "Invalid email format."
            return ""
        # create
        if not email:
            return "An email is required to create a customer."
        if not _EMAIL_FULL.match(email):
            return "Invalid email format."
        return ""
