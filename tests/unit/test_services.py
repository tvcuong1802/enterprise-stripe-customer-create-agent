# CMN-C1-595 — Unit tests: services (intent parser + Stripe client).

import pytest

import src.services.stripe_client as sc_mod
from src.services.intent_parser_service import CustomerIntentParserService, InputParseError
from src.services.stripe_client import StripeClient, StripeClientError


class _Resp:
    def __init__(self, ok=True, status=200, payload=None):
        self.ok = ok
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


class _FakeSecrets:
    def require(self, key):
        assert key == "STRIPE_API_KEY"
        return "sk_test_mock-key-for-testing"


@pytest.fixture(autouse=True)
def _mock_secrets(monkeypatch):
    monkeypatch.setattr(sc_mod, "current_secrets", lambda: _FakeSecrets())


class TestCustomerIntentParserService:
    def setup_method(self):
        self.p = CustomerIntentParserService()

    def test_parse_create_with_all_fields(self):
        out = self.p.parse(
            "Create a Stripe customer for Tanaka Retail KK — email "
            "billing@tanaka-retail.co.jp, name Tanaka Jiro, metadata: account_type=corporate"
        )
        assert out["action"] == "create"
        assert out["email"] == "billing@tanaka-retail.co.jp"
        assert out["name"] == "Tanaka Jiro"  # explicit "name" beats "for <company>"
        assert out["metadata"] == {"account_type": "corporate"}
        assert out["dry_run"] is False

    def test_quoted_name(self):
        out = self.p.parse("Register customer 'Yamada Corp K.K.' with email y@corp.jp")
        assert out["name"] == "Yamada Corp K.K." and out["email"] == "y@corp.jp"

    def test_lookup_by_email(self):
        out = self.p.parse("Look up the Stripe customer with email ops@acme.com")
        assert out["action"] == "lookup" and out["email"] == "ops@acme.com"

    def test_lookup_by_customer_id(self):
        out = self.p.parse("find customer cus_ABC123456")
        assert out["action"] == "lookup" and out["customer_id"] == "cus_ABC123456"

    def test_create_signal_beats_lookup_word(self):
        # "get" is a lookup word but an explicit create wins.
        out = self.p.parse("create and get a customer, email a@b.com")
        assert out["action"] == "create"

    def test_japanese_instruction_nfkc(self):
        out = self.p.parse("メール test@example.co.jp で顧客を作成、氏名 山田太郎、メタデータ: plan=pro")
        assert out["action"] == "create"
        assert out["email"] == "test@example.co.jp"
        assert out["name"] == "山田太郎"
        assert out["metadata"] == {"plan": "pro"}

    def test_explicit_dry_run_only(self):
        assert self.p.parse("create customer email a@b.com, do not create yet")["dry_run"] is True
        # A bare instruction is NOT a dry run.
        assert self.p.parse("create customer email a@b.com")["dry_run"] is False

    def test_name_not_polluted_by_conjunction(self):
        # Regression: a trailing clause must not be written into the customer name.
        out = self.p.parse("Create a customer named Bob Jones and send the invoice email bob@acme.com")
        assert out["name"] == "Bob Jones"

    def test_metadata_only_from_explicit_block(self):
        # Regression: stray key=value substrings must NOT become phantom metadata.
        assert self.p.parse("Create customer plan=premium region=us email a@b.com")["metadata"] == {}
        assert self.p.parse("Create customer email a@b.com, metadata: account_type=corporate plan=pro")["metadata"] == {
            "account_type": "corporate",
            "plan": "pro",
        }

    def test_missing_email_and_id_raises(self):
        with pytest.raises(InputParseError):
            self.p.parse("create a customer for Acme")

    def test_empty_raises(self):
        with pytest.raises(InputParseError):
            self.p.parse("   ")


