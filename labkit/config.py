"""lab.yaml -> plain structs, lookups by name, and every resource name derived from it."""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class NsgRule:
    name: str
    priority: int
    direction: str
    port: str
    src: str = "*"
    dst: str = "*"
    protocol: str = "Tcp"
    access: str = "Allow"


@dataclass
class Subnet:
    name: str
    prefix: str
    delegation: str | None = None
    natGateway: bool = False
    nsgRules: list[NsgRule] = field(default_factory=list)


@dataclass
class Network:
    name: str
    resourceGroup: str
    location: str
    addressSpace: list[str]
    subnets: list[Subnet]
    dnsZones: list[str] = field(default_factory=list)  # created in this RG, linked to this VNet


@dataclass
class Peering:
    """A link between two networks; both directions are created."""
    between: list[str]
    allowForwardedTraffic: bool = False


@dataclass
class DnsLink:
    """Links a zone owned by one network to another network's VNet."""
    zone: str
    network: str


@dataclass
class Model:
    name: str
    version: str
    sku: str
    capacity: int


@dataclass
class Jump:
    subnet: str
    sshPort: int
    sshSource: str
    vmSize: str
    adminUser: str
    socksPort: int


@dataclass
class Foundry:
    network: str
    agentSubnet: str
    peSubnet: str
    project: str
    model: Model
    jump: Jump


@dataclass
class Gateway:
    subnet: str
    sku: str
    publisherEmail: str
    publisherName: str
    publicAccess: bool
    product: str
    tokenLimitPerMinute: int
    inferenceApiVersion: str
    logMessages: int = 32768   # LLM prompt/completion bytes per message -> ApiManagementGatewayLlmLog (0 = off)
    logBodies: int = 8192      # HTTP body bytes for the agent/MCP APIs -> ApiManagementGatewayLogs (0 = off)


@dataclass
class Runtime:
    searchSku: str
    capabilityHost: str


@dataclass
class Knowledge:
    index: str
    source: str
    base: str


@dataclass
class Agent:
    name: str
    source: str
    cpu: str
    memory: str


@dataclass
class PromptAgent:
    name: str
    tool: str


@dataclass
class Mcp:
    tool: str
    product: str
    subscription: str


@dataclass
class App:
    """The 'customer' web app: its own RG, mapped to the SRE Agent."""
    resourceGroup: str
    location: str
    sku: str
    pythonVersion: str
    source: str
    http5xxThreshold: int
    http5xxSeverity: int
    responseTimeSeconds: int
    responseTimeSeverity: int


@dataclass
class ResponsePlan:
    id: str
    priorities: list[str]
    agentMode: str
    maxAttempts: int
    merge: bool


@dataclass
class Sre:
    network: str
    subnet: str
    name: str
    accessLevel: str
    modelProvider: str
    remediationRoles: dict[str, str]
    connector: str
    skill: str
    handlingAgent: str
    responsePlan: ResponsePlan


@dataclass
class External:
    """A resource outside Azure (a vendor API), pulled into managed Prometheus by our collector,
    which runs on a VM in its own network (never peered, nothing shared with the Foundry part)."""
    resourceGroup: str
    location: str
    network: str
    subnet: str
    sshPort: int
    adminUser: str
    vmSize: str
    vendor: str
    vendorContact: str
    exporterPort: int
    scrapeInterval: str
    prometheusVersion: str
    latencySeconds: float
    errorRatio: float
    alertFor: str
    severity: int


@dataclass
class Config:
    tenantId: str
    subscriptionId: str
    namePrefix: str
    nameSeed: str
    location: str             # the lab's region; a network or app entry may override it
    tags: dict
    networks: list[Network]
    peerings: list[Peering]
    dnsLinks: list[DnsLink]
    foundry: Foundry
    gateway: Gateway
    runtime: Runtime
    knowledge: Knowledge
    agent: Agent
    promptAgent: PromptAgent
    mcp: Mcp
    app: App
    sre: Sre
    external: External
    providers: list[str] = field(default_factory=list)
    azureConfigDir: str = ""  # az profile folder from lab.local.yaml; AZURE_CONFIG_DIR wins
    browser: str = ""         # tunnel --browser: firefox | edge, from lab.local.yaml (empty: first found)
    root: Path = Path(".")   # folder of lab.yaml; relative paths (agent source, .ssh) resolve from here
    env: str = ""            # --env name: lab.<env>.yaml was applied over lab.yaml


