"""Layer 10 - Azure SRE Agent, injected into the SRE network, mapped to the app's and the
external-monitoring RGs.

  ARM (management.azure.com)
    identity     user-assigned; read roles (+ remediation roles at High) on the mapped RGs
    agent        Microsoft.App/agents: knowledge graph = mapped RGs, Azure Monitor incidents,
                 VNet mode: subnet + sandbox egress AzureVNet, private DNS, MCP from the VNet
    connector    Microsoft.App/agents/connectors: MCP over HTTP to the private APIM gateway,
                 subscription key as a flat custom header
  Data plane ({agentEndpoint}, *.azuresre.ai, token audience https://azuresre.dev)
    response plan   v1 incidentPlayground/filters/{id}: which alerts it investigates alone
    skill           v2 extendedAgent/skills/{name}: when and how to call foundry_<tool>
    testconnection  v2 extendedAgent/connectors/{name}/testconnection: the broker's own MCP call

The data plane is public (Entra auth), so those calls skip the SOCKS tunnel."""

from __future__ import annotations

from dataclasses import dataclass

from labkit import arm, gateway, mcp, network, webapp
from labkit.config import Config, Names, suffix

API_AGENT = "2025-05-01-preview"
API_MI = "2023-01-31"
DP_SCOPE = "https://azuresre.dev/.default"
SRE_AGENT_ADMIN = "e79298df-d852-4c6d-84f9-5d13249d1e55"
READ_ROLES = {
    "Reader": "acdd72a7-3385-48ef-bd42-f606fba81ae7",
    "Monitoring Reader": "43d0d8ad-25c7-4714-9337-8ba259a9fe05",
    "Log Analytics Reader": "73c42c96-874c-492b-b04d-ab87d138a893",
    "Monitoring Data Reader": "b0d8363b-8ddd-447d-831f-62ca05bff136",   # PromQL on Azure Monitor workspaces
}


def managed(cfg: Config) -> list[tuple[str, str]]:
    """(RG, location) the agent maps: the customer app and the external-resource monitoring."""
    return [(cfg.app.resourceGroup, cfg.app.location), (cfg.external.resourceGroup, cfg.external.location)]


@dataclass
class SreNames:
    identity: str
    log: str
    appi: str


def names(cfg: Config) -> SreNames:
    p = cfg.namePrefix.lower()
    s = suffix(cfg, network.network(cfg, cfg.sre.network).resourceGroup)
    return SreNames(identity=f"id-{cfg.sre.name}", log=f"log-{p}-sre-{s}", appi=f"appi-{p}-sre-{s}")


def agent_id(c: arm.Client, cfg: Config) -> str:
    rg = network.network(cfg, cfg.sre.network).resourceGroup
    return arm.res_id(c, rg, "Microsoft.App/agents", cfg.sre.name)


def tool(cfg: Config) -> str:
    """The SRE Agent prefixes every MCP tool with '<connector>_'."""
    return f"{cfg.sre.connector}_{cfg.mcp.tool}"


def tools(cfg: Config) -> list[str]:
    """Connector tools: the hosted agent (the skill's tool) and the prompt agent, for side-by-side chats."""
    return [tool(cfg), f"{cfg.sre.connector}_{cfg.promptAgent.tool}"]


def roles(cfg: Config) -> dict[str, str]:
    return {**READ_ROLES, **(cfg.sre.remediationRoles if cfg.sre.accessLevel == "High" else {})}


def _dp(c: arm.Client, method: str, url: str, body: dict | None = None, ok: tuple = (), rbac_wait: int = 0):
    return arm.data(c, method, url, DP_SCOPE, body, ok=ok, rbac_wait=rbac_wait, direct=True)


