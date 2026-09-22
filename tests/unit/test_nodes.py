# CMN-C1-595 — Unit tests: nodes (pre_process / main create / post_process).

import json

from framework.schemas.agent_status import AgentStatus

from src.nodes.customer_create_node import CustomerCreateNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode


class _FakeStripe:
    """In-memory Stripe stub — records whether a write happened."""

    def __init__(self, existing=None):
        self.existing = existing  # dict or None (dedup hit)
        self.created = None  # set when create_customer is called

    @staticmethod
    def credential_available():
        return True  # stub simulates a provisioned env

    def find_customer_by_email(self, email):
        return self.existing

    def get_customer(self, customer_id):
        return {"customer_id": customer_id, "email": "z@b.com", "name": "Z"}

    def create_customer(self, params, idempotency_key):
        self.created = {"params": params, "idem": idempotency_key}
        return {"customer_id": "cus_created", "email": params.get("email", ""), "name": params.get("name", "")}

    @staticmethod
    def idempotency_key(params):
        return "idem-fixed"


def _ic(instruction):
    return {"input_context": {"instruction": instruction}, "error_log": []}


class TestPreProcessNode:
    def setup_method(self):
        self.node = PreProcessNode()

    def test_valid_create(self):
        out = self.node.execute(_ic("create customer, email a@b.com, name Jane Doe"))
        assert out["status"] == AgentStatus.SUCCESS.value
        assert out["validation_error"] == ""
        intent = json.loads(out["parsed_intent"])
        params = json.loads(out["customer_params"])
        assert intent["action"] == "create" and params["email"] == "a@b.com"

    def test_missing_email_graceful_error(self):
        out = self.node.execute(_ic("create a customer for Acme cus_ none"))
        assert out["status"] == AgentStatus.SUCCESS.value
        assert out["validation_error"]  # non-empty refusal

    def test_invalid_email_format(self):
        out = self.node.execute(_ic("create customer email not-an-email name X"))
        # "not-an-email" has no @ -> parser finds no email -> parse error refusal
        assert out["validation_error"]

    def test_japanese_language_detected(self):
        out = self.node.execute(_ic("メール a@b.com で顧客を作成、氏名 山田"))
        assert out["output_language"] == "ja"

    def test_s2_unsafe_input_sets_error(self):
        state = _ic("create customer email a@b.com ../../etc/passwd")
        gated = self.node._extra_security_gate_input(state)
        assert gated["status"] == AgentStatus.ERROR.value
        # execute() then short-circuits to ERROR
        out = self.node.execute(gated)
        assert out["status"] == AgentStatus.ERROR.value

    def test_bilingual_via_input_context(self):
        # Regression: bilingual is reachable via an explicit caller preference.
        out = self.node.execute(
            {
                "input_context": {
                    "instruction": "create customer email a@b.com name X",
                    "output_language": "bilingual",
                },
                "error_log": [],
            }
        )
        assert out["output_language"] == "bilingual"

    def test_reads_input_context_over_user_input(self):
        # user_input is masked/garbage; the real instruction is on input_context.
        state = {
            "input_context": {"instruction": "create customer email a@b.com"},
            "user_input": "[MASKED]",
            "error_log": [],
        }
        out = self.node.execute(state)
        assert out["validation_error"] == ""


