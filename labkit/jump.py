"""Layer 3 - jump host: a small Ubuntu VM in snet-jump whose only job is `ssh -D`, so
the laptop reaches the private data planes (Foundry, Search, APIM) from inside the VNet.
Bastion isn't used: tunnelling needs its Standard SKU. Part 2 only: the external vendor
has its own VM (labkit/external.py)."""

from __future__ import annotations

from labkit import arm, vm
from labkit.config import Config, Names

API_VM = vm.API_VM
key_path = vm.key_path
power_state = vm.power_state


def host(cfg: Config, n: Names) -> vm.Host:
    j = cfg.foundry.jump
    return vm.Host(name=n.jump_vm, pip=n.jump_pip, network=cfg.foundry.network, subnet=j.subnet, sshPort=int(j.sshPort),
                   adminUser=j.adminUser, vmSize=j.vmSize, layer="jump")


def public_ip(c: arm.Client, cfg: Config, n: Names) -> str | None:
    return vm.public_ip(c, cfg, host(cfg, n))


def vm_id(c: arm.Client, cfg: Config, n: Names) -> str:
    return vm.vm_id(c, cfg, host(cfg, n))


def ensure_running(c: arm.Client, cfg: Config, n: Names, timeout: int = 600) -> None:
    vm.ensure_running(c, cfg, host(cfg, n), timeout)


def up(c: arm.Client, cfg: Config, n: Names) -> None:
    h = host(cfg, n)
    print(f"== jump host {h.name}")
    vm.preflight(c, cfg, h, "python lab.py up foundry-network")
    vm.create(c, cfg, h)
