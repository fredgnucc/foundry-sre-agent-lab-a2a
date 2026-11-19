"""Layer - external monitoring: a resource outside Azure, watched with managed Prometheus.

The pull scenario: the vendor only exposes /metrics, so we run the collector.
  vendor     external/vendor_sim.py on the jump VM, 127.0.0.1:<exporterPort>/metrics
  collector  Prometheus in agent mode on the same VM: scrape the vendor, then remote-write
             (HTTPS POST, Entra token of a user-assigned identity, no key) to the workspace
  workspace  Azure Monitor workspace (Microsoft.Monitor/accounts) in its own RG; Azure creates
             its default data collection endpoint + rule in a managed RG MA_<amw>_<region>_managed
  alerts     Prometheus rule group (Microsoft.AlertsManagement/prometheusRuleGroups): a firing rule
             is an Azure Monitor alert on the workspace, so the SRE Agent, which maps this RG,
             receives it like any other alert (titles start with the response plan's prefix)

  ARM writes   PUT RG, workspace, identity, role on the DCR, rule group;
               PATCH the jump VM (attach identity); PUT runCommands/external-setup (runs setup.sh)
  Data plane   GET {prometheusQueryEndpoint}/api/v1/query (aud https://prometheus.monitor.azure.com)
               for `python lab.py promql`; public endpoint, Entra auth (Monitoring Data Reader)"""

from __future__ import annotations

import base64
import subprocess
import time
from dataclasses import dataclass
from urllib.parse import quote

from labkit import arm, jump, network, webapp
from labkit.config import Config, Names, network as net_of, suffix

API_AMW = "2023-04-03"
API_DCR = "2022-06-01"
API_PRG = "2023-03-01"
API_MI = "2023-01-31"
API_VM = jump.API_VM
QUERY_SCOPE = "https://prometheus.monitor.azure.com/.default"
METRICS_PUBLISHER = "3913510d-42f4-4e42-8a64-420c390055eb"   # on the DCR: remote-write
DATA_READER = "b0d8363b-8ddd-447d-831f-62ca05bff136"         # Monitoring Data Reader: PromQL
RUN_COMMAND = "external-setup"
MODE_FILE = "/var/lib/vendor-sim/mode"
MODES = ("ok", "slow", "errors", "down")


@dataclass
class ExtNames:
    amw: str
    identity: str
    rules: str
    alertPrefix: str


def names(cfg: Config) -> ExtNames:
    p = cfg.namePrefix.lower()
    return ExtNames(amw=f"amw-{p}-{suffix(cfg, cfg.external.resourceGroup)}", identity=f"id-{p}-collector",
                    rules=f"prg-{p}-vendor", alertPrefix=f"{webapp.names(cfg).alertPrefix}vendor-")


def amw_id(c: arm.Client, cfg: Config) -> str:
    return arm.res_id(c, cfg.external.resourceGroup, "Microsoft.Monitor/accounts", names(cfg).amw)


def rules_id(c: arm.Client, cfg: Config) -> str:
    return arm.res_id(c, cfg.external.resourceGroup, "Microsoft.AlertsManagement/prometheusRuleGroups", names(cfg).rules)


def ingestion(c: arm.Client, amw: dict) -> tuple[str, str]:
    """(remote-write URL, DCR id) from the workspace's default DCE + DCR."""
    ing = amw.get("properties", {}).get("defaultIngestionSettings", {})
    dce_id, dcr_id = ing.get("dataCollectionEndpointResourceId"), ing.get("dataCollectionRuleResourceId")
    if not dce_id or not dcr_id:
        return "<dce>/dataCollectionRules/<dcr-immutable-id>/streams/Microsoft-PrometheusMetrics/api/v1/write", "<dcr-id>"
    dce = arm.get(c, dce_id, API_DCR) or {}
    dcr = arm.get(c, dcr_id, API_DCR) or {}
    endpoint = dce.get("properties", {}).get("metricsIngestion", {}).get("endpoint", "<dce>")
    immutable = dcr.get("properties", {}).get("immutableId", "<dcr-immutable-id>")
    return (f"{endpoint}/dataCollectionRules/{immutable}/streams/Microsoft-PrometheusMetrics/api/v1/write"
            "?api-version=2023-04-24"), dcr_id


