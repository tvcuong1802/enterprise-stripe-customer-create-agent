# Test Specification — CMN-C1-595

## Test Strategy
- Test types: Unit (services + nodes + framework compliance), Integration (real
  end-to-end invoke through the compiled graph), Proof-of-Boundary.
- All tests run on the real `agenticstar-agentcore==1.0.0` wheel (CI `run-tests`
  installs it from the package registry). External Stripe HTTP is mocked at the
  `requests` boundary; the secret provider is mocked — no live API key is used.
- **Total: 63 tests, all passing on the wheel.**

| Suite | File | Count |
|-------|------|-------|
| Services (parser + Stripe client, incl. exact verb/path) | `tests/unit/test_services.py` | 22 |
| Nodes (pre_process / main create / post_process) | `tests/unit/test_nodes.py` | 19 |
| Framework compliance (TC-01..08) | `tests/unit/test_framework_compliance.py` | 11 |
| Integration (real invoke) | `tests/integration/test_graph.py` | 8 |
| Proof-of-boundary | `tests/proof_of_boundary/` | 3 |

## Framework Compliance Tests (Mandatory)

| TC-ID | Test | Expected Result | Where |
|-------|------|----------------|-------|
| TC-01 | State is a flat TypedDict extending AgentState; agent fields all `str` (JSON) | No Pydantic/dataclass; compound values JSON-serialized | `test_framework_compliance.py::TestTC01StateContract` |
| TC-02 | S-2 input gate rejects unsafe input | status ERROR (no raise) | `TestTC02InputSecurityGate` |
| TC-03 | No credential literal in `src/` | 0 matches (mirrors CI `gate-credential-scan`) | `TestTC03NoCredentialsInSrc` |
| TC-04 | InvocationContext via `from_state`; never stored in State | No InvocationContext-typed / `invocation_context` field | `TestTC04InvocationContextViaConfigurable` |
| TC-05 | S-4: each node emits ≥1 domain `emit_trace_event()` in `execute()` | Event emitted on every path | `TestTC05AuditLogging` |
| TC-06 | S-2 `_security_gate_input` is `@final` (non-bypassable) | Overriding raises `TypeError` at class def | `TestTC06TC07FinalGatesNonBypassable` |
| TC-07 | S-3 `_security_gate_output` is `@final` (non-bypassable) | Overriding raises `TypeError` at class def | `TestTC06TC07FinalGatesNonBypassable` |
| TC-08 | `required_trust_level` enforced (S-1) | Anonymous caller → ERROR; all nodes VERIFIED_EXTERNAL | `TestTC08TrustLevelEnforced` |

## Proof-of-Boundary Tests (Mandatory)

| PB-ID | Boundary | Expected Result | Where |
|-------|----------|----------------|-------|
| PB-2/PB-5 | State serialization / checkpoint safety | State fields primitives only; no Pydantic/InvocationContext/credential fields | `test_state_safety.py` |
| PB-4 | Import isolation | AST scan: 0 Level-0 imports | `test_import_isolation.py` |
| PB-6 | Invoke execution order | For every node: S-1 → node_start → S-2 `_security_gate_input` → `execute()` → S-3 `_security_gate_output` → node_complete | `test_pb_invoke_order.py` |

> PB-3 (real external service) is exercised indirectly: the integration suite drives
> the full graph against a mocked Stripe HTTP boundary (create / dedup / lookup /
> dry-run / error), asserting the exact REST verb + path (`POST /v1/customers`,
> `GET /v1/customers?email=`, `GET /v1/customers/{id}`) in `test_services.py`. A live
> Stripe credential is out of scope for CI.

## Business Logic Tests (in `test_nodes.py` + `test_graph.py`)

| ID | Behaviour | Input | Expected |
|----|-----------|-------|----------|
| BL-01 | Valid create | "Create … email a@b.com, name Jane" | `POST /v1/customers`, returns customer ID |
| BL-02 | Dedup guard | create with an email that already exists | existing customer returned, **0 writes** |
| BL-03 | Idempotency | create | `Idempotency-Key` header derived from the request |
| BL-04 | Missing email | "Create a customer for Acme" | graceful refusal, no write |
| BL-05 | Dry run | "… do not create yet" | projected create, no write |
| BL-06 | Lookup by email/id | "Look up … email …" / "find cus_…" | resolves existing, no write |
| BL-07 | Japanese output | JP instruction | keigo + 用語統制 + 認証された翻訳ではありません |
| BL-08 | S-1 denial | anonymous caller | ERROR, no write |
| BL-09 | S-3 egress | report containing a key shape | redacted to `[REDACTED-CREDENTIAL]` |

| Caller without write scope, no create_result | `caller_trust_level: verified_external` | Notice names the action that did not happen | `test_nodes.py::…::test_a_caller_who_cannot_write_gets_the_refusal_notice` |
| Caller WITH write scope, no create_result | `caller_trust_level: internal` | No refusal notice — an empty result here means something else happened, and the notice would be false | `test_nodes.py::…::test_a_caller_who_CAN_write_is_never_told_they_cannot` |
| Completed write (control) | result present, caller without write scope | No refusal notice; the result is reported | `test_nodes.py::…::test_a_completed_write_is_not_reported_as_refused` |

## Test Execution Summary
- Environment: real `agenticstar-agentcore==1.0.3` wheel (read from pip in the measuring
  environment, not copied from a pin), `mypy==1.10.0` — this repo's own CI pin, which is
  the version whose verdict CI reports — Python 3.11. Measured 2026-09-15.
- Total: **147 passed / 0 failed / 1 skipped** (a vendor-only import guard, under the
  registry wheel).
- `ruff check src/ tests/` clean · `ruff format --check src/` clean · `mypy src/` clean.
- The write-gate condition was mutation-tested: 3 mutants, 3 killed. Before this round the
  second half was unpinned — dropping `caller_may_write` left the suite green while every
  empty result read as a refusal.

## Refused input — what the sender receives (shared contract, 2026-09-15)

Measured across the fleet with a real model: a message the framework's S-2 gate declined
came back as `status: error` carrying the generic line "No answer could be produced for
this request." `normalize_terminal_output()` raises on any status but SUCCESS, so the
runner discarded the whole envelope and the sender read **"agent failed"** — with nothing
to act on, and no reason to send anything different next time.

| Situation | What is returned | Why |
|---|---|---|
| S-2 declined the MESSAGE | `status: success`, `refusal_kind: "input"`, a sentence naming what to change, plus the trailer | The sender is legitimate and holds something they can fix; they only learn that if the reply reaches them |
| The agent has its own refusal wording | That wording, not the shared sentence | "The shipment could not be classified" says which step stopped; the generic line does not |
| S-1 denied the CALLER | `status: error`, `refusal_kind: "trust"`, the refusal and nothing else | A caller not permitted to invoke the agent must not be told what it is for |
| S-3 blocked the agent's OWN output | unchanged — `status: error` | The agent produced something its output gate would not pass. The sender can do nothing with that, and must not be invited to retry |
| The agent genuinely broke | unchanged — `status: error` | The one signal that says this is an operations problem |

Nothing downstream reads `status` to detect a refusal any more: the envelope names the
refusal in `refusal_kind`. A contract that could only be read by the symptom it was fixing
was not a contract.

Enforced by `tests/unit/test_disclaimer_always_present.py` —
`test_a_refused_MESSAGE_is_delivered_and_says_what_to_change`,
`test_a_REAL_failure_is_still_an_error` (its control), and
`test_the_gate_token_is_matched_as_a_whole_token`.