def _agent_body(c: arm.Client, cfg: Config, location: str, identity: str, appi: dict) -> dict:
    s = cfg.sre
    p = appi.get("properties", {})
    return {
        "location": location,
        "tags": {**cfg.tags, "hidden-link: /app-insights-resource-id": appi.get("id", "<appi-id>")},
        "identity": {"type": "SystemAssigned, UserAssigned", "userAssignedIdentities": {identity: {}}},
        "properties": {
            # The resource-group mapping: what the agent sees, and where its alerts come from.
            "knowledgeGraphConfiguration": {"managedResources": [arm.rg_id(c, g) for g, _ in managed(cfg)],
                                            "identity": identity},
            "actionConfiguration": {"mode": "autonomous", "identity": identity, "accessLevel": s.accessLevel},
            "defaultModel": {"name": "Automatic", "provider": s.modelProvider},
            "incidentManagementConfiguration": {"type": "AzMonitor", "connectionName": "azmonitor"},
            "mcpServers": [],
            "logConfiguration": {"applicationInsightsConfiguration": {
                "appId": p.get("AppId", "<appi-app-id>"), "connectionString": p.get("ConnectionString", "<conn>")}},
            # Azure VNet mode: everything but platform traffic (orchestration, models) leaves
            # through the subnet and resolves with the VNet's DNS, so the peered APIM private
            # endpoint and the linked privatelink.azure-api.net zone apply to the agent.
            "vnetConfiguration": {"subnetResourceId": network.subnet_id(c, cfg, s.network, s.subnet)},
            "sandboxConfiguration": {"egress": {
                "mode": "AzureVNet",
                "vnetConfiguration": {"usePrivateDnsResolution": True},
                "allowHttpMcpServerNetworkAccess": False,   # remote MCP calls from the VNet, not SRE infra
                "allowedRegistries": ["pypi", "npmjs", "nuget-org", "ubuntu"],
                "allowedCodeRepositories": ["Github", "AzureDevOps", "GithubEnterprise"],
                "allowedHosts": []}},
        },
    }


def _response_plan(c: arm.Client, cfg: Config, endpoint: str) -> None:
    """PUT creates; PUT on an existing id returns 409 ('use POST to update')."""
    p = cfg.sre.responsePlan
    body = {"id": p.id, "name": p.id, "priorities": p.priorities, "titleContains": "",
            "titleContainsAny": [webapp.names(cfg).alertPrefix], "handlingAgent": "",
            "agentMode": p.agentMode, "maxAutomatedInvestigationAttempts": p.maxAttempts}
    url = f"{endpoint}/api/v1/incidentPlayground/filters/{p.id}"
    # SRE Agent Administrator was just granted: the data plane can 403 for minutes.
    if _dp(c, "PUT", url, body, ok=(409,), rbac_wait=900).status_code == 409:
        _dp(c, "POST", url, body, rbac_wait=300)


def _skill(c: arm.Client, cfg: Config, endpoint: str) -> None:
    name = cfg.sre.skill
    url = f"{endpoint}/api/v2/extendedAgent/skills/{name}"
    body = {"name": name, "type": "Skill", "tags": [], "properties": {
        "name": name, "tools": [tool(cfg)], "additionalFiles": [],
        "description": f"Ask the lab architect ({tool(cfg)}, a Foundry agent behind the private AI gateway) "
                       "for topology, dependencies and KQL while investigating this lab's resources.",
        "skillContent": (cfg.root / "sre" / f"{name}.md").read_text(encoding="utf-8")}}
    if _dp(c, "PUT", url, body, ok=(409,), rbac_wait=300).status_code == 409:
        _dp(c, "DELETE", url, ok=(404,))
        _dp(c, "PUT", url, body)


def _connector_props(c: arm.Client, cfg: Config, n: Names) -> dict:
    # authType CustomHeaders: every extra flat key is sent as a header. A nested "headers"
    # map is sent as one header literally named "headers" (APIM 401).
    url = mcp.mcp_url(n)
    return {"dataConnectorType": "Mcp", "dataSource": url, "identity": "",
            "extendedProperties": {"type": "http", "endpoint": url, "authType": "CustomHeaders",
                                   "Ocp-Apim-Subscription-Key": mcp.key(c, cfg, n),
                                   "selectedTools": tools(cfg), "toolsVisibleToMetaAgent": tools(cfg)}}


def test_connection(c: arm.Client, cfg: Config, n: Names, endpoint: str) -> bool:
    """The agent's MCP broker connects to the gateway itself: from the agent subnet.
    The body must be the full connector (with the key): the stored one has its secrets
    redacted, and an empty body is rejected as InvalidConnectorType."""
    s = cfg.sre
    body = {"name": s.connector, "type": "AgentConnector", "properties": _connector_props(c, cfg, n)}
    r = _dp(c, "POST", f"{endpoint}/api/v2/extendedAgent/connectors/{s.connector}/testconnection", body,
            ok=(400, 404, 500), rbac_wait=300)
    j = r.json() if r.content else {}
    ok = r.status_code == 200 and j.get("success") is True
    tools = [t.get("name") for t in j.get("tools") or []]
    print(f"    success={ok} tools={tools} {j.get('responseTimeMs', '-')}ms {j.get('errorMessage') or ''}"
          if r.status_code == 200 else f"    {r.text[:600]}")
    return ok


