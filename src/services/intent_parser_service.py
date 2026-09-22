"""AgentCore Platform v1.0"""

# CustomerIntentParserService — CMN-C1-595
# Deterministic natural-language -> structured Stripe customer-create/lookup intent.
# Handles English and Japanese instructions (NFKC-normalized). Stateless;
# unit-testable in isolation. Constructor-injected into PreProcessNode.
# MUST NOT be instantiated inside execute(). No live LLM call — rule-based so the
# extraction is reproducible and testable.

from __future__ import annotations

import re
from typing import Any
import unicodedata


class InputParseError(ValueError):
    """Raised when the instruction resolves to neither an email nor a customer id."""


# Email (permissive but anchored) — the create key and dedup key.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Stripe customer id (lookup by id).
_CUSTOMER_ID_RE = re.compile(r"\bcus_[A-Za-z0-9]{6,}\b")
# Name: quoted, or after an explicit "name"/"named"/JP "氏名"/"名前" keyword
# (preferred), else a "for <X>" / "customer <X>" fallback.
_QUOTED_NAME_RE = re.compile(r"['\"“”「」]([^'\"“”「」]{1,120})['\"“”「」]")
_NAME_EXPLICIT_RE = re.compile(r"(?:\bname[d]?\b|氏名|名前)\s*[:：]?\s*([^\n,;、。]+)", re.I)
_NAME_FALLBACK_RE = re.compile(r"(?:\bcustomer\s+for\b|\bfor\b|\bcustomer\b)\s*[:：]?\s*([^\n,;、。]+)", re.I)
# metadata: key=value pairs following "metadata:" / "メタデータ:".
_METADATA_BLOCK_RE = re.compile(r"(?:metadata|メタデータ)\s*[:：]\s*(.+)", re.I)
_KV_RE = re.compile(r"([A-Za-z0-9_.\-]+)\s*=\s*([^,;、\s]+)")
# Lookup directive (only classified as lookup when NO create signal is present).
_LOOKUP_RE = re.compile(r"\b(look\s?up|find|search|retrieve|get|show|fetch)\b|" r"照会|検索|検索して|探して|取得", re.I)
_CREATE_RE = re.compile(r"\b(create|add|register|onboard|new customer|make)\b|" r"作成|登録|追加|新規", re.I)
# dry-run must be an explicit directive — never a bare common word like "preview".
_DRY_RUN_RE = re.compile(
    r"\b(dry[- ]?run|dryrun|do not create|don'?t create|without creating|"
    r"no[- ]?write|simulate the create|preview only)\b|作成せず|作成しない|ドライラン",
    re.I,
)
# Corporate suffixes preserved verbatim (kept when trimming a name capture).
_CORP_SUFFIX = (
    "KK",
    "K.K.",
    "Inc",
    "Inc.",
    "Co.",
    "Ltd",
    "Ltd.",
    "LLC",
    "Corp",
    "Corp.",
    "GmbH",
    "株式会社",
    "合同会社",
    "有限会社",
)


class CustomerIntentParserService:
    """Parse an NL customer instruction (EN or JA) into a structured intent.

    Returns ``{"action", "email", "name", "metadata", "customer_id", "dry_run"}``.
    Raises ``InputParseError`` when neither an email nor a Stripe customer id is
    present — the agent never creates or looks up a customer it cannot key.
    """

    def parse(self, text: str) -> dict[str, Any]:
        if not text or not text.strip():
            raise InputParseError("Empty instruction.")

        # NFKC folds full-width latin/digits (common in Japanese input) to ASCII.
        text = unicodedata.normalize("NFKC", text)

        email = self._parse_email(text)
        customer_id = self._parse_customer_id(text)
        if not email and not customer_id:
            raise InputParseError(
                "No email or customer id found — provide the customer's email (to create) or a cus_… id (to look up)."
            )

        has_create = bool(_CREATE_RE.search(text))
        has_lookup = bool(_LOOKUP_RE.search(text))
        # Lookup only when it is a pure lookup directive with no create signal.
        action = "lookup" if (has_lookup and not has_create) else "create"

        return {
            "action": action,
            "email": email,
            "name": self._parse_name(text),
            "metadata": self._parse_metadata(text),
            "customer_id": customer_id,
            "dry_run": bool(_DRY_RUN_RE.search(text)),
        }

    @staticmethod
    def _parse_email(text: str) -> str:
        m = _EMAIL_RE.search(text)
        return m.group(0) if m else ""

    @staticmethod
    def _parse_customer_id(text: str) -> str:
        m = _CUSTOMER_ID_RE.search(text)
        return m.group(0) if m else ""

    @classmethod
    def _parse_name(cls, text: str) -> str:
        q = _QUOTED_NAME_RE.search(text)
        if q:
            return q.group(1).strip()
        # Prefer an explicit "name <X>" keyword over the looser "for/customer <X>".
        for rx in (_NAME_EXPLICIT_RE, _NAME_FALLBACK_RE):
            m = rx.search(text)
            if not m:
                continue
            name = cls._clean_name(m.group(1))
            if name:
                return name
        return ""

    @staticmethod
    def _clean_name(raw: str) -> str:
        name = raw.strip()
        # Trim a trailing "email …"/"metadata …" clause a greedy match ran into.
        name = re.split(r"\b(?:email|e-mail|metadata|メール|メタデータ)\b", name, flags=re.I)[0]
        # Stop at a connector/verb that signals the name has ended (avoid writing a
        # polluted name like "Bob Jones and send the invoice" to Stripe).
        name = re.split(
            r"\s+(?:and|then|with|plus|also|please|send|create|add|register|to|の)\s+",
            name,
            flags=re.I,
        )[0]
        name = name.strip(" -–—:;,")
        # Drop any @-token if the capture accidentally swept the email.
        name = " ".join(w for w in name.split() if "@" not in w).strip()
        # Cap a runaway capture; a customer name is short.
        if len(name.split()) > 6:
            name = " ".join(name.split()[:6])
        return name

    @staticmethod
    def _parse_metadata(text: str) -> dict[str, str]:
        # Only parse metadata from an EXPLICIT "metadata:"/"メタデータ:" block — never
        # from stray key=value substrings elsewhere (which would write phantom
        # metadata, e.g. a name "A=B Corp" or an inline "plan=x", to Stripe).
        block = _METADATA_BLOCK_RE.search(text)
        if not block:
            return {}
        md: dict[str, str] = {}
        for k, v in _KV_RE.findall(block.group(1)):
            # Skip an accidental email=… capture; email is a first-class field.
            if "@" in v:
                continue
            md[k] = v
        return md
