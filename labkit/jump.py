"""Layer 3 - jump host: a small Ubuntu VM in snet-jump whose only job is `ssh -D`, so
the laptop reaches the private data planes (Foundry, Search, APIM) from inside the VNet.
Bastion isn't used: tunnelling needs its Standard SKU."""

from __future__ import annotations

import base64
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

from labkit import arm, network
from labkit.config import Config, Names

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


def key_path(cfg: Config) -> Path:
    return cfg.root / ".ssh" / "id_ed25519"


def ssh_key(cfg: Config) -> str:
    """Project-local key pair (gitignored), created once."""
    key = key_path(cfg)
    if not key.exists():
        key.parent.mkdir(exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"{cfg.namePrefix}-jump", "-f", str(key)],
                       check=True)
    return key.with_suffix(".pub").read_text().strip()


def public_ip(c: arm.Client, cfg: Config, n: Names) -> str | None:
    rg = network.network(cfg, cfg.foundry.network).resourceGroup
    pip = arm.get(c, arm.res_id(c, rg, "Microsoft.Network/publicIPAddresses", n.jump_pip), API_NET)
    return (pip or {}).get("properties", {}).get("ipAddress")


def vm_id(c: arm.Client, cfg: Config, n: Names) -> str:
    rg = network.network(cfg, cfg.foundry.network).resourceGroup
    return arm.res_id(c, rg, "Microsoft.Compute/virtualMachines", n.jump_vm)


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


def ensure_running(c: arm.Client, cfg: Config, n: Names, timeout: int = 600) -> None:
    """Start the jump VM if it was stopped (e.g. by an auto-shutdown policy)."""
    vid = vm_id(c, cfg, n)
    state = power_state(c, vid)
    print(f"  jump vm {n.jump_vm}: {state}")
    if state == "missing":
        raise arm.ArmError(f"{n.jump_vm} not found: run `python lab.py up jump` first")
    if state in ("running", "starting"):
        return
    who = _last_stop(c, vid)
    if who:
        print(f"    last stop: {who}")
    if c.dry_run:
        return
    print(f"  POST {n.jump_vm}/start (ARM async op), then GET instanceView until running")
    arm.post(c, f"{vid}/start", API_VM)
    t0 = deadline = time.time()
    deadline += timeout
    last = state
    while (state := power_state(c, vid)) != "running":
        if time.time() > deadline:
            raise arm.ArmError(f"{n.jump_vm} still {state} after {timeout}s")
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


def preflight(c: arm.Client, cfg: Config) -> None:
    """GET the jump subnet and its live NSG; fail before any PUT if the subnet is missing or
    inbound TCP sshPort from the internet would be denied (first matching rule by priority wins)."""
    j = cfg.foundry.jump
    sid = network.subnet_id(c, cfg, cfg.foundry.network, j.subnet)
    sub = arm.get(c, sid, API_NET)
    if not sub:
        if c.dry_run:
            print(f"  preflight: {j.subnet} not deployed yet (dry-run), NSG check skipped")
            return
        raise arm.ArmError(f"subnet {j.subnet} not found: python lab.py up foundry-network")
    nsg_ref = sub["properties"].get("networkSecurityGroup", {}).get("id")
    if not nsg_ref:
        print(f"  preflight: {j.subnet} has no NSG, port {j.sshPort} open")
        return
    nsg = arm.get(c, nsg_ref, API_NET) or {}
    rules = [r for r in nsg.get("properties", {}).get("securityRules", []) + nsg.get("properties", {}).get(
        "defaultSecurityRules", []) if r["properties"]["direction"] == "Inbound"
        and r["properties"]["protocol"] in ("Tcp", "*")
        and r["properties"].get("sourceAddressPrefix") not in ("VirtualNetwork", "AzureLoadBalancer")
        and _port_match(r, int(j.sshPort))]
    first = min(rules, key=lambda r: r["properties"]["priority"], default=None)
    name = nsg_ref.rsplit("/", 1)[-1]
    if not first or first["properties"]["access"] != "Allow":
        rule = f"{first['name']} (priority {first['properties']['priority']})" if first else "no rule"
        raise arm.ArmError(f"NSG {name} on {j.subnet} denies inbound TCP {j.sshPort}: {rule} wins; "
                           f"python lab.py up foundry-network restores the lab.yaml rules, or check for a tenant policy")
    print(f"  preflight: {j.subnet} -> NSG {name}: TCP {j.sshPort} allowed by {first['name']} "
          f"(priority {first['properties']['priority']}, src {first['properties'].get('sourceAddressPrefix')})")


def up(c: arm.Client, cfg: Config, n: Names) -> None:
    j = cfg.foundry.jump
    net = network.network(cfg, cfg.foundry.network)
    rg, loc = net.resourceGroup, net.location
    print(f"== jump host {n.jump_vm}")
    preflight(c, cfg)
    pub = ssh_key(cfg)
    pip_id = arm.res_id(c, rg, "Microsoft.Network/publicIPAddresses", n.jump_pip)
    arm.put(c, pip_id, API_NET, {"location": loc, "tags": cfg.tags, "sku": {"name": "Standard"},
                                 "properties": {"publicIPAllocationMethod": "Static"}})
    nic_id = arm.res_id(c, rg, "Microsoft.Network/networkInterfaces", f"{n.jump_vm}-nic")
    arm.put(c, nic_id, API_NET, {"location": loc, "tags": cfg.tags, "properties": {"ipConfigurations": [{
        "name": "ipconfig1", "properties": {
            "subnet": {"id": network.subnet_id(c, cfg, net.name, j.subnet)},
            "privateIPAllocationMethod": "Dynamic", "publicIPAddress": {"id": pip_id}}}]}})
    # ensure, not put: customData (cloud-init) and the key can't change after creation.
    arm.ensure(c, arm.res_id(c, rg, "Microsoft.Compute/virtualMachines", n.jump_vm), API_VM, {
        "location": loc, "tags": cfg.tags, "properties": {
            "hardwareProfile": {"vmSize": j.vmSize},
            "storageProfile": {
                "imageReference": {"publisher": "Canonical", "offer": "ubuntu-24_04-lts", "sku": "server", "version": "latest"},
                "osDisk": {"createOption": "FromImage", "diskSizeGB": 32, "deleteOption": "Delete",
                           "managedDisk": {"storageAccountType": "StandardSSD_LRS"}}},
            "osProfile": {
                "computerName": n.jump_vm, "adminUsername": j.adminUser,
                "customData": base64.b64encode(CLOUD_INIT.format(port=j.sshPort).encode()).decode(),
                "linuxConfiguration": {"disablePasswordAuthentication": True, "ssh": {"publicKeys": [
                    {"path": f"/home/{j.adminUser}/.ssh/authorized_keys", "keyData": pub}]}}},
            "networkProfile": {"networkInterfaces": [{"id": nic_id, "properties": {"deleteOption": "Delete"}}]},
            "diagnosticsProfile": {"bootDiagnostics": {"enabled": True}}}})
    print(f"  ssh -p {j.sshPort} -i {key_path(cfg)} {j.adminUser}@{public_ip(c, cfg, n)}")
