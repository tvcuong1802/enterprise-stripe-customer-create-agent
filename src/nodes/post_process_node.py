"""AgentCore Platform v1.0 — PostProcessNode (CMN-C1-595).

Render an EN / JA / bilingual confirmation, existing-match notice, lookup result,
dry-run preview, or refusal report from the parsed intent and create result, and
enforce the S-3 output boundary (no Stripe API key value may cross into the report).
Japanese output uses controlled terminology (用語統制) and keigo, with a
machine-generation disclaimer (認証された翻訳ではありません). Customer names,
metadata, and the customer ID are preserved verbatim.
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.services.write_scope import caller_may_write
from shared.utils.audit_logger import emit_trace_event

# S-3: Stripe key shapes must never appear in the rendered report.
_KEY_PATTERNS = [
    re.compile(r"(?i)\b[sr]k_(?:live|test)_[A-Za-z0-9]{8,}\b"),  # sk_live_/sk_test_/rk_
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"\b[A-Za-z0-9]{40,}\b"),  # long opaque tokens
]
_JA_DISCLAIMER = "※ 本確認は機械生成であり、認証された翻訳ではありません。"


class PostProcessNode(FunctionNode):
    """Build the EN/JA confirmation/refusal report; S-3 output gate."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        language = state.get("output_language", "en") or "en"

        if not state.get("create_result") and not caller_may_write(state):
            # BOTH halves are required. "no result" alone is not "the gate refused": a
            # disambiguation, a dry run, or a blocked precondition also leave the key
            # empty, and each of those must still render its own report. Only a caller
            # who cannot write on this channel gets the refusal notice.
            #
            # And the notice names the action that did not happen rather than reporting a
            # generic failure -- a reader told "failed" cannot tell a refusal from a bug,
            # and will retry against a customer record they believe was never created.
            from src.services.write_scope import write_unavailable_notice  # noqa: PLC0415

            notice = write_unavailable_notice(
                language,
                action_en="The customer record creation",
                action_ja="顧客レコードの作成",
                channel_en="an authorised company billing system",
                channel_ja="社内の権限ある請求システム",
            )
            emit_trace_event("customer_report_refused", {"language": language}, state)
            return {
                "confirmation_report": notice,
                "formatted_output": notice,
                "status": AgentStatus.SUCCESS.value,
            }

        report_en = self._render_en(state)
        report_ja = self._render_ja(state)

        if language == "ja":
            report = report_ja
        elif language == "bilingual":
            report = f"{report_en}\n\n---\n\n{report_ja}"
        else:
            report = report_en

        emit_trace_event("report_compiled", {"language": language}, state)
        # get_output() surfaces `formatted_output` — write that so invoke() returns
        # the report; the outer graph does NOT override get_output.
        return {
            "confirmation_report": report,
            "formatted_output": report,
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_output(self, result: dict[str, Any]) -> dict[str, Any]:
        # S-3: redact any residual Stripe key value from all string outputs.
        for key in ("confirmation_report", "formatted_output"):
            val = result.get(key)
            if isinstance(val, str):
                for pat in _KEY_PATTERNS:
                    val = pat.sub("[REDACTED-CREDENTIAL]", val)
                result[key] = val
        return result

    # ── Outcome classification ────────────────────────────────────────────────
    @staticmethod
    def _outcome(state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        res = json.loads(state.get("create_result") or "{}")
        # validation_error takes priority over the lookup branch: a Stripe
        # transport failure during a lookup sets validation_error AND
        # lookup=True, and must render as an authoritative error — not a
        # definitive "no customer found". A clean lookup-miss never sets
        # validation_error, so this does not swallow a genuine miss.
        if state.get("validation_error"):
            kind = "error"
        elif res.get("no_credential"):
            # No Stripe credential provisioned (e.g. STG smoke) — the agent
            # fail-closed and made no live call. A safe, non-error outcome.
            kind = "no_credential"
        elif res.get("lookup"):
            kind = "lookup_hit" if res.get("matched_existing") else "lookup_miss"
        elif res.get("matched_existing"):
            kind = "existing"
        elif res.get("dry_run"):
            kind = "dry_run"
        elif res.get("created"):
            kind = "created"
        else:
            kind = "error"
        return kind, res

    def _render_en(self, state: dict[str, Any]) -> str:
        kind, res = self._outcome(state)
        cid = res.get("customer_id", "?")
        email = res.get("email", "")
        name = res.get("name", "")
        who = f"**{name}** ({email})" if name else f"{email}"
        if kind == "created":
            return f"# Stripe Customer — Created\n\nCreated customer {who}.\n\n**Customer ID:** `{cid}`"
        if kind == "existing":
            return (
                f"# Stripe Customer — Existing Match (no duplicate created)\n\n"
                f"A customer with that email already exists: {who}.\n\n"
                f"**Customer ID:** `{cid}`"
            )
        if kind == "dry_run":
            return f"# Stripe Customer — Dry Run (no record created)\n\nWould create customer {who}."
        if kind == "lookup_hit":
            return f"# Stripe Customer — Lookup\n\nFound customer {who}.\n\n**Customer ID:** `{cid}`"
        if kind == "lookup_miss":
            return "# Stripe Customer — Lookup\n\nNo customer matched that email or id."
        if kind == "no_credential":
            tail = f" Requested for {who}." if who else ""
            return (
                "# Stripe Customer — Not Executed (no credential)\n\n"
                "No Stripe API credential is configured for this environment, so "
                "no live Stripe call was made and no customer was created or "
                "looked up." + tail
            )
        return (
            f"# Stripe Customer — Not Created\n\n{state.get('validation_error', 'The request could not be processed.')}"
        )

    def _render_ja(self, state: dict[str, Any]) -> str:
        kind, res = self._outcome(state)
        cid = res.get("customer_id", "?")
        email = res.get("email", "")
        name = res.get("name", "")
        who = f"「{name}」（{email}）" if name else f"{email}"
        if kind == "created":
            return (
                f"# Stripe 顧客 — 作成完了\n\n顧客 {who} を作成いたしました。\n\n"
                f"**顧客ID:** `{cid}`\n\n{_JA_DISCLAIMER}"
            )
        if kind == "existing":
            return (
                f"# Stripe 顧客 — 既存一致（重複は作成していません）\n\n"
                f"同一メールの顧客が既に存在します：{who}。\n\n"
                f"**顧客ID:** `{cid}`\n\n{_JA_DISCLAIMER}"
            )
        if kind == "dry_run":
            return (
                f"# Stripe 顧客 — ドライラン（レコードは作成されていません）\n\n"
                f"顧客 {who} を作成予定です。\n\n{_JA_DISCLAIMER}"
            )
        if kind == "lookup_hit":
            return f"# Stripe 顧客 — 照会\n\n顧客 {who} が見つかりました。\n\n**顧客ID:** `{cid}`\n\n{_JA_DISCLAIMER}"
        if kind == "lookup_miss":
            return "# Stripe 顧客 — 照会\n\n該当する顧客が見つかりませんでした。\n\n" + _JA_DISCLAIMER
        if kind == "no_credential":
            return (
                "# Stripe 顧客 — 未実行（認証情報なし）\n\n"
                "本環境には Stripe API の認証情報が設定されていないため、"
                "実際の Stripe 呼び出しは行わず、顧客の作成・照会も実施していません。\n\n" + _JA_DISCLAIMER
            )
        return f"# Stripe 顧客 — 未作成\n\nリクエストを処理できませんでした。\n\n{_JA_DISCLAIMER}"
