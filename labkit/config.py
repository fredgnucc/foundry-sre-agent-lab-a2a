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
    responsePlan: ResponsePlan


@dataclass
class Config:
    subscriptionId: str
    namePrefix: str
    nameSeed: str
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
    root: Path = Path(".")   # folder of lab.yaml; relative paths (agent source, .ssh) resolve from here


def _network(raw: dict) -> Network:
    subnets = [Subnet(**{**s, "nsgRules": [NsgRule(**{**r, "port": str(r["port"])}) for r in s.get("nsgRules", [])]})
               for s in raw["subnets"]]
    return Network(**{**raw, "subnets": subnets})


def load(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    f = raw["foundry"]
    cfg = Config(
        subscriptionId=os.environ.get("AZURE_SUBSCRIPTION_ID") or raw["subscriptionId"],
        namePrefix=raw["namePrefix"],
        nameSeed=str(raw["nameSeed"]),
        tags=raw.get("tags", {}),
        networks=[_network(n) for n in raw["networks"]],
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
        app=App(**{**raw["app"], "pythonVersion": str(raw["app"]["pythonVersion"])}),
        sre=Sre(**{**raw["sre"], "responsePlan": ResponsePlan(**raw["sre"]["responsePlan"])}),
        root=Path(path).resolve().parent,
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
    if not (cfg.root / "sre" / f"{cfg.sre.skill}.md").is_file():
        raise ValueError(f"lab.yaml: sre.skill {cfg.sre.skill!r} needs sre/{cfg.sre.skill}.md")


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
