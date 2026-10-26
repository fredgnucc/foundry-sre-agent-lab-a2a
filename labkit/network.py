"""Layer 1 - network underlay, one loop per entry in lab.yaml `networks`: RG, NSGs,
NAT, VNet, private DNS zones and links, peerings. Plus the Log Analytics workspace."""

from __future__ import annotations

from labkit import arm
from labkit.config import Config, Names, Network, NsgRule, Peering, Subnet, network, vnet_name, zone_owner

API_RG = "2021-04-01"
API_NET = "2024-05-01"
API_DNS = "2024-06-01"
API_LAW = "2023-09-01"

NSG = "Microsoft.Network/networkSecurityGroups"
VNET = "Microsoft.Network/virtualNetworks"
ZONE = "Microsoft.Network/privateDnsZones"


def _rule(r: NsgRule) -> dict:
    return {"name": r.name, "properties": {
        "priority": r.priority, "direction": r.direction, "access": r.access, "protocol": r.protocol,
        "sourceAddressPrefix": r.src, "sourcePortRange": "*",
        "destinationAddressPrefix": r.dst, "destinationPortRange": r.port,
    }}


def nsg_name(vnet: str, s: Subnet) -> str:
    return f"nsg-{vnet}-{s.name.removeprefix('snet-')}"


def vnet_id(c: arm.Client, cfg: Config, net: Network) -> str:
    return arm.res_id(c, net.resourceGroup, VNET, vnet_name(cfg, net))


def subnet_id(c: arm.Client, cfg: Config, net_name: str, subnet_name: str) -> str:
    return f"{vnet_id(c, cfg, network(cfg, net_name))}/subnets/{subnet_name}"


def zone_id(c: arm.Client, cfg: Config, zone: str) -> str:
    return arm.res_id(c, zone_owner(cfg, zone).resourceGroup, ZONE, zone)


def _link(c: arm.Client, cfg: Config, zone: str, net: Network) -> None:
    arm.ensure(c, f"{zone_id(c, cfg, zone)}/virtualNetworkLinks/link-{vnet_name(cfg, net)}", API_DNS, {
        "location": "global", "tags": cfg.tags,
        "properties": {"registrationEnabled": False, "virtualNetwork": {"id": vnet_id(c, cfg, net)}}})


def _vnet_up(c: arm.Client, cfg: Config, net: Network) -> None:
    rg, loc, tags, vnet = net.resourceGroup, net.location, cfg.tags, vnet_name(cfg, net)

    # Every subnet gets an NSG: a tenant policy creates an empty one otherwise, and the
    # two then fight on every re-run. A PUT replaces the whole rule list.
    print(f"== [{net.name}] NSGs")
    arm.each(lambda s: arm.put(c, arm.res_id(c, rg, NSG, nsg_name(vnet, s)), API_NET, {
        "location": loc, "tags": tags, "properties": {"securityRules": [_rule(r) for r in s.nsgRules]}}),
        net.subnets)

    nat_id = None
    if any(s.natGateway for s in net.subnets):
        print(f"== [{net.name}] NAT gateway (outbound internet for natGateway subnets)")
        pip_id = arm.res_id(c, rg, "Microsoft.Network/publicIPAddresses", f"pip-nat-{vnet}")
        arm.put(c, pip_id, API_NET, {"location": loc, "tags": tags, "sku": {"name": "Standard"},
                                     "properties": {"publicIPAllocationMethod": "Static"}})
        nat_id = arm.res_id(c, rg, "Microsoft.Network/natGateways", f"nat-{vnet}")
        arm.put(c, nat_id, API_NET, {"location": loc, "tags": tags, "sku": {"name": "Standard"}, "properties": {
            "publicIpAddresses": [{"id": pip_id}], "idleTimeoutInMinutes": 4}})

    print(f"== [{net.name}] VNet {vnet}")
    subnets = []
    for s in net.subnets:
        props: dict = {"addressPrefix": s.prefix, "networkSecurityGroup": {"id": arm.res_id(c, rg, NSG, nsg_name(vnet, s))}}
        if s.delegation:
            props["delegations"] = [{"name": f"{s.name.removeprefix('snet-')}-delegation",
                                     "properties": {"serviceName": s.delegation}}]
        if s.natGateway:
            props["natGateway"] = {"id": nat_id}
            props["defaultOutboundAccess"] = False
        subnets.append({"name": s.name, "properties": props})
    vid = vnet_id(c, cfg, net)
    # A VNet PUT that omits virtualNetworkPeerings can drop them, so carry the live ones over.
    live = arm.get(c, vid, API_NET) or {}
    peerings = [{"name": p["name"], "properties": {k: p["properties"][k] for k in (
        "remoteVirtualNetwork", "allowVirtualNetworkAccess", "allowForwardedTraffic",
        "allowGatewayTransit", "useRemoteGateways")}}
        for p in live.get("properties", {}).get("virtualNetworkPeerings", [])]
    arm.put(c, vid, API_NET, {"location": loc, "tags": tags, "properties": {
        "addressSpace": {"addressPrefixes": net.addressSpace}, "subnets": subnets,
        "virtualNetworkPeerings": peerings}})

    if net.dnsZones:
        print(f"== [{net.name}] private DNS zones + links")

    def zone_up(zone: str) -> None:
        arm.ensure(c, arm.res_id(c, rg, ZONE, zone), API_DNS, {"location": "global", "tags": tags})
        _link(c, cfg, zone, net)

    arm.each(zone_up, net.dnsZones)


def _peer(c: arm.Client, cfg: Config, a: Network, b: Network, p: Peering) -> None:
    arm.put(c, f"{vnet_id(c, cfg, a)}/virtualNetworkPeerings/peer-to-{b.name}", API_NET, {"properties": {
        "remoteVirtualNetwork": {"id": vnet_id(c, cfg, b)}, "allowVirtualNetworkAccess": True,
        "allowForwardedTraffic": p.allowForwardedTraffic, "allowGatewayTransit": False, "useRemoteGateways": False}})


def up(c: arm.Client, cfg: Config, n: Names) -> None:
    fnet = network(cfg, cfg.foundry.network)

    def workspace() -> None:
        print(f"== Log Analytics {n.log}")
        arm.put(c, arm.res_id(c, fnet.resourceGroup, "Microsoft.OperationalInsights/workspaces", n.log), API_LAW, {
            "location": fnet.location, "tags": cfg.tags,
            "properties": {"sku": {"name": "PerGB2018"}, "retentionInDays": 30}})

    print("== resource groups")
    arm.each(lambda net: arm.put(c, arm.rg_id(c, net.resourceGroup), API_RG,
                                 {"location": net.location, "tags": cfg.tags}), cfg.networks)
    # Networks are independent of each other (and of the workspace) until linked/peered.
    arm.parallel(workspace, *[lambda net=net: _vnet_up(c, cfg, net) for net in cfg.networks])

    if cfg.dnsLinks:
        print("== DNS links to zones owned by another network")
    arm.each(lambda d: _link(c, cfg, d.zone, network(cfg, d.network)), cfg.dnsLinks)

    def peer(p: Peering) -> None:
        a, b = (network(cfg, x) for x in p.between)
        print(f"== peering {a.name} <-> {b.name}")
        _peer(c, cfg, a, b, p)
        _peer(c, cfg, b, a, p)

    arm.each(peer, cfg.peerings)
