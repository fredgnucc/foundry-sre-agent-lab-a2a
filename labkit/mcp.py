"""Layer 8 - both Foundry agents as MCP tools on the gateway, for the SRE Agent.

  REST API  <account>-sre         POST /ask {question} -> hosted agent's Responses endpoint (APIM's identity)
  REST API  <account>-sre-prompt  POST /ask {question} -> prompt agent's Responses endpoint (same call)
  MCP API   <account>-sre-mcp     tools <mcp.tool> and <promptAgent.tool> = the two /ask operations,
                                  at <gateway>/<account>-sre-mcp/mcp

The SRE Agent's MCP connector can only send a static header, so a subscription key is
its credential; the key stops at APIM, which calls Foundry with its managed identity."""

from __future__ import annotations

import json

from labkit import agent, arm, gateway, network, tunnel
from labkit.config import Config, Names

API_APIM = gateway.API_APIM
FOUNDRY_AGENT_CONSUMER = "eed3b665-ab3a-47b6-8f48-c9382fb1dad6"


def mcp_url(n: Names) -> str:
    return f"{gateway.gateway_url(n)}/{n.account}-sre-mcp/mcp"


def key(c: arm.Client, cfg: Config, n: Names) -> str:
    return arm.post(c, f"{gateway.apim_id(c, cfg, n)}/subscriptions/{cfg.mcp.subscription}/listSecrets",
                    API_APIM).get("primaryKey", "<key>")


def _rpc(c: arm.Client, url: str, headers: dict, method: str, params: dict | None = None,
         id_: int | None = None, timeout: int = 60) -> dict:
    """One MCP JSON-RPC message over streamable HTTP; the reply may be JSON or SSE.
    The session id from initialize is kept in headers for the following calls."""
    msg = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {}),
           **({"id": id_} if id_ is not None else {})}
    r = arm.data(c, "POST", url, None, msg, headers=headers, timeout=timeout)
    if sid := r.headers.get("Mcp-Session-Id"):
        headers["Mcp-Session-Id"] = sid
    if id_ is None or not r.text.strip():
        return {}
    if r.headers.get("Content-Type", "").startswith("text/event-stream"):
        events = [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:")]
        return next((e for e in events if e.get("id") == id_), events[-1] if events else {})
    return arm.decode(r)


def call_tool(c: arm.Client, cfg: Config, n: Names, question: str, tool: str | None = None) -> str:
    """The SRE Agent's path: initialize, tools/list, tools/call through the private gateway."""
    url = mcp_url(n)
    h = {"Ocp-Apim-Subscription-Key": key(c, cfg, n), "Accept": "application/json, text/event-stream"}
    _rpc(c, url, h, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                   "clientInfo": {"name": "lab.py", "version": "1"}}, 1)
    _rpc(c, url, h, "notifications/initialized")
    tools = _rpc(c, url, h, "tools/list", {}, 2).get("result", {}).get("tools", [])
    print(f"  tools: {[t['name'] for t in tools]}")
    res = _rpc(c, url, h, "tools/call", {"name": tool or cfg.mcp.tool, "arguments": {"question": question}}, 3, timeout=300)
    if "error" in res:
        raise arm.ArmError(f"tools/call: {json.dumps(res['error'])[:1500]}")
    return "".join(p.get("text", "") for p in res.get("result", {}).get("content", []))


