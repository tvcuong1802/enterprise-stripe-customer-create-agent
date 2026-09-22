# CMN-C1-595 — Integration: real end-to-end invoke through the compiled graph.

import pytest

import src.services.stripe_client as sc_mod
from framework.schemas.invocation_context import InvocationContext, TrustLevel
from src.graph.graph import StripeCustomerCreateAgent


class _Resp:
    def __init__(self, payload, ok=True, status=200):
        self._p = payload
        self.ok = ok
        self.status_code = status

    def json(self):
        return self._p


class _FakeSecrets:
    def require(self, key):
        return ("sk_test_" + "E" * 24)

    def get(self, key, default=None):
        # credential_available() probes with get() — the integration env is
        # provisioned, so the Stripe key is present.
        return ("sk_test_" + "E" * 24) if key == "STRIPE_API_KEY" else default


@pytest.fixture
def stripe_env(monkeypatch):
    """Mock the Stripe HTTP + secret boundary; expose a mutable state for the fixture."""
    env = {"existing": [], "post_count": 0}
    monkeypatch.setattr(sc_mod, "current_secrets", lambda: _FakeSecrets())

    def fake_get(url, headers=None, params=None, timeout=None):
        if "/v1/customers/" in url:  # get by id
            return _Resp({"id": url.rsplit("/", 1)[-1], "email": "z@b.com", "name": "Z"})
        return _Resp({"data": env["existing"]})

    def fake_post(url, headers=None, data=None, timeout=None):
        env["post_count"] += 1
        return _Resp({"id": "cus_INT", "email": data.get("email", ""), "name": data.get("name", "")})

    monkeypatch.setattr(sc_mod.requests, "get", fake_get)
    monkeypatch.setattr(sc_mod.requests, "post", fake_post)
    return env


#: A caller who is actually permitted to write. The Marketplace runner grants
#: VERIFIED_EXTERNAL to every user and CustomerCreateNode requires INTERNAL, so a
#: write-path test run at the old default no longer exercises the write -- it exercises
#: the refusal, which has its own test at the bottom of this file.
_WRITER = TrustLevel.INTERNAL


def _invoke(instruction, trust=_WRITER):
    agent = StripeCustomerCreateAgent()
    agent.compile()
    ctx = InvocationContext(session_id="it", caller_trust_level=trust, caller_id="tester")
    return agent.invoke(instruction, ctx=ctx, input_context={"instruction": instruction})


class TestGraphInvoke:
    def test_valid_create_returns_customer_id(self, stripe_env):
        out = _invoke("Create a Stripe customer for Acme, email ops@acme.com, name Jane Doe")
        assert out["status"] == "success"
        assert "cus_INT" in out["output"] and "Created" in out["output"]
        assert stripe_env["post_count"] == 1

    def test_duplicate_email_returns_existing_no_write(self, stripe_env):
        stripe_env["existing"] = [{"id": "cus_DUP", "email": "dup@acme.com", "name": "Dup"}]
        out = _invoke("Create a Stripe customer, email dup@acme.com, name New")
        assert "cus_DUP" in out["output"] and "Existing Match" in out["output"]
        assert stripe_env["post_count"] == 0  # dedup guard prevented a duplicate

    def test_missing_email_blocked(self, stripe_env):
        out = _invoke("Create a customer for SomeCompany")
        assert out["status"] == "success"  # graceful refusal
        assert "Not Created" in out["output"]
        assert stripe_env["post_count"] == 0

    def test_dry_run_no_write(self, stripe_env):
        out = _invoke("Create a Stripe customer, email dry@acme.com, name Dry, do not create yet")
        assert "Dry Run" in out["output"]
        assert stripe_env["post_count"] == 0

    def test_lookup_by_email(self, stripe_env):
        stripe_env["existing"] = [{"id": "cus_LK", "email": "look@acme.com", "name": "Look"}]
        out = _invoke("Look up the Stripe customer with email look@acme.com")
        assert "cus_LK" in out["output"]
        assert stripe_env["post_count"] == 0

    def test_japanese_output_has_disclaimer(self, stripe_env):
        out = _invoke("メール ja@acme.co.jp で顧客を作成、氏名 山田太郎")
        assert "認証された翻訳ではありません" in out["output"]

    def test_anonymous_caller_denied(self, stripe_env):
        out = _invoke("Create a customer, email x@acme.com, name X", trust=TrustLevel.ANONYMOUS)
        assert str(out.get("status")).lower().endswith("error")
        assert stripe_env["post_count"] == 0

    def test_unsafe_input_blocked_end_to_end(self, stripe_env):
        # S-2 hard reject propagates to a fail-closed ERROR state; no write happens.
        out = _invoke("Create a customer email a@b.com name X ../../etc/passwd")
        # A refused MESSAGE is now delivered as success so the reader can act on it,
        # and the envelope names which refusal it was. What this test is really about
        # -- nothing written, nothing leaked -- is unchanged and still asserted here.
        assert out.get("refusal_kind") == "input" or str(out.get("status")).lower().endswith("error")
        assert stripe_env["post_count"] == 0


def test_a_marketplace_caller_cannot_create_a_customer():
    """The ruling, end to end.

    VERIFIED_EXTERNAL is exactly what `run_agent_marketplace()` stamps on every caller, so
    if this run created the record, any Marketplace user could write into the customer's
    Stripe account. SUCCESS, not error: the runner raises on anything else and the caller
    would be shown "agent failed" for a request that was merely not permitted.
    """
    out = _invoke("create a customer for taro@example.co.jp named Taro", trust=TrustLevel.VERIFIED_EXTERNAL)
    assert out["status"] == "success"
    assert not out.get("create_result"), "a customer record was created"
    report = str(out.get("output") or out.get("formatted_output") or "")
    assert "was not performed" in report or "実行していません" in report, report[:300]
    for leak in ("TrustLevel", "VERIFIED_EXTERNAL", "INTERNAL", "token"):
        assert leak not in report, f"the refusal names {leak}"
