"""SOCKS tunnel through the jump host. Private data planes (Foundry, Search, APIM) are
called with socks5h, so DNS is resolved on the jump host against the private zones.

up() reuses a tunnel already listening on socksPort (e.g. `python lab.py tunnel` in
another window); otherwise it starts `ssh -D` and stops it when this process exits."""

from __future__ import annotations

import atexit
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

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


EDGE_PATHS = [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
              r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"]


def _private(ip: str) -> bool:
    return ipaddress.ip_address(ip).is_private


def dns_check(c: arm.Client, cfg: Config, n: Names) -> None:
    """dig on the jump host: the Foundry data-plane name must resolve to the PE. Not an HTTP
    probe: the public front door answers too, so a 200 proves nothing."""
    j, ip, key = cfg.foundry.jump, jump.public_ip(c, cfg, n), jump.key_path(cfg)
    host = f"{n.account}.services.ai.azure.com"
    r = subprocess.run(["ssh", "-p", str(j.sshPort), "-i", str(key), "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                        "-o", f"UserKnownHostsFile={key.parent / 'known_hosts'}", f"{j.adminUser}@{ip}",
                        f"dig +short {host}"], capture_output=True, text=True, timeout=60)
    addrs = [x for x in r.stdout.split() if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", x)]
    if not addrs:
        print(f"  WARNING jump host could not resolve {host}: {r.stderr.strip()[-200:]}")
    elif _private(addrs[-1]):
        print(f"  jump host resolves {host} -> {addrs[-1]} (private)")
    else:
        print(f"  WARNING {host} -> {addrs[-1]} (public) on the jump host: privatelink zone link missing")


def edge(c: arm.Client, cfg: Config, n: Names, url: str) -> None:
    """Edge through the tunnel, in a throwaway profile: the proxy applies to the whole Edge
    process, and a running Edge would ignore --proxy-server. Chromium already sends hostnames
    to a socks5:// proxy; the MAP rule guarantees no local lookup (EXCLUDE keeps the proxy
    itself reachable). Popen quotes each list item, so the rule's spaces survive."""
    exe = shutil.which("msedge") or next((os.path.expandvars(x) for x in EDGE_PATHS
                                          if os.path.exists(os.path.expandvars(x))), None)
    if not exe:
        raise arm.ArmError("msedge.exe not found; point a browser at the SOCKS port with remote DNS")
    dns_check(c, cfg, n)
    port = cfg.foundry.jump.socksPort
    profile = Path(tempfile.gettempdir()) / f"{cfg.namePrefix}-jump-profile"
    subprocess.Popen([exe, f"--user-data-dir={profile}", f"--proxy-server=socks5://127.0.0.1:{port}",
                      "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
                      "--no-first-run", "--no-default-browser-check", url],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"  edge -> {url} via socks5://127.0.0.1:{port}, DNS on the jump host (profile {profile})")
