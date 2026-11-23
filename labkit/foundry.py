"""Layer 2 - Foundry: project account (agent network injection), model-only account
with the LLM deployment, the project, and a private endpoint for each account."""

from __future__ import annotations

from labkit import arm, network
from labkit.config import Config, Names

API_COG = "2025-06-01"
API_NET = "2024-05-01"
FOUNDRY_USER = "53ca6127-db72-4b80-b1b0-d745d6d5456d"  # formerly "Azure AI User"
# The "account" private-link group serves all three hostnames, so one endpoint covers them.
FOUNDRY_ZONES = ["privatelink.services.ai.azure.com", "privatelink.openai.azure.com",
                 "privatelink.cognitiveservices.azure.com"]


def private_endpoint(c: arm.Client, cfg: Config, n: Names, name: str, target_id: str, group: str,
                     zones: list[str]) -> dict:
    """Private endpoint in the Foundry PE subnet plus a zone group, which writes the A records."""
    net = network.network(cfg, cfg.foundry.network)
    pe_id = arm.res_id(c, net.resourceGroup, "Microsoft.Network/privateEndpoints", name)
    arm.put(c, pe_id, API_NET, {"location": net.location, "tags": cfg.tags, "properties": {
        "subnet": {"id": network.subnet_id(c, cfg, net.name, cfg.foundry.peSubnet)},
        "privateLinkServiceConnections": [{"name": name, "properties": {
            "privateLinkServiceId": target_id, "groupIds": [group]}}],
    }})
    arm.put(c, f"{pe_id}/privateDnsZoneGroups/default", API_NET, {"properties": {
        "privateDnsZoneConfigs": [{"name": z.replace(".", "-"),
                                   "properties": {"privateDnsZoneId": network.zone_id(c, cfg, z)}} for z in zones]}})
    return arm.get(c, pe_id, API_NET) or {}


def _account_body(cfg: Config, location: str, name: str, injection_subnet: str | None) -> dict:
    props = {
        "customSubDomainName": name,
        "publicNetworkAccess": "Disabled",
        "networkAcls": {"defaultAction": "Deny", "bypass": "AzureServices" if injection_subnet else "None",
                        "virtualNetworkRules": [], "ipRules": []},
        # MCAPS policy forces this anyway (CognitiveServices_LocalAuth_Modify): Entra only.
        "disableLocalAuth": True,
    }
    if injection_subnet:
        # Only settable at creation. Note the field is subnetArmId, not subnetResourceId.
        props["allowProjectManagement"] = True
        props["networkInjections"] = [{"scenario": "agent", "subnetArmId": injection_subnet,
                                       "useMicrosoftManagedNetwork": False}]
    return {"location": location, "tags": cfg.tags, "kind": "AIServices",
            "sku": {"name": "S0"}, "identity": {"type": "SystemAssigned"}, "properties": props}


def up(c: arm.Client, cfg: Config, n: Names) -> None:
    f = cfg.foundry
    net = network.network(cfg, f.network)
    rg, loc = net.resourceGroup, net.location
    cog = "Microsoft.CognitiveServices/accounts"

    model_acc = arm.res_id(c, rg, cog, n.model_account)
    acc = arm.res_id(c, rg, cog, n.account)

    def model_chain() -> None:
        print(f"== model account {n.model_account} (LLM only, gateway is its one caller)")
        arm.put(c, model_acc, API_COG, _account_body(cfg, loc, n.model_account, None))
        m = f.model
        arm.put(c, f"{model_acc}/deployments/{m.name}", API_COG, {
            "sku": {"name": m.sku, "capacity": m.capacity},
            "properties": {"model": {"format": "OpenAI", "name": m.name, "version": m.version}}})
        private_endpoint(c, cfg, n, f"pe-{n.model_account}", model_acc, "account", FOUNDRY_ZONES)

    def project_chain() -> None:
        print(f"== project account {n.account} (agent compute injected into {f.agentSubnet})")
        arm.put(c, acc, API_COG, _account_body(cfg, loc, n.account, network.subnet_id(c, cfg, f.network, f.agentSubnet)))
        arm.put(c, f"{acc}/projects/{f.project}", API_COG, {
            "location": loc, "tags": cfg.tags, "identity": {"type": "SystemAssigned"},
            "properties": {"displayName": f.project}})
        # Data-plane role for the signed-in user, so the portal's agent and playground views work as
        # soon as the tunnel is up (step 7), not only after up agent.
        print("== my role on the project account: Foundry User")
        arm.role(c, acc, arm.claims(c)["oid"], FOUNDRY_USER, "User")
        private_endpoint(c, cfg, n, f"pe-{n.account}", acc, "account", FOUNDRY_ZONES)

    arm.parallel(model_chain, project_chain)
