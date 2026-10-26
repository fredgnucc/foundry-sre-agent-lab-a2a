"""Layer 3 - jump host: a small Ubuntu VM in snet-jump whose only job is `ssh -D`, so
the laptop reaches the private data planes (Foundry, Search, APIM) from inside the VNet.
Bastion isn't used: tunnelling needs its Standard SKU."""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

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


def up(c: arm.Client, cfg: Config, n: Names) -> None:
    j = cfg.foundry.jump
    net = network.network(cfg, cfg.foundry.network)
    rg, loc = net.resourceGroup, net.location
    print(f"== jump host {n.jump_vm}")
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
