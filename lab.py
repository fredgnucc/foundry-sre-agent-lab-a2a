"""foundry-to-sre-agent lab CLI. Raw ARM REST, no Bicep, no az.

  python lab.py up network            # per network: RG, NSGs, NAT, VNet, DNS; then peerings, Log Analytics
  python lab.py up foundry            # accounts, model, project, private endpoints
  python lab.py up jump               # Linux VM (sshd on 2222): the SOCKS door into the private VNet
  python lab.py up gateway            # APIM v2 AI gateway, private endpoint, key + Entra model APIs
  python lab.py up runtime            # storage, Cosmos, AI Search (all private) + capability host
  python lab.py up knowledge          # Foundry IQ: index of the lab's own architecture + knowledge base
  python lab.py up agent              # hosted agent agents/architect (via the tunnel)
  python lab.py up mcp                # the agent as an MCP tool on the gateway, for SRE Agent
  python lab.py up app                # the "customer" web app in its own RG: monitoring, alerts, zip deploy
  python lab.py up sre                # SRE Agent in the SRE VNet, mapped to the app RG, MCP connector,
                                      #   response plan + consult-architect skill (data plane)
  python lab.py up all                # every layer, in order
  python lab.py up all --watch        # repeat passes (with backoff) until one is clean
  python lab.py up all --dry-run      # print each PUT and its body, change nothing
  python lab.py show                  # read back what exists
  python lab.py tunnel                # hold the SOCKS tunnel open (Ctrl+C to close)
  python lab.py retrieve "question"   # knowledge base retrieve, raw hits
  python lab.py ask "question"        # the hosted agent, directly
  python lab.py mcp "question"        # the hosted agent through APIM MCP, as SRE Agent calls it
  python lab.py mcp-key               # MCP URL + subscription key for the SRE Agent connector
  python lab.py publish [--force]     # zip deploy app/ again (skipped when unchanged)
  python lab.py fault --mode errors   # FAULT_MODE=errors|slow|none + GET traffic -> alert -> incident
  python lab.py sre-test              # the SRE Agent's own MCP test call to the gateway
  python lab.py down --yes            # safe-order teardown + purges, then a leftovers audit
  python lab.py leftovers             # soft-deleted / surviving resources of this lab
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import requests

from labkit import (agent, arm, foundry, gateway, jump, knowledge, mcp, network, runtime, sre, teardown, tunnel,
                    webapp)
from labkit.config import Config, Names, load, names, vnet_name

LAYERS = {"network": network.up, "foundry": foundry.up, "jump": jump.up, "gateway": gateway.up,
          "runtime": runtime.up, "knowledge": knowledge.up, "agent": agent.up, "mcp": mcp.up,
          "app": webapp.up, "sre": sre.up}


def show(c: arm.Client, cfg: Config, n: Names) -> None:
    for net in cfg.networks:
        vnet = arm.get(c, network.vnet_id(c, cfg, net), network.API_NET)
        print(f"[{net.name}] {net.resourceGroup} {net.location} {vnet_name(cfg, net)}: "
              f"{'deployed' if vnet else 'not deployed'}")
        if not vnet:
            continue
        for s in vnet["properties"]["subnets"]:
            p = s["properties"]
            d = ",".join(x["properties"]["serviceName"] for x in p.get("delegations", []))
            nat = " nat" if p.get("natGateway") else ""
            print(f"  subnet {s['name']:<15} {p['addressPrefix']:<14} {d}{nat}")
        for p in vnet["properties"].get("virtualNetworkPeerings", []):
            print(f"  peering {p['name']}: {p['properties']['peeringState']}")
    fnet = network.network(cfg, cfg.foundry.network)
    rg = fnet.resourceGroup
    for name in (n.model_account, n.account):
        acc = arm.get(c, arm.res_id(c, rg, "Microsoft.CognitiveServices/accounts", name), foundry.API_COG)
        if not acc:
            print(f"  {name}: not deployed")
            continue
        p = acc["properties"]
        inj = [x["subnetArmId"].rsplit("/", 1)[-1] for x in p.get("networkInjections") or []]
        print(f"  {name}: {p['provisioningState']} public={p['publicNetworkAccess']} "
              f"localAuth={'off' if p.get('disableLocalAuth') else 'on'} injection={inj or '-'}")
        deps = arm.get(c, arm.res_id(c, rg, "Microsoft.CognitiveServices/accounts", f"{name}/deployments"), foundry.API_COG)
        for d in (deps or {}).get("value", []):
            print(f"    model {d['name']} {d['sku']['name']}/{d['sku']['capacity']} {d['properties']['provisioningState']}")
    for zone in foundry.FOUNDRY_ZONES:
        recs = arm.get(c, f"{network.zone_id(c, cfg, zone)}/A", network.API_DNS) or {}
        for r in recs.get("value", []):
            ips = ",".join(a["ipv4Address"] for a in r["properties"].get("aRecords", []))
            print(f"  dns {r['name']}.{zone} -> {ips}")
    ip = jump.public_ip(c, cfg, n)
    print(f"  jump {n.jump_vm}: {ip or 'not deployed'}")
    apim = arm.get(c, gateway.apim_id(c, cfg, n), gateway.API_APIM)
    if apim:
        p = apim["properties"]
        print(f"  apim {n.apim}: {p['provisioningState']} {apim['sku']['name']} public={p.get('publicNetworkAccess')} "
              f"vnet={p.get('virtualNetworkType')}")
        apis = arm.get(c, f"{apim['id']}/apis", gateway.API_APIM) or {}
        for a in apis.get("value", []):
            print(f"    api /{a['properties'].get('path')} {a['properties'].get('type') or 'http'}")
    else:
        print(f"  apim {n.apim}: not deployed")
    for kind, rid in runtime.ids(c, cfg, n).items():
        if kind == "project":
            continue
        api = {"storage": runtime.API_ST, "cosmos": runtime.API_COSMOS, "search": runtime.API_SRCH}[kind]
        r = arm.get(c, rid, api)
        state = r["properties"].get("provisioningState") if r else "not deployed"
        print(f"  {kind} {rid.rsplit('/', 1)[-1]}: {state}")
    host = arm.get(c, f"{runtime.ids(c, cfg, n)['project']}/capabilityHosts/{cfg.runtime.capabilityHost}", foundry.API_COG)
    print(f"  capability host {cfg.runtime.capabilityHost}: "
          f"{host['properties'].get('provisioningState') if host else 'not deployed'}")
    sre.show(c, cfg)


def reconcile(c: arm.Client, cfg: Config, n: Names, layers: list[str], watch: bool, attempts: int) -> None:
    """One pass = every layer in order; each PUT is idempotent, so a pass after a failure
    resumes where the last one stopped. --watch repeats passes with backoff until one is clean."""
    for attempt in range(1, (attempts if watch else 1) + 1):
        try:
            for layer in layers:
                LAYERS[layer](c, cfg, n)
            if watch:
                print(f"== converged on pass {attempt}")
            return
        except (arm.ArmError, requests.RequestException) as e:
            if not watch or attempt == attempts:
                raise
            pause = min(30 * 2 ** (attempt - 1), 300)
            print(f"== pass {attempt} failed: {str(e).splitlines()[0]}\n== retrying in {pause}s", file=sys.stderr)
            time.sleep(pause)


def main() -> int:
    # Agent answers contain Unicode (arrows, bullets); a piped Windows console is cp1252.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(Path(__file__).with_name("lab.yaml")))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_up = sub.add_parser("up")
    p_up.add_argument("layers", nargs="+", choices=[*LAYERS, "all"])
    p_up.add_argument("--dry-run", action="store_true")
    p_up.add_argument("--watch", action="store_true", help="re-run passes until one succeeds")
    p_up.add_argument("--attempts", type=int, default=8, help="max passes with --watch")
    sub.add_parser("show")
    p_down = sub.add_parser("down")
    p_down.add_argument("--yes", action="store_true", required=True)
    p_down.add_argument("--dry-run", action="store_true")
    sub.add_parser("leftovers")
    sub.add_parser("tunnel")
    for name in ("retrieve", "ask", "mcp"):
        sub.add_parser(name).add_argument("question")
    sub.add_parser("mcp-key")
    sub.add_parser("publish").add_argument("--force", action="store_true")
    p_fault = sub.add_parser("fault")
    p_fault.add_argument("--mode", choices=["errors", "slow", "none"], default="errors")
    p_fault.add_argument("--minutes", type=float, default=6)
    p_fault.add_argument("--workers", type=int, default=4)
    p_fault.add_argument("--traffic-only", action="store_true", help="keep FAULT_MODE as is")
    sub.add_parser("sre-test")
    args = ap.parse_args()

    try:
        cfg = load(args.config)
    except (KeyError, ValueError) as e:
        print(f"CONFIG {e}", file=sys.stderr)
        return 2
    n = names(cfg)
    c = arm.Client(subscription=cfg.subscriptionId, dry_run=getattr(args, "dry_run", False))
    try:
        if args.cmd == "up":
            layers = list(LAYERS) if "all" in args.layers else args.layers
            reconcile(c, cfg, n, layers, args.watch and not c.dry_run, args.attempts)
        elif args.cmd == "show":
            show(c, cfg, n)
        elif args.cmd == "leftovers":
            return 1 if teardown.leftovers(c, cfg, n) else 0
        elif args.cmd == "tunnel":
            tunnel.up(c, cfg, n)
            print(f"  holding the tunnel; set HTTPS_PROXY={c.proxies['https']} in other tools. Ctrl+C to close.")
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                return 0
        elif args.cmd == "retrieve":
            tunnel.up(c, cfg, n)
            for hit in knowledge.retrieve(c, cfg, n, args.question):
                print(f"- [{hit.get('category', '?')}] {hit.get('title', hit.get('ref_id', '?'))}")
        elif args.cmd == "ask":
            tunnel.up(c, cfg, n)
            print(agent.ask(c, cfg, n, args.question))
        elif args.cmd == "mcp":
            tunnel.up(c, cfg, n)
            print(mcp.call_tool(c, cfg, n, args.question))
        elif args.cmd == "mcp-key":
            print(f"url    {mcp.mcp_url(n)}\nheader Ocp-Apim-Subscription-Key: {mcp.key(c, cfg, n)}")
        elif args.cmd == "publish":
            webapp.publish(c, cfg, force=args.force)
        elif args.cmd == "fault":
            webapp.fault(c, cfg, args.mode, args.minutes, args.workers, args.traffic_only)
        elif args.cmd == "sre-test":
            a = arm.get(c, sre.agent_id(c, cfg), sre.API_AGENT)
            if not a:
                raise arm.ArmError(f"{cfg.sre.name} not deployed: python lab.py up sre")
            if not sre.test_connection(c, cfg, n, a["properties"]["agentEndpoint"]):
                return 1
        else:
            teardown.down(c, cfg, n)
            if not c.dry_run:
                print("== leftovers")
                return 1 if teardown.leftovers(c, cfg, n) else 0
    except arm.ArmError as e:
        print(f"ERROR {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
