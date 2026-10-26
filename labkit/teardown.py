"""Teardown in dependency order, then an audit of what survives a resource group delete.

Deleting the RGs alone leaves soft-deleted APIM/Foundry accounts (names reserved for
48 h), a soft-deleted workspace (14 days), diagnostic settings that reattach to a
recreated resource of the same name, and subnets held by service association links."""

from __future__ import annotations

from labkit import arm, foundry, network
from labkit.config import Config, Names

API_APIM = "2024-05-01"
API_DIAG = "2021-05-01-preview"
API_LAW = network.API_LAW
COG = "Microsoft.CognitiveServices/accounts"


def _drop_diagnostics(c: arm.Client, rid: str) -> None:
    for d in (arm.get(c, f"{rid}/providers/Microsoft.Insights/diagnosticSettings", API_DIAG) or {}).get("value", []):
        arm.delete(c, f"{rid}/providers/Microsoft.Insights/diagnosticSettings/{d['name']}", API_DIAG)


def _drop_capability_hosts(c: arm.Client, base: str) -> None:
    for h in (arm.get(c, f"{base}/capabilityHosts", foundry.API_COG) or {}).get("value", []):
        arm.delete(c, f"{base}/capabilityHosts/{h['name']}", foundry.API_COG)


def _deleted_account(c: arm.Client, loc: str, rg: str, name: str) -> str:
    return f"/subscriptions/{c.subscription}/providers/{COG.split('/')[0]}/locations/{loc}/resourceGroups/{rg}/deletedAccounts/{name}"


def _deleted_apim(c: arm.Client, loc: str, name: str) -> str:
    return f"/subscriptions/{c.subscription}/providers/Microsoft.ApiManagement/locations/{loc}/deletedservices/{name}"


def down(c: arm.Client, cfg: Config, n: Names) -> None:
    fnet = network.network(cfg, cfg.foundry.network)
    rg, loc = fnet.resourceGroup, fnet.location
    acc = arm.res_id(c, rg, COG, n.account)
    model_acc = arm.res_id(c, rg, COG, n.model_account)
    apim = arm.res_id(c, rg, "Microsoft.ApiManagement/service", n.apim)

    print("== diagnostic settings (they outlive the resource and reattach on recreate)")
    for rid, api in ((apim, API_APIM), (acc, foundry.API_COG), (model_acc, foundry.API_COG)):
        if arm.get(c, rid, api):
            _drop_diagnostics(c, rid)

    print("== capability hosts (they hold the injected agent subnet)")
    if arm.get(c, acc, foundry.API_COG):
        _drop_capability_hosts(c, f"{acc}/projects/{cfg.foundry.project}")
        _drop_capability_hosts(c, acc)

    print("== Foundry accounts: projects, delete, then purge (accounts with projects return 409)")
    for name, rid in ((n.account, acc), (n.model_account, model_acc)):
        for p in (arm.get(c, f"{rid}/projects", foundry.API_COG) or {}).get("value", []):
            arm.delete(c, f"{rid}/projects/{p['name'].rsplit('/', 1)[-1]}", foundry.API_COG)
        arm.delete(c, rid, foundry.API_COG)
        arm.delete(c, _deleted_account(c, loc, rg, name), foundry.API_COG)

    print("== APIM: delete, then purge (releases the name and snet-apim)")
    arm.delete(c, apim, API_APIM)
    arm.delete(c, _deleted_apim(c, loc, n.apim), API_APIM)

    print("== Log Analytics: force delete (skips the 14-day soft delete)")
    arm.delete(c, arm.res_id(c, rg, "Microsoft.OperationalInsights/workspaces", n.log), f"{API_LAW}&force=true")

    # Reverse order: networks that consume shared zones/peerings go first.
    for net in reversed(cfg.networks):
        print(f"== resource group {net.resourceGroup}")
        arm.delete(c, arm.rg_id(c, net.resourceGroup), network.API_RG)


def leftovers(c: arm.Client, cfg: Config, n: Names) -> int:
    """Print anything of this lab that still exists. Returns the count."""
    fnet = network.network(cfg, cfg.foundry.network)
    rg, loc = fnet.resourceGroup, fnet.location
    s = f"/subscriptions/{c.subscription}"
    found = []
    for net in cfg.networks:
        if arm.get(c, arm.rg_id(c, net.resourceGroup), network.API_RG):
            found.append(f"resource group {net.resourceGroup}")
    for name in (n.account, n.model_account):
        if arm.get(c, _deleted_account(c, loc, rg, name), foundry.API_COG):
            found.append(f"soft-deleted Foundry account {name}")
    if arm.get(c, _deleted_apim(c, loc, n.apim), API_APIM):
        found.append(f"soft-deleted APIM {n.apim}")
    for w in (arm.get(c, f"{s}/providers/Microsoft.OperationalInsights/deletedWorkspaces", API_LAW) or {}).get("value", []):
        if w["name"] == n.log:
            found.append(f"soft-deleted workspace {n.log}")
    # Azure creates one watcher per region on first network deploy; free and shared with
    # other labs in the region, so reported but not counted.
    watchers = (arm.get(c, f"{arm.rg_id(c, 'NetworkWatcherRG')}/providers/Microsoft.Network/networkWatchers",
                        network.API_NET) or {}).get("value", [])
    for loc in sorted({w["location"] for w in watchers} & {net.location for net in cfg.networks}):
        print(f"  INFO NetworkWatcherRG has a watcher in {loc} (Azure-created, shared, free)")
    for f in found:
        print(f"  LEFT {f}")
    print(f"  {len(found)} leftover(s)")
    return len(found)
