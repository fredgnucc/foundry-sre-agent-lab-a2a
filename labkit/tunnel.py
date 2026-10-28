"""SOCKS tunnel through the jump host. Private data planes (Foundry, Search, APIM) are
called with socks5h, so DNS is resolved on the jump host against the private zones.

up() reuses a tunnel already listening on socksPort (e.g. `python lab.py tunnel` in
another window); otherwise it starts `ssh -D` and stops it when this process exits."""

from __future__ import annotations

import atexit
import socket
import subprocess
import time

from labkit import arm, jump
from labkit.config import Config, Names


def listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def start(c: arm.Client, cfg: Config, n: Names, timeout: int = 300) -> subprocess.Popen | None:
    """Start ssh -D (None if a tunnel is already up). Retries while sshd is still booting."""
    j = cfg.foundry.jump
    if listening(j.socksPort):
        return None
    ip = jump.public_ip(c, cfg, n)
    if not ip:
        raise arm.ArmError("no jump host public IP: run `python lab.py up jump` first")
    jump.ensure_running(c, cfg, n)
    key = jump.key_path(cfg)
    cmd = ["ssh", "-N", "-D", f"127.0.0.1:{j.socksPort}", "-p", str(j.sshPort), "-i", str(key),
           "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={key.parent / 'known_hosts'}",
           "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30", "-o", "BatchMode=yes",
           "-o", "ConnectTimeout=10", f"{j.adminUser}@{ip}"]
    print(f"  ssh -N -D 127.0.0.1:{j.socksPort} -p {j.sshPort} {j.adminUser}@{ip} (key {key.name})")
    t0 = time.time()
    deadline = t0 + timeout
    attempt = 0
    while True:
        attempt += 1
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        for _ in range(60):
            if listening(j.socksPort):
                print(f"  tunnel up: socks5h://127.0.0.1:{j.socksPort} via {ip}:{j.sshPort} "
                      f"(attempt {attempt}, {time.time() - t0:.0f}s)")
                return proc
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        err = proc.stderr.read().strip() if proc.poll() is not None else "no listener after 30s"
        proc.kill()
        if time.time() > deadline:
            raise arm.ArmError(f"ssh tunnel to {ip}:{j.sshPort} failed: {err}\n"
                               f"  jump vm power state: {jump.power_state(c, jump.vm_id(c, cfg, n))}")
        reason = err.splitlines()[-1] if err else "exited"
        if attempt % 4 == 0:   # still failing: re-check the VM rather than retry blindly
            reason += f"; vm {jump.power_state(c, jump.vm_id(c, cfg, n))}"
        print(f"    ... attempt {attempt} ({time.time() - t0:.0f}s): {reason}; retrying in 15s")
        time.sleep(15)


def up(c: arm.Client, cfg: Config, n: Names) -> None:
    """Route this client's data-plane calls through the tunnel, starting it if needed."""
    if c.proxies:
        return
    proc = start(c, cfg, n)
    if proc:
        atexit.register(proc.kill)
    url = f"socks5h://127.0.0.1:{cfg.foundry.jump.socksPort}"
    c.proxies = {"https": url, "http": url}
