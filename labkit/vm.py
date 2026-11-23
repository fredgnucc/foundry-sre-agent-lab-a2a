"""A small Ubuntu VM with a public IP and sshd on a non-22 port. Two of them, never sharing
a network: the jump host (Foundry VNet, Part 2) and the vendor VM (its own VNet, external)."""

from __future__ import annotations

import base64
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from labkit import arm, network
from labkit.config import Config

API_VM = "2024-07-01"
API_NET = network.API_NET

# LF only: a CRLF after '#cloud-config' makes cloud-init silently discard the document,
# and the VM boots with sshd on 22 while ARM still reports Succeeded.
CLOUD_INIT = """#cloud-config
package_update: true
packages: [dnsutils, curl]
write_files:
  - path: /etc/ssh/sshd_config.d/10-lab-jump.conf
    permissions: '0644'
    content: |
      Port {port}
      PasswordAuthentication no
      PermitRootLogin no
      AllowTcpForwarding yes
      GatewayPorts no
runcmd:
  # Ubuntu 24.04 socket-activates sshd; the generator reads the drop-in on daemon-reload.
  - systemctl daemon-reload
  - systemctl restart ssh.socket
"""


@dataclass
class Host:
    name: str           # VM name; the NIC is <name>-nic
    pip: str
    network: str        # lab.yaml network name: the VM's RG and region are that network's
    subnet: str
    sshPort: int
    adminUser: str
    vmSize: str
    layer: str          # the `up` layer that creates it, for error messages


def key_path(cfg: Config) -> Path:
    return cfg.root / ".ssh" / "id_ed25519"


def ssh_key(cfg: Config) -> str:
    """Project-local key pair (gitignored), created once."""
    key = key_path(cfg)
    if not key.exists():
        key.parent.mkdir(exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"{cfg.namePrefix}-lab", "-f", str(key)],
                       check=True)
    return key.with_suffix(".pub").read_text().strip()


def _rg(cfg: Config, h: Host) -> str:
    return network.network(cfg, h.network).resourceGroup


def vm_id(c: arm.Client, cfg: Config, h: Host) -> str:
    return arm.res_id(c, _rg(cfg, h), "Microsoft.Compute/virtualMachines", h.name)


def public_ip(c: arm.Client, cfg: Config, h: Host) -> str | None:
    pip = arm.get(c, arm.res_id(c, _rg(cfg, h), "Microsoft.Network/publicIPAddresses", h.pip), API_NET)
    return (pip or {}).get("properties", {}).get("ipAddress")


def power_state(c: arm.Client, vid: str) -> str:
    """GET {vm}/instanceView -> 'running', 'deallocated', 'stopped', 'starting', ... or 'missing'."""
    iv = arm.get(c, f"{vid}/instanceView", API_VM)
    if iv is None:
        return "missing"
    codes = [s.get("code", "") for s in iv.get("statuses", [])]
    return next((s.split("/", 1)[1] for s in codes if s.startswith("PowerState/")), "unknown")


def _last_stop(c: arm.Client, vid: str) -> str:
    """Who stopped it: the latest deallocate/powerOff in the activity log (last 2 days)."""
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 2 * 86400))
    flt = f"eventTimestamp ge '{since}' and resourceUri eq '{vid}'"
    r = arm._send(c, "GET", f"{arm.ARM}/subscriptions/{c.subscription}/providers/Microsoft.Insights/eventtypes/"
                            f"management/values?api-version=2015-04-01&$filter={quote(flt)}"
                            "&$select=eventTimestamp,operationName,caller,status")
    if r.status_code >= 400:
        return ""
    for e in arm.decode(r).get("value", []):   # newest first
        op = e.get("operationName", {}).get("value", "").lower()
        if op.endswith(("/deallocate/action", "/poweroff/action")) and e.get("status", {}).get("value") == "Succeeded":
            return f"{op.rsplit('/', 2)[-2]} at {e['eventTimestamp'][:19]}Z by {e.get('caller', '?')}"
    return ""


def ensure_running(c: arm.Client, cfg: Config, h: Host, timeout: int = 600) -> None:
    """Start the VM if it was stopped (e.g. by an auto-shutdown policy)."""
    vid = vm_id(c, cfg, h)
    state = power_state(c, vid)
    print(f"  vm {h.name}: {state}")
    if state == "missing" and c.dry_run:
        return
    if state == "missing":
        raise arm.ArmError(f"{h.name} not found: run `python lab.py up {h.layer}` first")
    if state in ("running", "starting"):
        return
    who = _last_stop(c, vid)
    if who:
        print(f"    last stop: {who}")
    if c.dry_run:
        return
    print(f"  POST {h.name}/start (ARM async op), then GET instanceView until running")
    arm.post(c, f"{vid}/start", API_VM)
    t0 = deadline = time.time()
    deadline += timeout
    last = state
    while (state := power_state(c, vid)) != "running":
        if time.time() > deadline:
            raise arm.ArmError(f"{h.name} still {state} after {timeout}s")
        if state != last:
            print(f"    ... {state} ({time.time() - t0:.0f}s)")
            last = state
        time.sleep(5)
    print(f"    running after {time.time() - t0:.0f}s; sshd needs a few more seconds to boot")


def _port_match(rule: dict, port: int) -> bool:
    p = rule["properties"]
    for r in p.get("destinationPortRanges") or [p.get("destinationPortRange", "")]:
        lo, _, hi = str(r).partition("-")
        if r == "*" or (lo.isdigit() and int(lo) <= port <= int(hi or lo)):
            return True
    return False