class TestCustomerCreateNode:
    def _pre_state(self, action="create", email="a@b.com", name="Jane", dry_run=False, customer_id=""):
        intent = {
            "action": action,
            "email": email,
            "name": name,
            "metadata": {},
            "customer_id": customer_id,
            "dry_run": dry_run,
        }
        return {
            "parsed_intent": json.dumps(intent),
            "customer_params": json.dumps({"email": email, "name": name, "metadata": {}}),
            "validation_error": "",
            "error_log": [],
        }

    def test_create_new_customer_writes(self):
        fake = _FakeStripe(existing=None)
        out = CustomerCreateNode(client=fake).execute(self._pre_state())
        res = json.loads(out["create_result"])
        assert res["created"] is True and res["customer_id"] == "cus_created"
        assert fake.created is not None  # write happened
        assert fake.created["idem"] == "idem-fixed"  # idempotency key passed

    def test_duplicate_returns_existing_no_write(self):
        fake = _FakeStripe(existing={"customer_id": "cus_old", "email": "a@b.com", "name": "Old"})
        out = CustomerCreateNode(client=fake).execute(self._pre_state())
        res = json.loads(out["create_result"])
        assert res["matched_existing"] is True and res["created"] is False
        assert res["customer_id"] == "cus_old"
        assert fake.created is None  # NO duplicate write

    def test_dry_run_no_write(self):
        fake = _FakeStripe(existing=None)
        out = CustomerCreateNode(client=fake).execute(self._pre_state(dry_run=True))
        res = json.loads(out["create_result"])
        assert res["dry_run"] is True and res["created"] is False
        assert fake.created is None

    def test_dry_run_makes_no_stripe_call(self):
        # Regression: dry-run is a pure projection — no lookup GET, no create POST.
        class _NoCall:
            def find_customer_by_email(self, email):
                raise AssertionError("dry-run must not call Stripe")

            def create_customer(self, params, idempotency_key):
                raise AssertionError("dry-run must not write")

            @staticmethod
            def idempotency_key(params):
                return "k"

        out = CustomerCreateNode(client=_NoCall()).execute(self._pre_state(dry_run=True))
        assert json.loads(out["create_result"])["dry_run"] is True

    def test_lookup_by_id(self):
        fake = _FakeStripe()
        out = CustomerCreateNode(client=fake).execute(self._pre_state(action="lookup", customer_id="cus_9"))
        res = json.loads(out["create_result"])
        assert res["lookup"] is True and res["customer_id"] == "cus_9"
        assert fake.created is None

    def test_blocked_on_validation_error(self):
        fake = _FakeStripe(existing=None)
        state = self._pre_state()
        state["validation_error"] = "An email is required to create a customer."
        out = CustomerCreateNode(client=fake).execute(state)
        res = json.loads(out["create_result"])
        assert res["created"] is False and "reason" in res
        assert fake.created is None

    def test_stripe_error_becomes_validation_error(self):
        from src.services.stripe_client import StripeClientError

        class _Boom(_FakeStripe):
            def find_customer_by_email(self, email):
                return None

            def create_customer(self, params, idempotency_key):
                raise StripeClientError("boom 402")

        out = CustomerCreateNode(client=_Boom()).execute(self._pre_state())
        assert out["validation_error"]
        assert out["status"] == AgentStatus.SUCCESS.value  # graceful, not a crash