class TestStripeClientPure:
    def test_egress_guard_rejects_non_stripe(self):
        with pytest.raises(StripeClientError):
            StripeClient(base_url="https://evil.example.com")

    def test_egress_guard_allows_stripe(self):
        assert StripeClient(base_url="https://api.stripe.com")._base_url == "https://api.stripe.com"

    def test_default_base_is_stripe(self):
        assert StripeClient()._base_url == "https://api.stripe.com"

    def test_encode_params_metadata_brackets(self):
        form = StripeClient._encode_params(
            {"email": "a@b.com", "name": "N K.K.", "metadata": {"account_type": "corporate"}}
        )
        assert form == {"email": "a@b.com", "name": "N K.K.", "metadata[account_type]": "corporate"}

    def test_idempotency_key_deterministic(self):
        p = {"email": "a@b.com", "name": "X", "metadata": {"k": "v"}}
        assert StripeClient.idempotency_key(p) == StripeClient.idempotency_key(dict(p))
        assert StripeClient.idempotency_key(p) != StripeClient.idempotency_key({"email": "z@b.com"})

    def test_normalize_customer(self):
        out = StripeClient.normalize_customer({"id": "cus_1", "email": "a@b.com", "name": "X"})
        assert out == {"customer_id": "cus_1", "email": "a@b.com", "name": "X"}


class TestStripeClientVerbsAndPaths:
    """N-17: assert the EXACT REST verb + path for every Stripe operation."""

    def setup_method(self):
        self.client = StripeClient(base_url="https://api.stripe.com")

    def test_create_customer_uses_post_v1_customers(self, monkeypatch):
        calls = {}

        def fake_post(url, headers=None, data=None, timeout=None):
            calls["url"] = url
            calls["headers"] = headers
            calls["data"] = data
            return _Resp(ok=True, payload={"id": "cus_new", "email": "a@b.com", "name": "X"})

        monkeypatch.setattr(sc_mod.requests, "post", fake_post)
        monkeypatch.setattr(sc_mod.requests, "get", lambda *a, **k: pytest.fail("create must not GET"))
        out = self.client.create_customer({"email": "a@b.com", "name": "X", "metadata": {}}, idempotency_key="idem-123")
        assert calls["url"] == "https://api.stripe.com/v1/customers"  # exact path
        assert calls["headers"]["Idempotency-Key"] == "idem-123"  # idempotency header
        assert calls["headers"]["Authorization"].startswith("Bearer ")
        assert calls["data"] == {"email": "a@b.com", "name": "X"}  # form-encoded
        assert out["customer_id"] == "cus_new"

    def test_find_by_email_uses_get_v1_customers_query(self, monkeypatch):
        calls = {}

        def fake_get(url, headers=None, params=None, timeout=None):
            calls["url"] = url
            calls["params"] = params
            return _Resp(ok=True, payload={"data": [{"id": "cus_x", "email": "a@b.com"}]})

        monkeypatch.setattr(sc_mod.requests, "get", fake_get)
        monkeypatch.setattr(sc_mod.requests, "post", lambda *a, **k: pytest.fail("lookup must not POST"))
        out = self.client.find_customer_by_email("a@b.com")
        assert calls["url"] == "https://api.stripe.com/v1/customers"  # list endpoint
        assert calls["params"] == {"email": "a@b.com", "limit": 1}
        assert out["customer_id"] == "cus_x"

    def test_find_by_email_no_match_returns_none(self, monkeypatch):
        monkeypatch.setattr(sc_mod.requests, "get", lambda *a, **k: _Resp(ok=True, payload={"data": []}))
        assert self.client.find_customer_by_email("none@b.com") is None

    def test_get_customer_uses_get_v1_customers_id(self, monkeypatch):
        calls = {}

        def fake_get(url, headers=None, timeout=None):
            calls["url"] = url
            return _Resp(ok=True, payload={"id": "cus_9", "email": "z@b.com"})

        monkeypatch.setattr(sc_mod.requests, "get", fake_get)
        out = self.client.get_customer("cus_9")
        assert calls["url"] == "https://api.stripe.com/v1/customers/cus_9"
        assert out["customer_id"] == "cus_9"

    def test_create_raises_on_http_error(self, monkeypatch):
        monkeypatch.setattr(sc_mod.requests, "post", lambda *a, **k: _Resp(ok=False, status=402))
        with pytest.raises(StripeClientError):
            self.client.create_customer({"email": "a@b.com"}, idempotency_key="k")
