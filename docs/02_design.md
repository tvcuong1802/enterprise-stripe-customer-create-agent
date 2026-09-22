# CMN-C1-595 — Design Specification

**Template ID:** CMN-C1-595
**Name:** Enterprise Stripe Customer Create Agent
**Category:** Cat 1 (single technical capability)
**Industry:** CMN (cross-industry)
**Scaffold issue:** #1612

---

## 1. Architecture — L1 Base

**L1 Base:** `AgentBaseGraph` (Level-1 direct inheritance). The agent class
`StripeCustomerCreateAgent` inherits `AgentBaseGraph` directly — there is **no
Level-2 base class**. `ToolCallingAgent` is a conceptual reference to the
SaaS-operation / NL-to-API pattern only, never an inheritance target
(2026-05-18 L2-abolition policy).

The template follows the AgentCore three-layer separation:

| Layer | Principle | This template |
|-------|-----------|---------------|
| State | Composition (flat TypedDict) | `State(AgentState)` — primitives + JSON-serialized strings only |
| Node | Inheritance (Template Method) | `FunctionNode` subclasses override `execute(self, state) -> dict` |
| Graph | Composition (Builder) | `StripeCustomerCreateAgent(AgentBaseGraph)` registers 3 slots |

The single capability delivered is **customer provisioning**: turn a
natural-language instruction into either a newly created Stripe customer or a
returned existing match, behind an extract → validate → dedup → idempotent-create
→ audit envelope. Lookup is the **internal dedup/idempotency guard** of the
create flow, not a standalone user-facing capability (per PM/moderator resolution
on #1612).

---

## 2. Node design — AgentBaseGraph 3-slot fold

The default `AgentBaseGraph` pipeline is used unchanged (no `add_edges()`
override):

```
START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
```

The 5 logical proposal stages fold into the 3 slots:

| Slot | Node class | Logical stages | Responsibility |
|------|-----------|----------------|----------------|
| `pre_process` | `PreProcessNode` | IntentParse + FieldExtract + field validation | S-2 input gate; parse create/lookup intent; extract email/name/metadata; NFKC; validate required fields + email format → `validation_error` |
| `main` | `CustomerCreateNode` | ValidationCheck (dedup lookup) + CustomerCreate | The write capability: dedup lookup by email → return existing on match, else idempotent `POST /v1/customers`; lookup intent → GET; dry-run guard |
| `post_process` | `PostProcessNode` | ConfirmationGenerate | Render EN/JA/bilingual confirmation; S-3 egress redaction; S-4 audit; surface `formatted_output` |

`main` is a **single `FunctionNode`** (Cat 1 — no inner graph, no `GraphNode`,
no state bridge). Field-format validation lives in `pre_process` (input
well-formedness); the dedup lookup that gates the write lives in `main` alongside
the create, because both operate against the Stripe API and constitute the write
decision.

---

## 3. State contract

`State` extends `AgentState`. All shared fields (`user_input`, `validated_input`,
`status`, `session_id`, `node_history`, `error_log`, `formatted_output`, `result`,
`caller_trust_level`, `hitl_*`, etc.) are inherited and NOT redeclared. Compound
values are stored as **JSON-serialized strings** so the msgpack checkpoint stays
safe (no raw `dict`/`list` fields, no Pydantic, no credentials, no
`InvocationContext`).

| Field | Type | Written by | Contents |
|-------|------|-----------|----------|
| `parsed_intent` | `str` (JSON) | PreProcessNode | `{"action":"create"\|"lookup","email","name","metadata":{...},"customer_id","dry_run":bool}` |
| `customer_params` | `str` (JSON) | PreProcessNode | validated create params `{"email","name","metadata":{...}}` |
| `validation_error` | `str` | PreProcessNode / CustomerCreateNode | non-empty ⇒ graceful business error (status stays SUCCESS; post renders refusal) |
| `output_language` | `str` | PreProcessNode | `"en"` \| `"ja"` \| `"bilingual"` (default `"en"`) |
| `create_result` | `str` (JSON) | CustomerCreateNode | `{"created":bool,"matched_existing":bool,"dry_run":bool,"lookup":bool,"customer_id","email","name"}` |
| `confirmation_report` | `str` | PostProcessNode | final Markdown report; mirrored into inherited `formatted_output` (surfaced by `get_output()`) |

Defaults: string fields `""`; `output_language` `"en"`. The outer graph does
**not** override `get_output()` — the default surfaces `formatted_output`.

---

## 4. Services (constructor-injected, stateless)

Both services are built once in `register_nodes()` and injected into node
constructors — never instantiated inside `execute()`.

### 4.1 `CustomerIntentParserService`
Deterministic NL → structured intent (no live LLM call; rule-based and fully
unit-testable). NFKC-normalizes input, handles EN and JA.

`parse(text) -> {"action","email","name","metadata":{...},"customer_id","dry_run"}`
- `action`: `"lookup"` when the instruction is a pure lookup/find directive **and**
  carries no create signal, else `"create"` (default).
- `email`: first RFC-ish email match.
- `name`: quoted name, or the value after `name`/`for`/`名前`/`氏名`; corporate
  suffixes (KK, 株式会社, Inc, Co., Ltd) preserved verbatim.
- `metadata`: `key=value` pairs (after `metadata:` / `メタデータ:`).
- `customer_id`: a `cus_...` token when present (lookup by id).
- `dry_run`: only on an explicit directive (`dry run`, `do not create`,
  `作成せず`) — never a bare common word.
- Raises `InputParseError` when neither an email nor a customer id can be found.

### 4.2 `StripeClient`
Thin Stripe REST client. **S-3 egress guard**: only a `*.stripe.com` host is
permitted, so a misconfiguration cannot exfiltrate the key off-platform. The
API key is fetched **per call** via `current_secrets().require("STRIPE_API_KEY")`
— never cached on the instance, never read from state or `os.environ`.

| Method | Verb + path | Notes |
|--------|-------------|-------|
| `find_customer_by_email(email)` | `GET /v1/customers?email=<email>&limit=1` | dedup lookup; returns first match dict or `None` |
| `get_customer(customer_id)` | `GET /v1/customers/{id}` | lookup by id |
| `create_customer(params, idempotency_key)` | `POST /v1/customers` | form-encoded body; `Idempotency-Key` header; `Authorization: Bearer <key>` |

Base host: `https://api.stripe.com`. The idempotency key is derived
deterministically from the validated request (sha256 of email + name + sorted
metadata) so a retried call returns the same customer instead of duplicating it.

---

## 5. Five-layer security mapping

| Layer | Where | Implementation |
|-------|-------|----------------|
| S-1 Trust gate | every node | `required_trust_level: ClassVar[TrustLevel] = VERIFIED_EXTERNAL` declared in each node class; enforced by `BaseNode.__call__` before `execute()`. A create is a PII write. |
| S-2 Input gate | `PreProcessNode._extra_security_gate_input` | reject unsafe path/control chars + oversized input (status ERROR, no raise). Framework `@final _security_gate_input` also PII-scans `user_input` first. |
| S-3 Output gate | `PostProcessNode._extra_security_gate_output` | redact any Stripe key shape (`sk_live_`, `sk_test_`, `rk_`, `Bearer …`, long opaque tokens) from the report. Framework `@final _security_gate_output` credential-scans all string outputs. Graph-level gates are dead on this backbone — S-3 lives on the node. |
| S-4 Audit | every node's `execute()` | `emit_trace_event("<domain_verb>", {...}, state)` — one domain event per node (`intent_parsed`, `customer_created`, `customer_matched_existing`, `report_compiled`, …). Never `node_start/complete/error` (framework owns those). Payloads carry counts/flags/ids only — never raw PII or the key. |
| S-5 Credential scan | CI `gate-credential-scan` | no credentials in `src/`; tests use clearly-labelled mock values. |

**S-2 name-masking note (verified on wheel 1.0.0):** the framework's
`_security_gate_input` masks Title-Case runs in `user_input` as a high-recall
"name" heuristic — this corrupts the customer name and email needed to create the
record. So `server.py` mirrors the instruction onto the structured
`input_context["instruction"]` channel (not in the PII-scan field set), and
`PreProcessNode` parses that channel (falling back to `user_input`). S-2's SAFETY
role (unsafe-char/size reject) and S-3 egress remain fully in force; the customer
data is the caller's own supplied input, echoed back only as ID + minimal
confirmation, never leaked beyond the caller.

**Secrets:** `requires.secrets: ["STRIPE_API_KEY"]` declared in `config/agent.yaml`;
retrieved via `current_secrets().require()`. Never stored in State (State is
checkpointed to the DB).

---

## 6. Edge cases

| Input | Behaviour |
|-------|-----------|
| No email and no customer id | `validation_error` "email required to create a customer" → refusal report, no write (graceful, status SUCCESS) |
| Malformed email | `validation_error` "invalid email format" → refusal report |
| Duplicate email (existing customer) | return the existing customer (`matched_existing`), **no** second record created |
| Retried identical create | idempotency key → Stripe returns the same customer |
| `dry run` directive | projected create, no write |
| Unsafe chars / oversized input | S-2 hard reject → status ERROR |
| Out-of-scope (update/delete/subscription) | not parsed as create → refusal (no fabricated write) |
| Stripe API error | caught → `validation_error` → refusal report (no partial customer) |

No fabrication: the agent never invents a customer ID; every returned ID comes
from a real Stripe response.

---

## 7. HITL / memory

`hitl.enabled` is **absent** (no HITL). A create is reversible (a customer can be
deleted) and dedup-guarded, so no mandatory human gate is imposed. `memory_enabled`
is not required. High-assurance deployments may add a preview-before-create step
externally; the dedup guard and idempotency key are always on.

---

## 8. Japanese output

When `output_language` is `ja` or `bilingual`, the confirmation is rendered in
business Japanese with controlled terminology (用語統制) and keigo (です／ます),
carrying the machine-generation disclaimer **認証された翻訳ではありません**.
Corporate names (法人名), addresses, metadata, and the customer ID are preserved
verbatim — only the framing is localized. Japanese input is NFKC-normalized (so
full-width digits/latin fold to ASCII) before parsing.

---

## 9. Disclaimer

This agent creates and looks up Stripe customer records the user instructs; it is
an operational billing-ops tool, **not financial, tax, accounting, or legal
advice**. Customer email and name are personal data handled under Japan's APPI —
the operation is trust-gated and audit-logged, but lawful basis and overall APPI
compliance remain the customer's responsibility. Where output is localized into
Japanese it is terminology-standardized for readability, **not a certified
translation (認証された翻訳ではありません)**.

---

## 10. Wheel-verified contract facts (agenticstar-agentcore==1.0.0)

- `BaseNode.__call__(self, state)` — state only; runs S-1 → node_start → S-2 → `execute()` → S-3 → node_complete. Never overridden.
- `execute(self, state) -> dict` — no `config` param; returns a partial state update.
- `FunctionNode._security_gate_input/_output` are `@final` (`__init_subclass__`-enforced) — extended via `_extra_security_gate_input/_output`, never overridden.
- `AgentBaseGraph` has **no** `_extra_security_gate_output` → a graph-level gate is dead; S-2/S-3 live on nodes.
- `AgentStatus` values are strings (`"success"`, `"error"`); `TrustLevel` is a str-Enum (`ANONYMOUS`/`VERIFIED_EXTERNAL`/`INTERNAL`).
- Secrets: `current_secrets().require()`; audit: `emit_trace_event()` from `shared.utils.audit_logger`.

## Known constraint — write modes on the Marketplace one-shot Pod entry point (2026-09-14)

`run_agent_marketplace()` stamps every caller `VERIFIED_EXTERNAL` and offers no surface to
raise it. CoE ruled (an internal ruling, the framework contract; tracked at (internal reference removed) / #251) that write
modes are **out of scope for this entry point**, and that lowering a template's own write
authorization to `VERIFIED_EXTERNAL` so they become reachable is **prohibited** — at that
level any Marketplace user could create records in the customer's Stripe account.

**Present state.** The template deploys and registers normally. `CustomerCreateNode`
declares `required_trust_level = TrustLevel.INTERNAL`, and `Graph.add_edges()` routes
`pre_process → post_process` for a caller who cannot reach that level. The caller is told,
in their own language, which action was not performed and where it can be performed — at
`status: success`, because the runner raises on anything else. Read-only, which is what
Stage ⑤ accepts. The supported deployment for the write path is the standalone HTTP entry
point (`src/api/server.py`), whose adapter maps an authenticated runner credential to
`INTERNAL`.

A **present-state constraint, not a permanent exclusion** — (internal reference removed) is open.