def preflight(c: arm.Client, cfg: Config, h: Host, fix: str) -> None:
    """GET the subnet and its live NSG; fail before any PUT if the subnet is missing or
    inbound TCP sshPort from the internet would be denied (first matching rule by priority wins)."""
    sid = network.subnet_id(c, cfg, h.network, h.subnet)
    sub = arm.get(c, sid, API_NET)
    if not sub:
        if c.dry_run:
            print(f"  preflight: {h.subnet} not deployed yet (dry-run), NSG check skipped")
            return
        raise arm.ArmError(f"subnet {h.subnet} not found: {fix}")
    nsg_ref = sub["properties"].get("networkSecurityGroup", {}).get("id")
    if not nsg_ref:
        print(f"  preflight: {h.subnet} has no NSG, port {h.sshPort} open")
        return
    nsg = arm.get(c, nsg_ref, API_NET) or {}
    rules = [r for r in nsg.get("properties", {}).get("securityRules", []) + nsg.get("properties", {}).get(
        "defaultSecurityRules", []) if r["properties"]["direction"] == "Inbound"
        and r["properties"]["protocol"] in ("Tcp", "*")
        and r["properties"].get("sourceAddressPrefix") not in ("VirtualNetwork", "AzureLoadBalancer")
        and _port_match(r, int(h.sshPort))]
    first = min(rules, key=lambda r: r["properties"]["priority"], default=None)
    name = nsg_ref.rsplit("/", 1)[-1]
    if not first or first["properties"]["access"] != "Allow":
        rule = f"{first['name']} (priority {first['properties']['priority']})" if first else "no rule"
        raise arm.ArmError(f"NSG {name} on {h.subnet} denies inbound TCP {h.sshPort}: {rule} wins; "
                           f"{fix} restores the lab.yaml rules, or check for a tenant policy")
    print(f"  preflight: {h.subnet} -> NSG {name}: TCP {h.sshPort} allowed by {first['name']} "
          f"(priority {first['properties']['priority']}, src {first['properties'].get('sourceAddressPrefix')})")


def create(c: arm.Client, cfg: Config, h: Host) -> None:
    net = network.network(cfg, h.network)
    rg, loc = net.resourceGroup, net.location
    pub = ssh_key(cfg)
    pip_id = arm.res_id(c, rg, "Microsoft.Network/publicIPAddresses", h.pip)
    arm.put(c, pip_id, API_NET, {"location": loc, "tags": cfg.tags, "sku": {"name": "Standard"},
                                 "properties": {"publicIPAllocationMethod": "Static"}})
    nic_id = arm.res_id(c, rg, "Microsoft.Network/networkInterfaces", f"{h.name}-nic")
    arm.put(c, nic_id, API_NET, {"location": loc, "tags": cfg.tags, "properties": {"ipConfigurations": [{
        "name": "ipconfig1", "properties": {
            "subnet": {"id": network.subnet_id(c, cfg, h.network, h.subnet)},
            "privateIPAllocationMethod": "Dynamic", "publicIPAddress": {"id": pip_id}}}]}})
    # ensure, not put: customData (cloud-init) and the key can't change after creation.
    arm.ensure(c, vm_id(c, cfg, h), API_VM, {
        "location": loc, "tags": cfg.tags, "properties": {
            "hardwareProfile": {"vmSize": h.vmSize},
            "storageProfile": {
                "imageReference": {"publisher": "Canonical", "offer": "ubuntu-24_04-lts", "sku": "server", "version": "latest"},
                "osDisk": {"createOption": "FromImage", "diskSizeGB": 32, "deleteOption": "Delete",
                           "managedDisk": {"storageAccountType": "StandardSSD_LRS"}}},
            "osProfile": {
                "computerName": h.name, "adminUsername": h.adminUser,
                "customData": base64.b64encode(CLOUD_INIT.format(port=h.sshPort).encode()).decode(),
                "linuxConfiguration": {"disablePasswordAuthentication": True, "ssh": {"publicKeys": [
                    {"path": f"/home/{h.adminUser}/.ssh/authorized_keys", "keyData": pub}]}}},
            "networkProfile": {"networkInterfaces": [{"id": nic_id, "properties": {"deleteOption": "Delete"}}]},
            "diagnosticsProfile": {"bootDiagnostics": {"enabled": True}}}})
    print(f"  ssh -p {h.sshPort} -i {key_path(cfg)} {h.adminUser}@{public_ip(c, cfg, h)}")


def delete(c: arm.Client, cfg: Config, h: Host) -> None:
    """VM (its NIC and OS disk go with it: deleteOption), then the public IP."""
    rg = _rg(cfg, h)
    arm.delete(c, vm_id(c, cfg, h), API_VM)
    arm.delete(c, arm.res_id(c, rg, "Microsoft.Network/networkInterfaces", f"{h.name}-nic"), API_NET)
    arm.delete(c, arm.res_id(c, rg, "Microsoft.Network/publicIPAddresses", h.pip), API_NET)


def ssh(cfg: Config, ip: str, h: Host, cmd: str, timeout: int = 60) -> subprocess.CompletedProcess:
    key = key_path(cfg)
    return subprocess.run(["ssh", "-p", str(h.sshPort), "-i", str(key), "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                           "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={key.parent / 'known_hosts'}",
                           f"{h.adminUser}@{ip}", cmd], capture_output=True, text=True, timeout=timeout)
