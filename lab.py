"""foundry-sre-agent-lab-a2a lab CLI. Raw ARM REST, no Bicep, no az.

SRE part (required): an SRE Agent on a web app nobody operates
  python lab.py up providers          # register the resource providers listed in lab.yaml (new subscription)
  python lab.py up sre-network        # SRE VNet: RG, NSG, NAT, delegated agent subnet
  python lab.py up app                # the "customer" web app in its own RG: monitoring, alerts, zip deploy
  python lab.py up sre                # SRE Agent in the SRE VNet, mapped to the app + external RGs, response plan;
                                      #   with the Foundry part: MCP connector + consult-architect skill
Foundry part (optional): a private Foundry agent the SRE Agent consults over MCP
  python lab.py up foundry-network    # Foundry VNet: RG, NSGs, VNet, private DNS zones, gateway Log Analytics
  python lab.py up foundry            # accounts, model, project, private endpoints
  python lab.py up jump               # Linux VM (sshd on 2222): the SOCKS door into the private VNet
  python lab.py up gateway            # APIM v2 AI gateway, private endpoint, key + Entra model APIs
  python lab.py up runtime            # storage, Cosmos, AI Search (all private) + capability host
  python lab.py up knowledge          # Foundry IQ: index of the lab's own architecture + knowledge base
  python lab.py up agent              # hosted agent agents/architect (via the tunnel)
  python lab.py up prompt             # prompt agent architect-prompt: definition only, same knowledge
  python lab.py up mcp                # peering + DNS link to the SRE VNet, both agents as MCP tools on the
                                      #   gateway, SRE Agent connector when the SRE part exists
Vendor (optional, after the SRE part): a resource outside Azure
  python lab.py up external           # vendor VM in its own VNet: fake vendor /metrics + Prometheus agent ->
                                      #   Azure Monitor workspace (managed Prometheus), alert rules
  One layer at a time (no `up all`): check each one before the next; `status` shows what comes next.
  python lab.py up <layer> --watch    # repeat passes (with backoff) until one is clean
  python lab.py up <layer> --dry-run  # print each PUT and its body, change nothing
  python lab.py status                # one line per layer (deployed or not) + the next command
  python lab.py show                  # read back what exists
  python lab.py tunnel                # hold the SOCKS tunnel open (Ctrl+C to close)
  python lab.py retrieve "question"   # knowledge base retrieve, raw hits
  python lab.py ask "question"        # the hosted agent, directly (--prompt: the prompt agent)
  python lab.py mcp "question"        # the hosted agent through APIM MCP, as SRE Agent calls it (--prompt)
  python lab.py mcp-key               # MCP URL + subscription key for the SRE Agent connector
  python lab.py publish [--force]     # zip deploy app/ again (skipped when unchanged)
  python lab.py fault --mode errors   # FAULT_MODE=errors|slow|none + GET traffic -> alert -> incident
  python lab.py sre-test              # the SRE Agent's own MCP test call to the gateway
  python lab.py vendor slow           # external vendor mode ok|slow|errors|down (no arg: read it) -> Prometheus alert
  python lab.py promql "up"           # PromQL instant query on the Azure Monitor workspace
  python lab.py m365 [--scope Tenant] [--app-version 1.0.1]
                                      # publish the hosted agent to Teams / M365 Copilot (opt-in:
                                      #   opens only the Activity Protocol route to Bot Service)
  python lab.py down --yes            # safe-order teardown + purges, then a leftovers audit
  python lab.py down external --yes   # only the external layer (vendor VM + VNet, workspace, rules, identity); keeps its RG
  python lab.py leftovers             # soft-deleted / surviving resources of this lab
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import requests

from labkit import (agent, arm, external, foundry, gateway, jump, knowledge, m365, mcp, network, prompt, providers, runtime,
                    sre, teardown, tunnel, vm, webapp)
from labkit.config import Config, Names, load, names, vnet_name

def mcp_up(c: arm.Client, cfg: Config, n: Names) -> None:
    mcp.up(c, cfg, n)
    sre.attach(c, cfg, n)


# Deployment order: the SRE part first (required), then the optional Foundry part.
LAYERS = {"providers": providers.up, "sre-network": network.sre_up, "app": webapp.up, "sre": sre.up,
          "foundry-network": network.foundry_up, "foundry": foundry.up, "jump": jump.up, "gateway": gateway.up,
          "runtime": runtime.up, "knowledge": knowledge.up, "agent": agent.up,
          "prompt": prompt.up, "mcp": mcp_up, "external": external.up}
NEEDS = {"sre-network": ["providers"], "app": ["providers"], "sre": ["sre-network"],
         "foundry-network": ["providers"], "foundry": ["foundry-network"], "jump": ["foundry-network"],
         "gateway": ["foundry"], "runtime": ["foundry"], "knowledge": ["jump", "runtime"],
         "agent": ["gateway", "knowledge"], "prompt": ["gateway", "knowledge"],
         "mcp": ["agent", "prompt", "sre-network"], "external": ["providers"]}


def layer_table() -> str:
    rows = [f"  {name:<16} {'needs ' + ', '.join(NEEDS[name]) if name in NEEDS else ''}" for name in LAYERS]
    return ("layers, in deployment order:\n" + "\n".join(rows) +
            "\n\nwhere am I / what's next: python lab.py status")


def probes(c: arm.Client, cfg: Config, n: Names) -> dict[str, list[tuple[str, str]]]:
    """One ARM GET per layer proves it is deployed. Data-plane layers (knowledge, agent, prompt)
    and providers have no probe; their own prerequisites are checked instead."""
    aid = gateway.apim_id(c, cfg, n)
    project = runtime.ids(c, cfg, n)["project"]
    return {"sre-network": [(network.vnet_id(c, cfg, network.network(cfg, cfg.sre.network)), network.API_NET)],
            "foundry-network": [(network.vnet_id(c, cfg, network.network(cfg, cfg.foundry.network)), network.API_NET)],
            "foundry": [(project, foundry.API_COG)],
            "jump": [(jump.vm_id(c, cfg, n), jump.API_VM)],
            "gateway": [(aid, gateway.API_APIM)],
            "runtime": [(f"{project}/capabilityHosts/{cfg.runtime.capabilityHost}", foundry.API_COG)],
            "mcp": [(f"{aid}/apis/{n.account}-sre-mcp", gateway.API_APIM)],
            "app": [(webapp.site_id(c, cfg), webapp.API_WEB)],
            "external": [(vm.vm_id(c, cfg, external.host(cfg)), vm.API_VM), (external.amw_id(c, cfg), external.API_AMW),
                         (external.rules_id(c, cfg), external.API_PRG)],
            "sre": [(sre.agent_id(c, cfg), sre.API_AGENT)]}


def probe_state(c: arm.Client, targets: list[tuple[str, str]]) -> str | None:
    """None when every probed resource exists and Succeeded, else '<name> <state>'."""
    for rid, api in targets:
        r = arm.get(c, rid, api)
        state = (r or {}).get("properties", {}).get("provisioningState", "Succeeded") if r else "not found"
        if state.lower() != "succeeded":
            return f"{rid.rsplit('/', 1)[-1]} {state}"
    return None


def region_drift(c: arm.Client, cfg: Config) -> list[str]:
    """Deployed VNets / web app whose region differs from lab.yaml. A VNet can't move region,
    so the PUT would fail halfway; the only way is down, then up in the new region."""
    norm = lambda s: s.replace(" ", "").lower()
    found = [(f"network {net.name}", net.location, arm.get(c, network.vnet_id(c, cfg, net), network.API_NET))
             for net in cfg.networks]
    found.append(("app", cfg.app.location, arm.get(c, webapp.site_id(c, cfg), webapp.API_WEB)))
    return [f"  {what} is deployed in {r['location']} but lab.yaml says {want}"
            for what, want, r in found if r and norm(r["location"]) != norm(want)]


def region_line(cfg: Config) -> str:
    over = [f"{x.name} {x.location}" for x in cfg.networks if x.location != cfg.location]
    over += [f"app {cfg.app.location}"] if cfg.app.location != cfg.location else []
    return cfg.location + (f"; override: {', '.join(over)}" if over else "")


def status(c: arm.Client, cfg: Config, n: Names) -> None:
    """One line per layer, in deploy order. Data-plane layers live behind the tunnel, so they
    are not probed here; `python lab.py retrieve|ask|mcp` exercise them."""
    probe, done, nxt = probes(c, cfg, n), {}, None
    print(f"lab status ({region_line(cfg)})")
    for layer in LAYERS:
        if layer == "providers":
            left = [ns for ns in cfg.providers if providers._state(c, ns) != "Registered"]
            done[layer], line = not left, ("all registered" if not left else f"not registered: {', '.join(left)}")
        elif layer in probe:
            bad = probe_state(c, probe[layer])
            done[layer], line = bad is None, ("deployed" if bad is None else bad)
        else:
            done[layer] = all(done.get(d, True) for d in NEEDS.get(layer, []))
            print(f"  {'?':<4} {layer:<16} data plane, not probed (needs {', '.join(NEEDS[layer])})")
            continue
        print(f"  {'ok' if done[layer] else '--':<4} {layer:<16} {line}")
        if not done[layer] and nxt is None and all(done.get(d) for d in NEEDS.get(layer, [])):
            nxt = layer
    cmd = "python lab.py" + (f" --env {cfg.env}" if cfg.env else "")
    print(f"next: {cmd} up {nxt} --dry-run" if nxt else "next: every probed layer is deployed")
    if drift := region_drift(c, cfg):
        print(f"REGION MISMATCH:\n" + "\n".join(drift) + f"\n  -> {cmd} down --yes, then up again")


def check_needs(c: arm.Client, cfg: Config, n: Names, layers: list[str]) -> None:
    """Stop before any PUT when a prerequisite layer is neither deployed nor part of this run."""
    probe, verdict, missing = probes(c, cfg, n), {}, []
    for layer in layers:
        todo, seen = list(NEEDS.get(layer, [])), set()
        while todo:
            dep = todo.pop(0)
            if dep in seen or dep in layers:
                continue
            seen.add(dep)
            if dep not in probe:
                todo += NEEDS.get(dep, [])
                continue
            if dep not in verdict:
                verdict[dep] = probe_state(c, probe[dep])
            if verdict[dep]:
                missing.append(f"  {layer} needs {dep} ({verdict[dep]}) -> python lab.py up {dep}")
    if missing:
        raise arm.ArmError("prerequisites missing, nothing was changed:\n" + "\n".join(dict.fromkeys(missing)))


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
    ip = vm.public_ip(c, cfg, external.host(cfg))
    print(f"  vendor {external.names(cfg).vm}: {ip or 'not deployed'}")
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


class _Tee:
    """Console output also goes to the run log."""
    def __init__(self, stream, log):
        self.stream, self.log = stream, log

    def write(self, s):
        self.stream.write(s)
        self.log.write(s)
        self.log.flush()
        return len(s)

    def flush(self):
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


def start_logs(c: arm.Client, args: argparse.Namespace) -> Path:
    """logs/<time>-<command>.log = this run's console; logs/changes.jsonl = every change, all runs.
    With --env, the same under logs/<env>/."""
    logs = Path(__file__).with_name("logs") / (args.env or "")
    logs.mkdir(parents=True, exist_ok=True)
    c.run = time.strftime("%Y%m%d-%H%M%S") + "-" + "-".join([args.cmd, *getattr(args, "layers", [])])
    c.run += "-dry-run" if c.dry_run else ""
    c.journal = str(logs / "changes.jsonl")
    log = open(logs / f"{c.run}.log", "a", encoding="utf-8")
    log.write(f"# {time.strftime('%Y-%m-%d %H:%M:%S %z')}  python lab.py {' '.join(sys.argv[1:])}\n")
    sys.stdout, sys.stderr = _Tee(sys.stdout, log), _Tee(sys.stderr, log)
    return logs / f"{c.run}.log"


def main() -> int:
    # Agent answers contain Unicode (arrows, bullets); a piped Windows console is cp1252.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(Path(__file__).with_name("lab.yaml")))
    ap.add_argument("--env", default="", help="apply lab.<env>.yaml over the config: a second, separate instance")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_up = sub.add_parser("up", epilog=layer_table(), formatter_class=argparse.RawDescriptionHelpFormatter)
    p_up.add_argument("layers", nargs="*", metavar="layer", help="one or more layers (see below)")
    p_up.add_argument("--dry-run", action="store_true")
    p_up.add_argument("--watch", action="store_true", help="re-run passes until one succeeds")
    p_up.add_argument("--attempts", type=int, default=8, help="max passes with --watch")
    sub.add_parser("show")
    sub.add_parser("status")
    p_down = sub.add_parser("down")
    p_down.add_argument("layer", nargs="?", choices=["external"], help="only this layer (default: the whole lab)")
    p_down.add_argument("--yes", action="store_true", required=True)
    p_down.add_argument("--dry-run", action="store_true")
    sub.add_parser("leftovers")
    p_tun = sub.add_parser("tunnel")
    p_tun.add_argument("--browser", nargs="?", const="https://ai.azure.com/", metavar="URL",
                       help="also open a browser through the tunnel: 'browser' in lab.local.yaml, else Firefox, "
                            "else Edge (default URL https://ai.azure.com)")
    p_tun.add_argument("--edge", nargs="?", const="https://ai.azure.com/", metavar="URL",
                       help="same as --browser, with Edge")
    sub.add_parser("retrieve").add_argument("question")
    for name in ("ask", "mcp"):
        p = sub.add_parser(name)
        p.add_argument("question")
        p.add_argument("--prompt", action="store_true", help="the prompt agent instead of the hosted one")
    sub.add_parser("mcp-key")
    sub.add_parser("publish").add_argument("--force", action="store_true")
    p_fault = sub.add_parser("fault")
    p_fault.add_argument("--mode", choices=["errors", "slow", "none"], default="errors")
    p_fault.add_argument("--minutes", type=float, default=6)
    p_fault.add_argument("--workers", type=int, default=4)
    p_fault.add_argument("--traffic-only", action="store_true", help="keep FAULT_MODE as is")
    sub.add_parser("sre-test")
    p_vendor = sub.add_parser("vendor", help="set (or read) the fake external vendor's mode on the jump VM")
    p_vendor.add_argument("mode", nargs="?", choices=external.MODES)
    sub.add_parser("promql", help="PromQL instant query on the Azure Monitor workspace").add_argument("query")
    p_m365 = sub.add_parser("m365", help="publish the hosted agent to Teams / Microsoft 365 Copilot")
    p_m365.add_argument("--scope", choices=sorted(m365.SCHEMES), default="Shared",
                        help="Shared: only you, no approval. Tenant: whole org, after M365 admin approval.")
    p_m365.add_argument("--app-version", default="1.0.0", help="bump to republish metadata changes")
    p_m365.add_argument("--display-name")
    args = ap.parse_args()
    if args.cmd == "up":
        if "all" in args.layers:
            # Never validated end to end: a failure deep in a full run is hard to read in a lab.
            print("lab.py up all is disabled: deploy one layer at a time and check it before the next.\n"
                  "  python lab.py status   # what is deployed and the next command\n\n" + layer_table(),
                  file=sys.stderr)
            return 2
        bad = [x for x in args.layers if x not in LAYERS]
        if bad or not args.layers:
            p_up.print_usage(sys.stderr)
            print(f"lab.py up: {'unknown layer ' + ', '.join(bad) if bad else 'which layer?'}\n\n{layer_table()}",
                  file=sys.stderr)
            return 2

    try:
        cfg = load(args.config, args.env)
    except (KeyError, ValueError) as e:
        print(f"CONFIG {e}", file=sys.stderr)
        return 2
    n = names(cfg)
    if cfg.azureConfigDir:
        # az (and azure-identity, which runs az) read their login from this folder
        os.environ["AZURE_CONFIG_DIR"] = os.path.normpath(os.path.expanduser(cfg.azureConfigDir))
    c = arm.Client(subscription=cfg.subscriptionId, tenant=cfg.tenantId, dry_run=getattr(args, "dry_run", False))
    log = start_logs(c, args)
    if cfg.env:
        print(f"env: {cfg.env} (lab.yaml + lab.{cfg.env}.yaml), prefix {cfg.namePrefix}, "
              f"RGs {', '.join(dict.fromkeys([x.resourceGroup for x in cfg.networks] + [cfg.app.resourceGroup, cfg.external.resourceGroup]))}")
    try:
        return run(c, cfg, n, args)
    finally:
        print(f"log: {log.relative_to(Path(__file__).parent)} | changes: {log.parent.relative_to(Path(__file__).parent).as_posix()}/changes.jsonl")


def run(c: arm.Client, cfg: Config, n: Names, args: argparse.Namespace) -> int:
    try:
        arm.preflight(c)
        if args.cmd == "up":
            layers = [x for x in LAYERS if x in args.layers]
            if drift := region_drift(c, cfg):
                raise arm.ArmError("region mismatch, nothing was changed:\n" + "\n".join(drift)
                                   + "\n  -> python lab.py down --yes, then up again")
            check_needs(c, cfg, n, layers)
            reconcile(c, cfg, n, layers, args.watch and not c.dry_run, args.attempts)
        elif args.cmd == "show":
            show(c, cfg, n)
        elif args.cmd == "status":
            status(c, cfg, n)
        elif args.cmd == "leftovers":
            return 1 if teardown.leftovers(c, cfg, n) else 0
        elif args.cmd == "tunnel":
            tunnel.up(c, cfg, n)
            if args.browser or args.edge:
                tunnel.browser(c, cfg, n, args.browser or args.edge, "edge" if args.edge else cfg.browser)
            print(f"  holding the tunnel; set HTTPS_PROXY={c.proxies['https']} in other tools. Ctrl+C to close.")
            try:
                while True:
                    time.sleep(10)
                    if not tunnel.listening(cfg.foundry.jump.socksPort):
                        print(f"  {time.strftime('%H:%M:%S')} tunnel lost; reconnecting")
                        c.proxies = {}
                        tunnel.up(c, cfg, n)
            except KeyboardInterrupt:
                return 0
        elif args.cmd == "retrieve":
            tunnel.up(c, cfg, n)
            for hit in knowledge.retrieve(c, cfg, n, args.question):
                print(f"- [{hit.get('category', '?')}] {hit.get('title', hit.get('ref_id', '?'))}")
        elif args.cmd == "ask":
            tunnel.up(c, cfg, n)
            print(agent.ask(c, cfg, n, args.question, cfg.promptAgent.name if args.prompt else None))
        elif args.cmd == "mcp":
            tunnel.up(c, cfg, n)
            print(mcp.call_tool(c, cfg, n, args.question, cfg.promptAgent.tool if args.prompt else None))
        elif args.cmd == "mcp-key":
            print(f"url    {mcp.mcp_url(n)}\nheader Ocp-Apim-Subscription-Key: {mcp.key(c, cfg, n)}")
        elif args.cmd == "publish":
            webapp.publish(c, cfg, force=args.force)
        elif args.cmd == "fault":
            webapp.fault(c, cfg, args.mode, args.minutes, args.workers, args.traffic_only)
        elif args.cmd == "m365":
            m365.publish(c, cfg, n, args.scope, args.app_version, args.display_name)
        elif args.cmd == "vendor":
            external.vendor(c, cfg, n, args.mode)
        elif args.cmd == "promql":
            external.promql(c, cfg, args.query)
        elif args.cmd == "sre-test":
            a = arm.get(c, sre.agent_id(c, cfg), sre.API_AGENT)
            if not a:
                raise arm.ArmError(f"{cfg.sre.name} not deployed: python lab.py up sre")
            if not sre.test_connection(c, cfg, n, a["properties"]["agentEndpoint"]):
                return 1
        else:
            if args.layer == "external":
                external.down(c, cfg, n)
                return 0
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
