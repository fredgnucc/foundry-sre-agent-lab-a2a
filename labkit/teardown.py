"""Teardown in dependency order, then an audit of what survives a resource group delete.

Deleting the RGs alone leaves soft-deleted APIM/Foundry accounts (names reserved for
48 h), a soft-deleted workspace (14 days), diagnostic settings that reattach to a
recreated resource of the same name, and subnets held by service association links."""

from __future__ import annotations

import time

from labkit import arm, foundry, network, sre, webapp
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


def _delete_account(c: arm.Client, rid: str, timeout: int = 900) -> None:
    """ARM's nested-resource check lags the project delete: the account DELETE keeps
    returning 409 CannotDeleteResource for a few minutes after the project GET is 404."""
    deadline = time.time() + timeout
    while True:
        try:
            return arm.delete(c, rid, foundry.API_COG)
        except arm.ArmError as e:
            if "CannotDeleteResource" not in str(e) or time.time() > deadline:
                raise
            print("    ... nested project still registered, retrying in 30s")
            time.sleep(30)


def _deleted_account(c: arm.Client, loc: str, rg: str, name: str) -> str:
    return f"/subscriptions/{c.subscription}/providers/{COG.split('/')[0]}/locations/{loc}/resourceGroups/{rg}/deletedAccounts/{name}"


def _deleted_apim(c: arm.Client, loc: str, name: str) -> str:
    return f"/subscriptions/{c.subscription}/providers/Microsoft.ApiManagement/locations/{loc}/deletedservices/{name}"


def _workspaces(c: arm.Client, cfg: Config, n: Names) -> list[tuple[str, str]]:
    """(rg, name) of every Log Analytics workspace this lab creates."""
    fnet = network.network(cfg, cfg.foundry.network)
    return [(fnet.resourceGroup, n.log), (cfg.app.resourceGroup, webapp.names(cfg).log),
            (network.network(cfg, cfg.sre.network).resourceGroup, sre.names(cfg).log)]


def down(c: arm.Client, cfg: Config, n: Names) -> None:
    fnet = network.network(cfg, cfg.foundry.network)
    rg, loc = fnet.resourceGroup, fnet.location
    acc = arm.res_id(c, rg, COG, n.account)
    model_acc = arm.res_id(c, rg, COG, n.model_account)
    apim = arm.res_id(c, rg, "Microsoft.ApiManagement/service", n.apim)
    site = webapp.site_id(c, cfg)

    print("== SRE Agent first (holds the delegated agent subnet; connectors go with it)")
    arm.delete(c, sre.agent_id(c, cfg), sre.API_AGENT)

    print("== diagnostic settings (they outlive the resource and reattach on recreate)")
    for rid, api in ((apim, API_APIM), (acc, foundry.API_COG), (model_acc, foundry.API_COG), (site, webapp.API_WEB)):
        if arm.get(c, rid, api):
            _drop_diagnostics(c, rid)

    # Purge in the region the resource actually lives in, which differs from lab.yaml after a region switch.
    where = lambda rid, api: (arm.get(c, rid, api) or {}).get("location", loc).replace(" ", "").lower()

    def account(name: str, rid: str) -> None:
        aloc = where(rid, foundry.API_COG)
        if arm.get(c, rid, foundry.API_COG):
            # Capability hosts hold the injected agent subnet; ~20 min, and the account waits on them.
            _drop_capability_hosts(c, f"{rid}/projects/{cfg.foundry.project}")
            _drop_capability_hosts(c, rid)
        for p in (arm.get(c, f"{rid}/projects", foundry.API_COG) or {}).get("value", []):
            arm.delete(c, f"{rid}/projects/{p['name'].rsplit('/', 1)[-1]}", foundry.API_COG)
        _delete_account(c, rid)
        arm.delete(c, _deleted_account(c, aloc, rg, name), foundry.API_COG)

    def gateway() -> None:
        aloc = where(apim, API_APIM)
        arm.delete(c, apim, API_APIM)
        arm.delete(c, _deleted_apim(c, aloc, n.apim), API_APIM)

    def group(g: str) -> None:
        # Force delete first: an RG delete would soft-delete the workspace for 14 days.
        for wrg, w in _workspaces(c, cfg, n):
            if wrg == g:
                arm.delete(c, arm.res_id(c, wrg, "Microsoft.OperationalInsights/workspaces", w), f"{API_LAW}&force=true")
        arm.delete(c, arm.rg_id(c, g), network.API_RG)

    # Only the foundry RG waits: it holds the VNet, the shared DNS zones and the peering.
    others = [g for g in dict.fromkeys([cfg.app.resourceGroup] + [x.resourceGroup for x in reversed(cfg.networks)]) if g != rg]
    print(f"== in parallel: Foundry accounts (capability hosts, projects, delete, purge), APIM (delete, purge), "
          f"resource groups {', '.join(others)}")
    arm.parallel(lambda: account(n.account, acc), lambda: account(n.model_account, model_acc), gateway,
                 *[lambda g=g: group(g) for g in others])

    print(f"== resource group {rg} (VNet, DNS zones, peering)")
    group(rg)


def leftovers(c: arm.Client, cfg: Config, n: Names) -> int:
    """Print anything of this lab that still exists. Returns the count."""
    fnet = network.network(cfg, cfg.foundry.network)
    rg, loc = fnet.resourceGroup, fnet.location
    s = f"/subscriptions/{c.subscription}"
    found = []
    for g in [net.resourceGroup for net in cfg.networks] + [cfg.app.resourceGroup]:
        if arm.get(c, arm.rg_id(c, g), network.API_RG):
            found.append(f"resource group {g}")
    for name in (n.account, n.model_account):
        if arm.get(c, _deleted_account(c, loc, rg, name), foundry.API_COG):
            found.append(f"soft-deleted Foundry account {name}")
    if arm.get(c, _deleted_apim(c, loc, n.apim), API_APIM):
        found.append(f"soft-deleted APIM {n.apim}")
    ours = {w for _, w in _workspaces(c, cfg, n)}
    for w in (arm.get(c, f"{s}/providers/Microsoft.OperationalInsights/deletedWorkspaces", API_LAW) or {}).get("value", []):
        if w["name"] in ours:
            found.append(f"soft-deleted workspace {w['name']}")
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