def _rules(cfg: Config, en: ExtNames, amw: str) -> dict:
    e = cfg.external
    sel = f'{{job="vendor",vendor="{e.vendor}"}}'

    def rule(name: str, expr: str, summary: str) -> dict:
        return {"alert": f"{en.alertPrefix}{name}", "expression": expr, "for": e.alertFor, "severity": e.severity,
                "enabled": True, "labels": {"vendor": e.vendor},
                "annotations": {"summary": summary, "vendor": e.vendor, "contact": e.vendorContact,
                                "scenario": "external resource, pulled by our collector"},
                "resolveConfiguration": {"autoResolved": True, "timeToResolve": "PT10M"}, "actions": []}

    return {"location": cfg.external.location, "tags": cfg.tags, "properties": {
        "description": f"Third-party API {e.vendor}, scraped by our collector", "scopes": [amw],
        "enabled": True, "interval": "PT1M", "rules": [
            rule("latency", f"vendor_api_latency_seconds{sel} > {e.latencySeconds}",
                 f"{e.vendor} API latency above {e.latencySeconds}s"),
            rule("errors", f"rate(vendor_api_errors_total{sel}[5m]) / rate(vendor_api_requests_total{sel}[5m]) "
                           f"> {e.errorRatio}", f"{e.vendor} API error ratio above {e.errorRatio:.0%}"),
            rule("down", f"up{sel} == 0", f"{e.vendor} metrics endpoint unreachable")]}}


def _attach_identity(c: arm.Client, vid: str, ident_id: str) -> None:
    """PATCH keeps any identity the VM already has; a no-op when attached."""
    vm = arm.get(c, vid, API_VM) or {}
    cur = vm.get("identity") or {}
    if ident_id.lower() in {k.lower() for k in (cur.get("userAssignedIdentities") or {})}:
        print(f"  identity already attached to {vid.rsplit('/', 1)[-1]}")
        return
    kinds = {t.strip() for t in cur.get("type", "").split(",") if t.strip() and t.strip() != "None"} | {"UserAssigned"}
    arm.call(c, "PATCH", vid, API_VM, {"identity": {
        "type": ", ".join(sorted(kinds)), "userAssignedIdentities": {ident_id: {}}}})
    if not c.dry_run:
        arm.wait(c, vid, API_VM)


def _script(cfg: Config, n: Names, write_url: str, client_id: str) -> str:
    e, root = cfg.external, cfg.root / "external"
    sim = base64.b64encode((root / "vendor_sim.py").read_bytes()).decode()
    head = "\n".join([f"PROM_VERSION='{e.prometheusVersion}'", f"VENDOR_PORT='{e.exporterPort}'",
                      f"VENDOR_NAME='{e.vendor}'", f"SCRAPE_INTERVAL='{e.scrapeInterval}'",
                      f"WRITE_URL='{write_url}'", f"CLIENT_ID='{client_id}'", f"COLLECTOR='{n.jump_vm}'",
                      f"VENDOR_SIM_B64='{sim}'"])
    # LF only: the script runs under bash on Linux.
    return "#!/bin/bash\n" + head + "\n" + (root / "setup.sh").read_text(encoding="utf-8").replace("\r\n", "\n")


def _run_setup(c: arm.Client, cfg: Config, n: Names, script: str) -> None:
    vid = jump.vm_id(c, cfg, n)
    rc = f"{vid}/runCommands/{RUN_COMMAND}"
    arm.put(c, rc, API_VM, {"location": net_of(cfg, cfg.foundry.network).location, "properties": {
        "source": {"script": script}, "asyncExecution": False, "timeoutInSeconds": 900,
        "treatFailureAsDeploymentFailure": True}}, timeout=1200)
    if c.dry_run:
        return
    iv = (arm.get(c, rc, f"{API_VM}&$expand=instanceView") or {}).get("properties", {}).get("instanceView", {})
    print(f"  run command {RUN_COMMAND}: {iv.get('executionState')} exit={iv.get('exitCode')}")
    for line in (iv.get("output") or "").strip().splitlines()[-12:]:
        print(f"    {line}")
    if iv.get("error"):
        print(f"    stderr: {iv['error'].strip()[-600:]}")