def up(c: arm.Client, cfg: Config, n: Names) -> None:
    s, sn = cfg.sre, names(cfg)
    net = network.network(cfg, s.network)
    rg, loc = net.resourceGroup, net.location
    aid = agent_id(c, cfg)

    print(f"== agent monitoring {sn.log} + {sn.appi}")
    appi = webapp.monitoring(c, cfg, rg, loc, sn.log, sn.appi)

    # The mapped RGs must exist for the role assignments; the app/external layers deploy into them.
    rgs = [g for g, _ in managed(cfg)]
    print(f"== managed resource groups {', '.join(rgs)}")
    for g, gloc in managed(cfg):
        arm.put(c, arm.rg_id(c, g), network.API_RG, {"location": gloc, "tags": cfg.tags})

    print(f"== identity {sn.identity}: {', '.join(roles(cfg))} on {', '.join(rgs)}")
    ident = arm.put(c, arm.res_id(c, rg, "Microsoft.ManagedIdentity/userAssignedIdentities", sn.identity), API_MI,
                    {"location": loc, "tags": cfg.tags})
    ident_id = arm.res_id(c, rg, "Microsoft.ManagedIdentity/userAssignedIdentities", sn.identity)
    principal = ident.get("properties", {}).get("principalId", "<identity-principal>")
    arm.each(lambda gr: arm.role(c, arm.rg_id(c, gr[0]), principal, gr[1]),
             [(g, r) for g in rgs for r in roles(cfg).values()])

    print(f"== SRE Agent {s.name} ({loc}, {s.accessLevel}, {s.modelProvider}) in {s.subnet}")
    agent = arm.put(c, aid, API_AGENT, _agent_body(c, cfg, loc, ident_id, appi))
    me = arm.claims(c)["oid"]
    arm.role(c, aid, me, SRE_AGENT_ADMIN, ptype="User")

    # The Foundry MCP path is optional: without it the agent still watches the mapped RG.
    has_mcp = arm.get(c, f"{gateway.apim_id(c, cfg, n)}/apis/{n.account}-sre-mcp", gateway.API_APIM) is not None
    if has_mcp:
        print(f"== MCP connector {s.connector} -> {mcp.mcp_url(n)} (tools {', '.join(tools(cfg))})")
        arm.put(c, f"{aid}/connectors/{s.connector}", API_AGENT, {"properties": _connector_props(c, cfg, n)})
    else:
        print(f"== MCP connector skipped: mcp layer not deployed (python lab.py up mcp, then up sre again)")
    if c.dry_run:
        return

    endpoint = agent["properties"]["agentEndpoint"]
    print(f"== response plan {s.responsePlan.id} (data plane {endpoint})")
    _response_plan(c, cfg, endpoint)
    if has_mcp:
        print(f"== skill {s.skill} -> {tool(cfg)}")
        _skill(c, cfg, endpoint)
        print(f"== connector test from the agent (MCP to the private gateway)")
        test_connection(c, cfg, n, endpoint)
    print("  portal: https://sre.azure.com")


def show(c: arm.Client, cfg: Config) -> None:
    an = webapp.names(cfg)
    site = arm.get(c, webapp.site_id(c, cfg), webapp.API_WEB)
    if site:
        url, _ = webapp.hosts(site)
        mode = webapp._settings(c, site["id"]).get("FAULT_MODE", "?") if site else "?"
        print(f"  app {an.site}: {site['properties']['state']} {url} FAULT_MODE={mode} "
              f"code={site.get('tags', {}).get(webapp.SHA_TAG, '-')}")
    else:
        print(f"  app {an.site}: not deployed")
    agent = arm.get(c, agent_id(c, cfg), API_AGENT)
    if not agent:
        print(f"  sre agent {cfg.sre.name}: not deployed")
        return
    p = agent["properties"]
    egress = p.get("sandboxConfiguration", {}).get("egress", {}).get("mode", "-")
    managed = [m.rsplit("/", 1)[-1] for m in p.get("knowledgeGraphConfiguration", {}).get("managedResources", [])]
    print(f"  sre agent {cfg.sre.name}: {p.get('provisioningState')} {p.get('runningState', '')} egress={egress} "
          f"access={p.get('actionConfiguration', {}).get('accessLevel')} manages={managed}")
    print(f"    endpoint {p.get('agentEndpoint')}")
    for con in (arm.get(c, f"{agent['id']}/connectors", API_AGENT) or {}).get("value", []):
        p = con["properties"]
        tools = p.get("extendedProperties", {}).get("toolsVisibleToMetaAgent")
        print(f"    connector {con['name'].rsplit('/', 1)[-1]}: {p.get('dataConnectorType')} tools={tools}")