def up(c: arm.Client, cfg: Config, n: Names) -> None:
    m = cfg.mcp
    rg = network.network(cfg, cfg.foundry.network).resourceGroup
    aid = gateway.apim_id(c, cfg, n)
    principal = (arm.get(c, aid, API_APIM) or {}).get("identity", {}).get("principalId", "<apim-principal>")

    # The bridge between the two parts: the SRE Agent reaches the gateway's private endpoint.
    network.link(c, cfg)

    print(f"== APIM identity: Foundry Agent Consumer on project {cfg.foundry.project}")
    arm.role(c, arm.res_id(c, rg, "Microsoft.CognitiveServices/accounts", f"{n.account}/projects/{cfg.foundry.project}"),
             principal, FOUNDRY_AGENT_CONSUMER)

    hosted, prompt = cfg.agent.name, cfg.promptAgent.name
    rests = {f"{n.account}-sre": hosted, f"{n.account}-sre-prompt": prompt}
    for api, name in rests.items():
        print(f"== REST API /{api} (POST /ask -> agent {name})")
        arm.put(c, f"{aid}/apis/{api}", API_APIM, {"properties": {
            "displayName": api, "path": api, "protocols": ["https"],
            "subscriptionRequired": True, "format": "openapi+json",
            "value": gateway.template("openapi-ask.json", AGENT=name)}})
        arm.put(c, f"{aid}/apis/{api}/policies/policy", API_APIM, {"properties": {"format": "rawxml", "value": gateway.template(
            "policy-mcp.xml", PROJECT_ENDPOINT=agent.project_url(cfg, n), AGENT=name)}})

    topics = ("about this lab: networks, subnets, private endpoints, Foundry, the AI gateway and its APIs, agents, "
              "traffic flows, KQL and teardown caveats.")
    mcp = f"{aid}/apis/{n.account}-sre-mcp"
    print(f"== MCP API /{n.account}-sre-mcp (tools {m.tool}, {cfg.promptAgent.tool})")
    arm.put(c, mcp, API_APIM, {"properties": {
        "type": "mcp", "displayName": f"{n.account}-sre-mcp", "path": f"{n.account}-sre-mcp", "protocols": ["https"],
        "description": f"Foundry agents {hosted} (hosted) and {prompt} (prompt), exposed as MCP tools for Azure SRE Agent.",
        "subscriptionRequired": True,
        "mcpTools": [
            {"name": m.tool, "operationId": f"{aid}/apis/{n.account}-sre/operations/ask", "description": (
                f"Ask the lab architect, hosted agent {hosted} (code, own container, knowledge base), {topics}")},
            {"name": cfg.promptAgent.tool, "operationId": f"{aid}/apis/{n.account}-sre-prompt/operations/ask",
             "description": (f"Ask the lab architect, prompt agent {prompt} (no code, Foundry-run, search index), {topics}")}]}})

    print(f"== product {m.product} + subscription {m.subscription} (the connector's key)")
    product = f"{aid}/products/{m.product}"
    arm.put(c, product, API_APIM, {"properties": {
        "displayName": "SRE Agent MCP", "description": "Foundry agents exposed to Azure SRE Agent over MCP.",
        "subscriptionRequired": True, "approvalRequired": False, "state": "published"}})
    arm.each(lambda api: arm.call(c, "PUT", f"{product}/apis/{api}", API_APIM, {}),
             [*rests, f"{n.account}-sre-mcp"])
    arm.put(c, f"{aid}/subscriptions/{m.subscription}", API_APIM, {"properties": {
        "displayName": "Azure SRE Agent", "scope": product, "state": "active"}})

    b = cfg.gateway.logBodies
    print(f"== body logging on {', '.join(rests)} and -sre-mcp ({b or 'off'} bytes; MCP response not logged)")
    arm.parallel(*[lambda api=api: gateway.api_diagnostics(c, aid, f"{aid}/apis/{api}", body=b) for api in rests],
                 lambda: gateway.api_diagnostics(c, aid, mcp, body=b, response_body=False))

    if c.dry_run:
        return
    tunnel.up(c, cfg, n)
    q = "Which private endpoint fronts the AI gateway, and what is its IP?"
    for tool in (m.tool, cfg.promptAgent.tool):
        print(f"== MCP test through the private gateway, {tool}: {q}")
        print(f"  answer: {call_tool(c, cfg, n, q, tool)}")
    print(f"  SRE Agent connector: {mcp_url(n)}  header Ocp-Apim-Subscription-Key "
          f"(python lab.py mcp-key prints it)")