def _network(raw: dict) -> Network:
    subnets = [Subnet(**{**s, "nsgRules": [NsgRule(**{**r, "port": str(r["port"])}) for r in s.get("nsgRules", [])]})
               for s in raw["subnets"]]
    return Network(**{**raw, "subnets": subnets})


def _target(path: Path) -> tuple[str, str, str, str]:
    """Tenant, subscription and az profile folder: AZURE_TENANT_ID / AZURE_SUBSCRIPTION_ID /
    AZURE_CONFIG_DIR, else lab.local.yaml next to lab.yaml (git-ignored, so the IDs never reach the repo)."""
    local = path.with_name("lab.local.yaml")
    raw = (yaml.safe_load(local.read_text(encoding="utf-8")) or {}) if local.exists() else {}
    tenant = os.environ.get("AZURE_TENANT_ID") or raw.get("tenantId")
    sub = os.environ.get("AZURE_SUBSCRIPTION_ID") or raw.get("subscriptionId")
    if not tenant or not sub:
        raise ValueError(f"tenantId/subscriptionId missing: copy lab.local.example.yaml to {local.name} "
                         "and fill it in, or set AZURE_TENANT_ID and AZURE_SUBSCRIPTION_ID")
    return (str(tenant), str(sub), os.environ.get("AZURE_CONFIG_DIR") or str(raw.get("azureConfigDir") or ""),
            str(raw.get("browser") or "").lower())


def _merge(base, over):
    """Overlay wins; dicts merge key by key, anything else (lists included) is replaced."""
    if isinstance(base, dict) and isinstance(over, dict):
        return {**base, **{k: _merge(base.get(k), v) for k, v in over.items()}}
    return over


def _fill(x, prefix: str):
    if isinstance(x, dict):
        return {k: _fill(v, prefix) for k, v in x.items()}
    if isinstance(x, list):
        return [_fill(v, prefix) for v in x]
    return x.replace("{namePrefix}", prefix) if isinstance(x, str) else x