def up(c: arm.Client, cfg: Config, n: Names) -> None:
    e, en = cfg.external, names(cfg)
    rg, loc = e.resourceGroup, e.location
    print(f"== resource group {rg} (monitoring of resources outside Azure)")
    arm.put(c, arm.rg_id(c, rg), network.API_RG, {"location": loc, "tags": cfg.tags})

    print(f"== Azure Monitor workspace {en.amw} (managed Prometheus)")
    amw = arm.put(c, amw_id(c, cfg), API_AMW, {"location": loc, "tags": cfg.tags})
    write_url, dcr_id = ingestion(c, amw)
    print(f"  remote-write: POST {write_url.split('?')[0]}")
    print(f"  query:        GET {amw.get('properties', {}).get('metrics', {}).get('prometheusQueryEndpoint', '<query>')}"
          "/api/v1/query")
    me = arm.claims(c)["oid"]
    arm.role(c, amw_id(c, cfg), me, DATA_READER, ptype="User")

    print(f"== collector identity {en.identity}: Monitoring Metrics Publisher on the workspace's DCR")
    ident_id = arm.res_id(c, rg, "Microsoft.ManagedIdentity/userAssignedIdentities", en.identity)
    ident = arm.put(c, ident_id, API_MI, {"location": loc, "tags": cfg.tags})
    p = ident.get("properties", {})
    if not c.dry_run:
        arm.role(c, dcr_id, p["principalId"], METRICS_PUBLISHER)

    print(f"== collector on {n.jump_vm}: vendor-sim + Prometheus {e.prometheusVersion} (agent mode)")
    jump.ensure_running(c, cfg, n)
    _attach_identity(c, jump.vm_id(c, cfg, n), ident_id)
    _run_setup(c, cfg, n, _script(cfg, n, write_url, p.get("clientId", "<client-id>")))

    print(f"== Prometheus rule group {en.rules}: {en.alertPrefix}latency|errors|down (Sev{e.severity}, for {e.alertFor})")
    arm.put(c, rules_id(c, cfg), API_PRG, _rules(cfg, en, amw_id(c, cfg)))
    print(f"  next: python lab.py promql 'vendor_api_latency_seconds'  |  python lab.py vendor slow")


def vendor(c: arm.Client, cfg: Config, n: Names, mode: str | None) -> None:
    """ssh to the jump VM: set the vendor's mode (or just read it), then show its metrics."""
    j, ip, key = cfg.foundry.jump, jump.public_ip(c, cfg, n), jump.key_path(cfg)
    if not ip:
        raise arm.ArmError("no jump host public IP: python lab.py up jump")
    jump.ensure_running(c, cfg, n)
    cmd = f"echo {mode} | sudo tee {MODE_FILE} >/dev/null; " if mode else ""
    cmd += (f"echo mode=$(cat {MODE_FILE}); curl -s -w ' (HTTP %{{http_code}})\\n' "
            f"127.0.0.1:{cfg.external.exporterPort}/metrics | grep -v '^#'")
    r = subprocess.run(["ssh", "-p", str(j.sshPort), "-i", str(key), "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                        "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={key.parent / 'known_hosts'}",
                        f"{j.adminUser}@{ip}", cmd], capture_output=True, text=True, timeout=60)
    print(r.stdout.rstrip() or r.stderr.strip())
    if mode and mode != "ok":
        print(f"  alert {names(cfg).alertPrefix}... fires after {cfg.external.alertFor} of breach + up to 1 min "
              f"evaluation; python lab.py vendor ok to recover")


def promql(c: arm.Client, cfg: Config, query: str) -> None:
    amw = arm.get(c, amw_id(c, cfg), API_AMW)
    if not amw:
        raise arm.ArmError(f"{names(cfg).amw} not deployed: python lab.py up external")
    url = f"{amw['properties']['metrics']['prometheusQueryEndpoint']}/api/v1/query?query={quote(query)}"
    r = arm.data(c, "GET", url, QUERY_SCOPE, direct=True, rbac_wait=300)
    res = r.json().get("data", {}).get("result", [])
    if not res:
        print("  no series (remote-write lags ~1-2 min after the first scrape)")
    for s in res:
        labels = ",".join(f"{k}={v}" for k, v in s.get("metric", {}).items())
        ts, val = s.get("value", [0, "?"])
        print(f"  {{{labels}}} {val} @ {time.strftime('%H:%M:%S', time.localtime(float(ts)))}")
