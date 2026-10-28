"""Optional: publish the hosted agent to Microsoft Teams and Microsoft 365 Copilot.

The project has public network access off, so the portal's Publish button can't be used.
REST flow (https://learn.microsoft.com/azure/foundry/agents/how-to/publish-copilot-virtual-network):

  1. GET  {project}/agents/{a}                      -> instance_identity.client_id (the bot's app ID)
  2. PUT  Microsoft.BotService/botServices/{bot}    -> Azure Bot (F0, SingleTenant, public access off)
     PUT  .../channels/MsTeamsChannel                  forwarding to the agent's Activity Protocol route
  3. PATCH {project}/agents/{a} (merge-patch)       -> protocol "activity" with enable_m365_public_endpoint
     plus a BotService* authorization scheme. Only that route opens, and only to Bot Service /
     M365 IP ranges with a valid Bot Service token; responses, Entra and all other APIs stay private.
  4. POST {project}/agents/{a}/microsoft365/publish -> Teams app in the agent store

Not part of `up all`: it opens a public route. Calls 1, 3 and 4 go through the tunnel."""

from __future__ import annotations

from labkit import agent, arm, network, tunnel
from labkit.config import Config, Names

API_BOT = "2022-09-15"
ACTIVITY_API = "2025-11-15-preview"   # what the portal sets on its bots; Foundry rejects the call without it
SCHEMES = {"Shared": "BotServiceRbac", "Tenant": "BotServiceTenant"}


def bot_name(cfg: Config, n: Names) -> str:
    return f"bot-{cfg.namePrefix}-{cfg.agent.name}-{n.suffix}"[:42]   # Bot Service max length


def bot_id(c: arm.Client, cfg: Config, n: Names) -> str:
    rg = network.network(cfg, cfg.foundry.network).resourceGroup
    return arm.res_id(c, rg, "Microsoft.BotService/botServices", bot_name(cfg, n))


def _get_agent(c: arm.Client, cfg: Config, n: Names) -> dict:
    return arm.decode(arm.data(c, "GET", agent._url(cfg, n, f"/agents/{cfg.agent.name}"), agent.SCOPE))


def _bot(c: arm.Client, cfg: Config, n: Names, client_id: str, display: str) -> str:
    endpoint = (f"{agent.project_url(cfg, n)}/agents/{cfg.agent.name}/endpoint/protocols/activityprotocol"
                f"?api-version={ACTIVITY_API}")
    arm.call(c, "POST", f"/subscriptions/{c.subscription}/providers/Microsoft.BotService/register", "2021-04-01")
    bid = bot_id(c, cfg, n)
    arm.put(c, bid, API_BOT, {"location": "global", "kind": "azurebot", "tags": cfg.tags, "sku": {"name": "F0"},
                              "properties": {"displayName": display, "endpoint": endpoint, "msaAppId": client_id,
                                             "msaAppTenantId": arm.claims(c)["tid"], "msaAppType": "SingleTenant",
                                             "publicNetworkAccess": "Disabled"}})
    arm.put(c, f"{bid}/channels/MsTeamsChannel", API_BOT,
            {"location": "global", "properties": {"channelName": "MsTeamsChannel"}})
    print(f"    bot -> {endpoint}")
    return bid


def _enable_activity(c: arm.Client, cfg: Config, n: Names, a: dict, scheme: str) -> None:
    """Merge-patch replaces both lists: keep what the endpoint has (responses, Entra)."""
    ep = a.get("agent_endpoint") or {}
    protocols = {**(ep.get("protocol_configuration") or {"responses": {}}),
                 "activity": {"enable_m365_public_endpoint": True}}
    schemes = [s for s in ep.get("authorization_schemes") or [{"type": "Entra"}]
               if not s.get("type", "").startswith("BotService")] + [{"type": scheme}]
    arm.data(c, "PATCH", agent._url(cfg, n, f"/agents/{cfg.agent.name}"), agent.SCOPE,
             {"agent_endpoint": {"protocol_configuration": protocols, "authorization_schemes": schemes}},
             headers={"Content-Type": "application/merge-patch+json"})
    print(f"    protocols={sorted(protocols)} schemes={[s['type'] for s in schemes]}")


def publish(c: arm.Client, cfg: Config, n: Names, scope: str, version: str, display: str | None) -> None:
    name = cfg.agent.name
    display = display or name
    tunnel.up(c, cfg, n)

    print(f"== 1/4 agent identity ({name})")
    a = _get_agent(c, cfg, n)
    client_id = a["instance_identity"]["client_id"]
    print(f"    client_id {client_id}")

    print(f"== 2/4 Azure Bot {bot_name(cfg, n)} + Teams channel")
    bid = _bot(c, cfg, n, client_id, display)

    print(f"== 3/4 Activity Protocol, Microsoft 365 route only ({SCHEMES[scope]})")
    _enable_activity(c, cfg, n, a, SCHEMES[scope])

    print(f"== 4/4 publish to Microsoft 365 (scope {scope}, app {version})")
    desc = f"Architect of the {cfg.namePrefix} lab: private Foundry hosted agent, knows the lab's topology."
    r = arm.data(c, "POST", agent._url(cfg, n, f"/agents/{name}/microsoft365/publish"), agent.SCOPE, {
        "agentDisplayName": display, "botServiceArmId": bid, "publishScope": scope,
        "publishAsAutopilot": False, "appVersion": version,
        "shortDescription": desc[:80], "fullDescription": desc, "developerName": "foundry-to-sre-agent lab",
        "developerWebsiteUrl": "https://azure.microsoft.com", "privacyUrl": "https://privacy.microsoft.com",
        "termsOfUseUrl": "https://www.microsoft.com/legal/terms-of-use"}, ok=(400, 409), timeout=300)
    if r.status_code == 409 or "already exists" in r.text:
        print(f"    app {version} already published; bot and endpoint updated. Bump --app-version to change metadata.")
        return
    if r.status_code >= 400:
        raise arm.ArmError(f"publish -> {r.status_code}\n{r.text[:1500]}")
    print(f"    {r.text[:600]}")
    where = "Your agents" if scope == "Shared" else "Built by your org, after approval in admin.cloud.microsoft > Agents > Requests"
    print(f"  Teams / Copilot (same tenant): Apps > {where} > '{display}'")
