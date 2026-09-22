"""What THIS agent's output must not be used for, and how it reads a request.

Kept in its own module so the wording lives beside the agent it describes, while the
MECHANISM stays byte-identical fleet-wide in ``disclaimer.py`` and ``input_intake.py``.
A generic "AI-generated draft" is true of every template here and tells a reader nothing
they can act on; this names the decisions the output must not stand in for.
"""

from __future__ import annotations

SCOPE_EN = "Reference only: a record of a Stripe customer-create request and what it found, from the instruction supplied. It is not a billing decision, not confirmation that any charge will succeed, and not a substitute for your own review of the customer record. Check Stripe itself before relying on anything here."

SCOPE_JA = (
    " "
    "参考情報です。ご指示に基づく Stripe 顧客作成リクエストとその結果の記録であり、請求上の判断でも、決済の成功を保証するものでも、顧客レコードのご確認に代わるものでもありません。ご利用前に Stripe 上でご確認ください。"
)


# Names that stay in Latin script inside a Japanese answer -- they are names, not
# English prose, and a language check that counts them gets this agent wrong.
LANGUAGE_POLICY: dict[str, object] = {
    "identifiers": ("Stripe",),
}


# What the intake call reads out of a free-form request. No `fields` are declared:
# this agent's own extractors already recover what it needs, and a second extractor
# would be a second source of truth. What the call adds is the language of the answer
# -- a request typed in romanised Japanese is entirely Latin, and reading the
# characters gets that reader wrong.
INTAKE_POLICY: dict[str, object] = {
    "languages": ("en", "ja"),
    "default_language": "en",
    "fields": {},
    "capabilities": (
        "Create a Stripe customer from a plain-language instruction",
        "Report an existing customer that already matches, instead of creating a duplicate",
        "Say which fields the instruction did not supply",
    ),
    "examples": (
        {
            "message": "Create a Stripe customer for Acme Ltd, billing email ap@acme.example.",
            "expect": {"language": "en", "fields": {}, "fits": "yes", "suggestion": None},
        },
        {
            "message": "Acme 社の Stripe 顧客を作成してください。請求先メールは ap@acme.example です。",
            "expect": {"language": "ja", "fields": {}, "fits": "yes", "suggestion": None},
        },
        {
            "message": "Refund the last charge for this customer.",
            "expect": {"language": "en", "fields": {}, "fits": "no", "suggestion": 1},
        },
    ),
}

# Re-exported so every call site reads `from src.services.agent_scope import
# resolve_answer_language` -- the wording above is per agent, the mechanism is not, and it
# lives in language_decision.py where one patch fixes every repo.
from src.services.language_decision import (  # noqa: E402
    language_instruction,
    resolve_answer_language,
)

__all__ = [
    "INTAKE_POLICY",
    "LANGUAGE_POLICY",
    "SCOPE_EN",
    "SCOPE_JA",
    "language_instruction",
    "resolve_answer_language",
]
