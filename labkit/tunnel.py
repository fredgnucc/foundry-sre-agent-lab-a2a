"""SOCKS tunnel through the jump host. Private data planes (Foundry, Search, APIM) are
called with socks5h, so DNS is resolved on the jump host against the private zones.

up() reuses a tunnel already listening on socksPort (e.g. `python lab.py tunnel` in
another window); otherwise it starts `ssh -D` and stops it when this process exits."""

from __future__ import annotations

import atexit
import ipaddress
import json
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


BROWSERS = {
    "firefox": ("firefox", [r"%ProgramFiles%\Mozilla Firefox\firefox.exe",
                            r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe"]),
    "edge": ("msedge", [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
                        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"]),
}

# Firefox profile prefs: SOCKS5 with remote DNS (names resolve on the jump host), no DoH that
# would bypass it, and no Windows SSO, so sign-in asks for the lab tenant's account.
FIREFOX_PREFS = {
    "network.proxy.type": 1, "network.proxy.socks": "127.0.0.1", "network.proxy.socks_version": 5,
    "network.proxy.socks_remote_dns": True, "network.proxy.no_proxies_on": "", "network.trr.mode": 5,
    "network.http.windows-sso.enabled": False, "browser.shell.checkDefaultBrowser": False,
    "browser.aboutwelcome.enabled": False, "datareporting.policy.dataSubmissionPolicyBypassNotification": True,
}


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


def _find(name: str) -> str | None:
    cmd, paths = BROWSERS[name]
    return shutil.which(cmd) or next((os.path.expandvars(x) for x in paths
                                      if os.path.exists(os.path.expandvars(x))), None)


def browser(c: arm.Client, cfg: Config, n: Names, url: str, name: str = "") -> None:
    """A browser through the tunnel, in a throwaway profile per lab, so its proxy and sign-in
    never touch your everyday browser. name: firefox | edge; empty picks the first one found."""
    names = [name] if name else list(BROWSERS)
    if name and name not in BROWSERS:
        raise arm.ArmError(f"browser {name!r} in lab.local.yaml: use one of {', '.join(BROWSERS)}")
    found = next(((b, exe) for b in names if (exe := _find(b))), None)
    if not found:
        raise arm.ArmError(f"{' or '.join(names)} not found; set browser: in lab.local.yaml, or point any "
                           f"browser at socks5://127.0.0.1:{cfg.foundry.jump.socksPort} with remote DNS")
    b, exe = found
    if "ai.azure.com" in url and "tid=" not in url:
        url += ("&" if "?" in url else "?") + f"tid={cfg.tenantId}"  # land in the lab tenant
    dns_check(c, cfg, n)
    port = cfg.foundry.jump.socksPort
    profile = Path(tempfile.gettempdir()) / f"{cfg.namePrefix}-jump-{b}"
    if b == "firefox":
        # -no-remote + its own profile: a separate Firefox even if one is already open.
        profile.mkdir(exist_ok=True)
        prefs = {**FIREFOX_PREFS, "network.proxy.socks_port": port}
        (profile / "user.js").write_text("".join(
            f'user_pref("{k}", {json.dumps(v)});\n'
            for k, v in prefs.items()), encoding="utf-8")
        args = [exe, "-no-remote", "-profile", str(profile), url]
    else:
        # The proxy applies to the whole Edge process, and a running Edge would ignore
        # --proxy-server, hence the own profile. The MAP rule guarantees no local lookup
        # (EXCLUDE keeps the proxy itself reachable). Popen quotes each item, so spaces survive.
        args = [exe, f"--user-data-dir={profile}", f"--proxy-server=socks5://127.0.0.1:{port}",
                "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
                "--no-first-run", "--no-default-browser-check", url]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"  {b} -> {url} via socks5://127.0.0.1:{port}, DNS on the jump host (profile {profile})")