def load(path: str | Path, env: str = "") -> Config:
    """lab.yaml, then lab.<env>.yaml over it (a second, separate instance: own prefix, so own
    RGs and names), then {namePrefix} filled in everywhere."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if env:
        over = Path(path).with_name(f"lab.{env}.yaml")
        if not over.exists():
            raise ValueError(f"--env {env}: {over.name} not found next to {Path(path).name}; create it with at "
                             f"least a namePrefix other than {raw['namePrefix']!r}")
        raw = _merge(raw, yaml.safe_load(over.read_text(encoding="utf-8")) or {})
        if raw["namePrefix"] == yaml.safe_load(Path(path).read_text(encoding="utf-8"))["namePrefix"]:
            raise ValueError(f"--env {env}: {over.name} must set a namePrefix other than {raw['namePrefix']!r}, "
                             "or it would deploy over the main lab")
        raw["tags"] = {**raw.get("tags", {}), "env": env}
    raw = _fill(raw, raw["namePrefix"])
    tenant, sub, azdir, browser = _target(Path(path).resolve())
    f = raw["foundry"]
    loc = raw["location"]
    cfg = Config(
        tenantId=tenant,
        subscriptionId=sub,
        namePrefix=raw["namePrefix"],
        nameSeed=str(raw["nameSeed"]),
        location=loc,
        tags=raw.get("tags", {}),
        networks=[_network({"location": loc, **n}) for n in raw["networks"]],
        peerings=[Peering(**p) for p in raw.get("peerings", [])],
        dnsLinks=[DnsLink(**d) for d in raw.get("dnsLinks", [])],
        foundry=Foundry(**{**f,
                           "model": Model(**{**f["model"], "version": str(f["model"]["version"])}),
                           "jump": Jump(**{**f["jump"], "sshPort": int(f["jump"]["sshPort"]),
                                           "sshSource": str(f["jump"]["sshSource"])})}),
        gateway=Gateway(**{**raw["gateway"], "inferenceApiVersion": str(raw["gateway"]["inferenceApiVersion"])}),
        runtime=Runtime(**raw["runtime"]),
        knowledge=Knowledge(**raw["knowledge"]),
        agent=Agent(**{**raw["agent"], "cpu": str(raw["agent"]["cpu"])}),
        promptAgent=PromptAgent(**raw["promptAgent"]),
        mcp=Mcp(**raw["mcp"]),
        app=App(**{"location": loc, **raw["app"], "pythonVersion": str(raw["app"]["pythonVersion"])}),
        sre=Sre(**{**raw["sre"], "responsePlan": ResponsePlan(**raw["sre"]["responsePlan"])}),
        external=External(**{"location": loc, **raw["external"],
                             "prometheusVersion": str(raw["external"]["prometheusVersion"]),
                             "sshPort": int(raw["external"]["sshPort"])}),
        providers=raw.get("providers", []),
        azureConfigDir=azdir,
        browser=browser,
        root=Path(path).resolve().parent,
        env=env,
    )
    check(cfg)
    return cfg


def check(cfg: Config) -> None:
    """Fail on dangling references before any API call is made."""
    for p in cfg.peerings:
        if len(p.between) != 2 or p.between[0] == p.between[1]:
            raise ValueError(f"lab.yaml: peering needs two different networks, got {p.between}")
        for name in p.between:
            network(cfg, name)
    for d in cfg.dnsLinks:
        network(cfg, d.network)
        if zone_owner(cfg, d.zone).name == d.network:
            raise ValueError(f"lab.yaml: {d.zone} is already linked to its owner {d.network}")
    fnet = network(cfg, cfg.foundry.network)
    for s in (cfg.foundry.agentSubnet, cfg.foundry.peSubnet, cfg.foundry.jump.subnet, cfg.gateway.subnet):
        subnet(fnet, s)
    if not (cfg.root / cfg.agent.source / "main.py").is_file():
        raise ValueError(f"lab.yaml: agent.source {cfg.agent.source!r} has no main.py")
    if not (cfg.root / cfg.app.source / "app.py").is_file():
        raise ValueError(f"lab.yaml: app.source {cfg.app.source!r} has no app.py")
    subnet(network(cfg, cfg.sre.network), cfg.sre.subnet)
    xnet = network(cfg, cfg.external.network)
    subnet(xnet, cfg.external.subnet)
    if xnet.resourceGroup != cfg.external.resourceGroup or any(xnet.name in p.between for p in cfg.peerings):
        raise ValueError(f"lab.yaml: network {xnet.name!r} is the vendor's own: resourceGroup must be "
                         f"external.resourceGroup and it must not be peered")
    for f in (cfg.sre.skill, cfg.sre.handlingAgent):
        if not (cfg.root / "sre" / f"{f}.md").is_file():
            raise ValueError(f"lab.yaml: sre {f!r} needs sre/{f}.md")


def network(cfg: Config, name: str) -> Network:
    for n in cfg.networks:
        if n.name == name:
            return n
    raise KeyError(f"lab.yaml: no network named {name!r}")


def subnet(net: Network, name: str) -> Subnet:
    for s in net.subnets:
        if s.name == name:
            return s
    raise KeyError(f"lab.yaml: network {net.name!r} has no subnet {name!r}")


def zone_owner(cfg: Config, zone: str) -> Network:
    for n in cfg.networks:
        if zone in n.dnsZones:
            return n
    raise KeyError(f"lab.yaml: no network owns DNS zone {zone!r}")


def suffix(cfg: Config, rg: str) -> str:
    """Hash of sub/RG/prefix/seed (like uniqueString(rg.id) in Bicep): re-runs and other
    commands derive the same names without storing state."""
    seed = f"{cfg.subscriptionId}|{rg}|{cfg.namePrefix}|{cfg.nameSeed}"
    return base64.b32encode(hashlib.sha256(seed.encode()).digest()).decode().lower()[:12]


def vnet_name(cfg: Config, net: Network) -> str:
    return f"vnet-{cfg.namePrefix.lower()}-{suffix(cfg, net.resourceGroup)}"


@dataclass
class Names:
    suffix: str
    vnet: str
    account: str        # project account (agents, network injection)
    model_account: str  # model-only account, reachable only through the gateway
    apim: str
    log: str
    storage: str
    cosmos: str
    search: str
    jump_vm: str
    jump_pip: str


def names(cfg: Config) -> Names:
    """Names of the Foundry-side resources; they share the Foundry network's suffix."""
    fnet = network(cfg, cfg.foundry.network)
    s = suffix(cfg, fnet.resourceGroup)
    p = cfg.namePrefix.lower()
    return Names(
        suffix=s,
        vnet=vnet_name(cfg, fnet),
        account=f"aif-{p}-{s}",
        model_account=f"aim-{p}-{s}",
        apim=f"apim-{p}-{s}",
        log=f"log-{p}-{s}",
        storage=f"st{p}{s}"[:24],
        cosmos=f"cosmos-{p}-{s}",
        search=f"srch-{p}-{s}",
        jump_vm=f"vm-{p}-jump",
        jump_pip=f"pip-{p}-jump",
    )