class TestPostProcessNode:
    def _state(self, create_result, language="en", validation_error=""):
        return {
            "create_result": json.dumps(create_result),
            "output_language": language,
            "validation_error": validation_error,
            "error_log": [],
        }

    def test_created_report_en(self):
        out = PostProcessNode().execute(
            self._state(
                {
                    "created": True,
                    "matched_existing": False,
                    "dry_run": False,
                    "lookup": False,
                    "customer_id": "cus_1",
                    "email": "a@b.com",
                    "name": "Jane",
                }
            )
        )
        assert "Created" in out["formatted_output"] and "cus_1" in out["formatted_output"]

    def test_existing_match_report(self):
        out = PostProcessNode().execute(
            self._state(
                {
                    "created": False,
                    "matched_existing": True,
                    "dry_run": False,
                    "lookup": False,
                    "customer_id": "cus_old",
                    "email": "a@b.com",
                    "name": "Old",
                }
            )
        )
        assert "Existing Match" in out["formatted_output"] and "cus_old" in out["formatted_output"]

    def test_error_report(self):
        out = PostProcessNode().execute(
            self._state(
                {"created": False, "matched_existing": False, "dry_run": False, "lookup": False},
                validation_error="An email is required to create a customer.",
            )
        )
        assert "Not Created" in out["formatted_output"]
        assert "email is required" in out["formatted_output"]

    def test_lookup_transport_error_renders_as_error_not_customer_not_found(self):
        # Regression: a Stripe transport failure during a LOOKUP sets
        # validation_error AND create_result.lookup=True. It must render as an
        # authoritative error carrying the failure reason, NOT a definitive
        # "No customer matched" lookup-miss.
        out = PostProcessNode().execute(
            self._state(
                {
                    "created": False,
                    "matched_existing": False,
                    "dry_run": False,
                    "lookup": True,
                    "reason": "connection reset",
                },
                validation_error="Stripe operation failed: connection reset",
            )
        )
        report = out["formatted_output"]
        assert "Not Created" in report
        assert "Stripe operation failed: connection reset" in report
        assert "No customer matched" not in report

    def test_clean_lookup_miss_still_renders_not_found(self):
        # A genuine lookup-miss does NOT set validation_error and must still
        # render as "No customer matched" (fix must not swallow real misses).
        out = PostProcessNode().execute(
            self._state({"created": False, "matched_existing": False, "dry_run": False, "lookup": True})
        )
        assert "No customer matched" in out["formatted_output"]

    def test_japanese_disclaimer(self):
        out = PostProcessNode().execute(
            self._state(
                {
                    "created": True,
                    "matched_existing": False,
                    "dry_run": False,
                    "lookup": False,
                    "customer_id": "cus_1",
                    "email": "a@b.com",
                    "name": "山田",
                },
                language="ja",
            )
        )
        assert "認証された翻訳ではありません" in out["formatted_output"]
        assert "作成完了" in out["formatted_output"]

    def test_s3_redacts_stripe_key(self):
        node = PostProcessNode()
        leaked = {
            "confirmation_report": ("id cus_1 key sk_live_" + "E" * 24 + " done"),
            "formatted_output": ("key sk_live_" + "E" * 24),
        }
        out = node._extra_security_gate_output(leaked)
        assert ("sk_live_" + "E" * 24) not in out["formatted_output"]
        assert "[REDACTED-CREDENTIAL]" in out["formatted_output"]

    def test_short_metadata_value_not_over_redacted(self):
        """ADVISORY-01 (S-3 Pattern-3 boundary): normal Stripe metadata below the
        40-char opaque-token threshold must pass through the S-3 egress gate
        unredacted. A 32-char hex UUID-like id and a 39-char alphanumeric id are
        realistic metadata values; both are under 40 chars and must survive.
        The `[A-Za-z0-9]{40,}` pattern is intentionally UNCHANGED — tightening it
        risks letting a real long credential through; this test documents that the
        boundary is safe for normal values instead of weakening the pattern."""
        node = PostProcessNode()
        hex_id = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"  # 32 chars
        alnum_id = "abcdefghij0123456789ABCDEFGHIJ012345678"  # 39 chars
        report = f"Created customer cus_1 with metadata order_id={hex_id} " f"and ref={alnum_id} recorded."
        leaked = {"confirmation_report": report, "formatted_output": report}
        out = node._extra_security_gate_output(leaked)
        assert hex_id in out["formatted_output"]
        assert alnum_id in out["formatted_output"]
        assert hex_id in out["confirmation_report"]
        assert alnum_id in out["confirmation_report"]
        assert "[REDACTED-CREDENTIAL]" not in out["formatted_output"]


class TestAnEmptyResultIsNotAlwaysARefusal:
    """Two different situations leave `create_result` empty, and only one is a refusal.

    The write node is routed around when the caller cannot write on this channel — that
    reader gets the notice naming the action that did not happen. But a disambiguation, a
    dry run or a blocked precondition also leave the key empty, and each of those has its
    own report to render. Reporting "you are not permitted to write" to a caller who is
    permitted, and who simply asked a question, tells them to go find an access problem
    that does not exist.
    """

    @staticmethod
    def _state(trust, create_result=""):
        return {
            "create_result": create_result,
            "output_language": "en",
            "validation_error": "",
            "error_log": [],
            "caller_trust_level": trust,
        }

    #: A fragment of the shared notice that does not depend on this repo's action wording.
    #: Asserting the whole sentence would mean copying the node's wording into the test,
    #: where it drifts silently the first time someone rephrases it.
    MARKER = "was not performed."

    def test_a_caller_who_cannot_write_gets_the_refusal_notice(self):
        out = PostProcessNode().execute(self._state("verified_external"))
        assert self.MARKER in out["formatted_output"]

    def test_a_caller_who_CAN_write_is_never_told_they_cannot(self):
        """The half that was unpinned: with `internal` trust an empty result means
        something else happened, and the refusal notice would be simply false."""
        out = PostProcessNode().execute(self._state("internal"))
        assert self.MARKER not in out["formatted_output"]

    def test_a_completed_write_is_not_reported_as_refused(self):
        """The other direction, so the condition cannot collapse to its second half."""
        result = json.dumps(
            {
                "created": True,
                "matched_existing": False,
                "dry_run": False,
                "lookup": False,
                "customer_id": "cus_1",
                "email": "a@b.com",
                "name": "Jane",
            }
        )
        out = PostProcessNode().execute(self._state("verified_external", create_result=result))
        assert self.MARKER not in out["formatted_output"]
        assert "cus_1" in out["formatted_output"]
