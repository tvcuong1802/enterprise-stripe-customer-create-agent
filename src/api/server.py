"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# For platform-level routing, AgentGateway calls agent.invoke() directly.

import os
import secrets
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from src.graph.graph import StripeCustomerCreateAgent

app = FastAPI(title="Agent")

agent = StripeCustomerCreateAgent()
agent.compile()
# Namespace = template id (per-agent secret isolation), matching config/agent.yaml.
agent.provision_secrets(secrets_factory(namespace="cmn-c1-595", agent_name="cmn-c1-595"))


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> Any:
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Standalone/STG caller auth (the framework contract "Entry-point exception";
    # the deployment runbook): when INVOKE_AUTH_TOKEN is set on the server
    # environment, callers that no upstream middleware vouched for (still
    # ANONYMOUS) must present it as a Bearer token and run at
    # VERIFIED_EXTERNAL — the nodes declare required_trust_level=
    # VERIFIED_EXTERNAL, so an un-elevated ANONYMOUS caller (the STG smoke)
    # would be refused at the S-1 gate. Middleware-established trust is never
    # demoted. This adapter is the entry-point auth boundary (standalone
    # equivalent of the platform AuthMiddleware) — a deployment-level caller
    # credential, not an agent secret, so ctx.secrets does not apply (no
    # InvocationContext exists before auth).
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input
        # (headers decode as latin-1), which would 500 instead of the generic 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose — do not leak whether the token was absent,
            # malformed, or wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL
    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        # The instruction carries the customer name + email (create payload). The
        # framework S-2 gate masks Title-Case runs / emails in `input`, which would
        # corrupt the fields needed to create the record, so the instruction is
        # mirrored onto the structured `input_context["instruction"]` channel; the
        # pre_process node parses that, falling back to `input`.
        return agent.invoke(req.input, ctx=ctx, input_context={"instruction": req.input})


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent": "cmn-c1-595"}
