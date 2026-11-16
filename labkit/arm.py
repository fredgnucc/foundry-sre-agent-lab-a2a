"""ARM REST, Go style: one struct (Client) and plain functions that take it.

Every call is printed as METHOD path -> status, so the output reads like the wire.
Long-running operations are waited on in one place, the way azure-core does it:
poll Azure-AsyncOperation for status, then GET the resource for its final state.
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Iterable

import requests

ARM = "https://management.azure.com"
_OK = {"succeeded"}
_FAILED = {"failed", "canceled", "cancelled"}
RETRY_STATUS = {429, 500, 502, 503, 504}
RETRIES = 6
# POSTs that read or ask rather than change: kept out of the change journal
_READ_POST = re.compile(r"/(list\w*|retrieve|search|responses|mcp|testconnection|get\w*)$", re.I)


class ArmError(Exception):
    pass


@dataclass
class Client:
    """Safe to share across threads: one HTTP session per thread, token cache under a lock."""
    subscription: str
    tenant: str | None = None   # tokens are requested for this tenant, whatever the az default is
    dry_run: bool = False
    credential: object = None
    tokens: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    local: threading.local = field(default_factory=threading.local)
    proxies: dict | None = None   # set by tunnel.up(): data-plane calls go through the jump host
    journal: str | None = None    # JSONL file: one line per change (PUT/PATCH/DELETE/changing POST)
    run: str = ""                 # run id written on every journal line


def record(c: Client, plane: str, method: str, target: str, status: int, api: str = "") -> None:
    """Append one change to c.journal; reads, read-only POSTs and dry-runs are skipped."""
    if not c.journal or c.dry_run or method == "GET" or (method == "POST" and _READ_POST.search(target)):
        return
    result = ("absent" if method == "DELETE" and status == 404 else "failed" if status >= 400
              else "deleted" if method == "DELETE" else "created" if status == 201 else "accepted" if status == 202
              else "updated" if method in ("PUT", "PATCH") else "done")
    line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "run": c.run, "tenant": c.tenant,
            "subscription": c.subscription, "plane": plane, "method": method, "target": target,
            **({"api": api} if api else {}), "status": status, "result": result}
    with c.lock, open(c.journal, "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")


def _session(c: Client, fresh: bool = False) -> requests.Session:
    if fresh or not hasattr(c.local, "session"):
        if hasattr(c.local, "session"):
            c.local.session.close()
        c.local.session = requests.Session()
    return c.local.session


def token(c: Client, scope: str = f"{ARM}/.default") -> str:
    with c.lock:
        cached = c.tokens.get(scope)
        if cached and cached[1] - 120 > time.time():
            return cached[0]
        if c.credential is None:
            if c.tenant and not _profile_has_tenant(c.tenant):
                raise ArmError(f"az profile {profile()} has no login for tenant {c.tenant}. Use the right "
                               f"profile (Remove-Item env:AZURE_CONFIG_DIR, or point it elsewhere) or run "
                               f"az login --tenant {c.tenant}")
            from azure.identity import DefaultAzureCredential

            c.credential = DefaultAzureCredential(exclude_interactive_browser_credential=True,
                                                  additionally_allowed_tenants=["*"])
        try:
            t = c.credential.get_token(scope, tenant_id=c.tenant) if c.tenant else c.credential.get_token(scope)
        except Exception as e:
            raise ArmError(f"no token for tenant {c.tenant} ({type(e).__name__}). Sign in to it in this az "
                           f"profile ({profile()}): az login --tenant {c.tenant}") from e
        c.tokens[scope] = (t.token, t.expires_on)
        return t.token


def each(fn: Callable, items: Iterable, workers: int = 8) -> None:
    """fn(item) for every item, concurrently: a goroutine per item plus a WaitGroup.
    Every item runs to the end; then the first error is raised and the others printed."""
    items = list(items)
    if len(items) < 2:
        for i in items:
            fn(i)
        return
    with ThreadPoolExecutor(min(workers, len(items))) as ex:
        futures = [ex.submit(fn, i) for i in items]
    errors = [f.exception() for f in futures if f.exception()]
    for e in errors[1:]:
        print(f"  also failed: {str(e).splitlines()[0]}")
    if errors:
        raise errors[0]


def parallel(*jobs: Callable[[], object]) -> None:
    """Run independent no-argument functions concurrently; same error rules as each()."""
    each(lambda job: job(), jobs)


def rg_id(c: Client, rg: str) -> str:
    return f"/subscriptions/{c.subscription}/resourceGroups/{rg}"


def res_id(c: Client, rg: str, rtype: str, name: str) -> str:
    """rtype like 'Microsoft.Network/virtualNetworks'; name may be 'parent/child/grandchild'."""
    return f"{rg_id(c, rg)}/providers/{rtype}/{name}"


def _short(c: Client, path: str) -> str:
    return path.replace(f"/subscriptions/{c.subscription}", "")


def call(c: Client, method: str, path: str, api: str, body: dict | None = None,
         ok404: bool = False, quiet: bool = False) -> requests.Response | None:
    """One HTTP request. Returns None in dry-run for writes; raises ArmError on >= 400."""
    if c.dry_run and method != "GET":
        print(f"  [dry-run] {method} {_short(c, path)}?api-version={api}")
        if body is not None:
            print("    " + json.dumps(body, indent=2).replace("\n", "\n    "))
        return None
    r = _send(c, method, f"{ARM}{path}?api-version={api}",
              None if body is None else json.dumps(body))
    record(c, "arm", method, path, r.status_code, api)
    if not quiet:
        print(f"  {method} {_short(c, path)} -> {r.status_code}")
    if r.status_code == 404 and ok404:
        return r
    if r.status_code >= 400:
        raise ArmError(f"{method} {_short(c, path)} -> {r.status_code}\n{r.text[:1500]}")
    return r


def _send(c: Client, method: str, url: str, data: str | None = None) -> requests.Response:
    """Idle pooled connections get reset (WinError 10054) during long waits, and ARM
    returns transient 429/5xx (e.g. 504 PolicyEvaluationTimeout). PUT, GET and DELETE
    are idempotent in ARM, so retry them; POST is not retried."""
    for attempt in range(RETRIES):
        last = attempt == RETRIES - 1 or method == "POST"
        try:
            r = _session(c).request(method, url, data=data, timeout=120, headers={
                "Authorization": f"Bearer {token(c)}", "Content-Type": "application/json"})
        except (requests.ConnectionError, requests.Timeout):
            if last:
                raise
            _session(c, fresh=True)
            time.sleep(2 ** attempt)
            continue
        if r.status_code not in RETRY_STATUS or last:
            return r
        print(f"  {method} {r.status_code}, retrying")
        time.sleep(int(r.headers.get("Retry-After", min(2 ** (attempt + 2), 60))))
    raise AssertionError("unreachable")


def decode(r: requests.Response) -> dict:
    """JSON body; APIM prefixes some with a UTF-8 BOM that r.json() rejects, and returns
    policies as raw XML (wrapped here as {"value": xml})."""
    if not r.content:
        return {}
    text = r.content.decode("utf-8-sig")
    return json.loads(text) if "json" in r.headers.get("Content-Type", "json") else {"value": text}


def get(c: Client, path: str, api: str) -> dict | None:
    r = call(c, "GET", path, api, ok404=True, quiet=True)
    return None if r.status_code == 404 else decode(r)


def put(c: Client, path: str, api: str, body: dict, timeout: int = 1800) -> dict:
    """Create or update, then block until provisioningState is terminal."""
    r = call(c, "PUT", path, api, body)
    if r is None:
        return {}
    op = r.headers.get("Azure-AsyncOperation")
    if op:
        _poll(c, op, path, timeout)
    return wait(c, path, api, timeout)


def ensure(c: Client, path: str, api: str, body: dict, timeout: int = 1800) -> dict:
    """PUT only if missing or not Succeeded. For resources with nothing that can drift
    (DNS zones, VNet links) where a re-PUT is a slow async no-op."""
    cur = get(c, path, api)
    if cur and cur.get("properties", {}).get("provisioningState", "").lower() == "succeeded":
        print(f"  exists {_short(c, path)}")
        return cur
    return put(c, path, api, body, timeout)


def post(c: Client, path: str, api: str, body: dict | None = None) -> dict:
    r = call(c, "POST", path, api, body if body is not None else {})
    return {} if r is None else decode(r)


def delete(c: Client, path: str, api: str, timeout: int = 3600) -> None:
    """DELETE, follow the async status URL, then GET until 404. Some providers (Foundry
    projects) report the operation done while the resource is still Deleting."""
    r = call(c, "DELETE", path, api, ok404=True)
    if r is None or r.status_code == 404:
        return
    op = r.headers.get("Azure-AsyncOperation") or r.headers.get("Location")
    if op and r.status_code not in (200, 204):
        _poll(c, op, f"DELETE {path}", timeout)
    deadline = time.time() + timeout
    while get(c, path, api) is not None:
        if time.time() > deadline:
            raise ArmError(f"{_short(c, path)} still exists {timeout}s after DELETE")
        time.sleep(10)


def wait(c: Client, path: str, api: str, timeout: int = 1800) -> dict:
    """GET until provisioningState is Succeeded (resources without one count as done)."""
    deadline, last = time.time() + timeout, ""
    while True:
        res = get(c, path, api) or {}
        state = str(res.get("properties", {}).get("provisioningState", "Succeeded" if res else "Missing"))
        if state.lower() in _OK:
            return res
        if state.lower() in _FAILED:
            raise ArmError(f"{_short(c, path)} provisioningState={state}\n{json.dumps(res)[:1500]}")
        if time.time() > deadline:
            raise ArmError(f"{_short(c, path)} still {state} after {timeout}s")
        if state != last:
            print(f"    ... {path.rsplit('/', 1)[-1]}: {state}")
            last = state
        time.sleep(15)


def _poll(c: Client, url: str, label: str, timeout: int) -> None:
    deadline, last = time.time() + timeout, ""
    while time.time() < deadline:
        r = _send(c, "GET", url)
        if r.status_code >= 400:
            raise ArmError(f"poll {label} -> {r.status_code}\n{r.text[:1500]}")
        status = decode(r).get("status") or ("InProgress" if r.status_code == 202 else "Succeeded")
        if status.lower() in _FAILED:
            raise ArmError(f"{label} {status}: {r.text[:1500]}")
        if status.lower() in _OK:
            return
        if status != last:
            print(f"    ... {label.rsplit('/', 1)[-1]}: {status}")
            last = status
        time.sleep(min(max(int(r.headers.get("Retry-After", 10)), 5), 30))
    raise ArmError(f"{label} not finished after {timeout}s")


API_ROLE = "2022-04-01"


def role(c: Client, scope: str, principal: str, role_def: str, ptype: str = "ServicePrincipal",
         timeout: int = 300) -> None:
    """Role assignment with a deterministic name (like guid() in Bicep), so re-runs are no-ops.
    A brand-new managed identity can take minutes to replicate: retry PrincipalNotFound."""
    name = uuid.uuid5(uuid.NAMESPACE_URL, f"{scope.lower()}|{principal}|{role_def}")
    path = f"{scope}/providers/Microsoft.Authorization/roleAssignments/{name}"
    if get(c, path, API_ROLE):
        return
    body = {"properties": {"principalId": principal, "principalType": ptype,
                           "roleDefinitionId": f"/subscriptions/{c.subscription}/providers/Microsoft.Authorization/roleDefinitions/{role_def}"}}
    deadline = time.time() + timeout
    while True:
        try:
            call(c, "PUT", path, API_ROLE, body)
            return
        except ArmError as e:
            if "RoleAssignmentExists" in str(e):
                return
            if "PrincipalNotFound" not in str(e) or time.time() > deadline:
                raise
            print("    ... principal not replicated yet")
            time.sleep(15)


def claims(c: Client) -> dict:
    """Claims of the signed-in caller's ARM token: oid (object ID), tid (tenant)."""
    payload = token(c).split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def profile() -> str:
    """The az CLI config folder in use: AZURE_CONFIG_DIR switches between tenants' logins."""
    return os.environ.get("AZURE_CONFIG_DIR") or os.path.join("~", ".azure")


def _profile_has_tenant(tenant: str) -> bool:
    """Local read of azureProfile.json. Asking az for a token in a tenant this profile never
    signed in to opens an interactive Windows sign-in (WAM) instead of failing."""
    f = os.path.join(os.path.expanduser(profile()), "azureProfile.json")
    try:
        with open(f, encoding="utf-8-sig") as fh:
            subs = json.load(fh).get("subscriptions", [])
    except (OSError, ValueError):
        return True   # unknown layout: let the token call decide
    return any(s.get("tenantId") == tenant for s in subs)


def preflight(c: Client) -> None:
    """Stop before any change unless the token is for c.tenant and c.subscription lives there.
    GET /subscriptions/{id} is a read that every subscription Reader can do."""
    tid = claims(c).get("tid")
    if tid != c.tenant:
        raise ArmError(f"token is for tenant {tid}, the lab expects {c.tenant}. In profile {profile()}: "
                       f"az login --tenant {c.tenant}")
    try:
        s = get(c, f"/subscriptions/{c.subscription}", "2022-12-01")
    except ArmError:
        s = None
    if not s or s.get("tenantId") != c.tenant:
        raise ArmError(f"subscription {c.subscription} not visible in tenant {c.tenant} "
                       f"(profile {profile()}); check lab.local.yaml / AZURE_SUBSCRIPTION_ID")
    print(f"target: tenant {c.tenant} | subscription {s.get('displayName')} ({c.subscription}) | "
          f"az profile {profile()}")


def data(c: Client, method: str, url: str, scope: str | None, body: dict | None = None, *,
         files: dict | None = None, content: bytes | None = None, headers: dict | None = None,
         ok: tuple = (), timeout: int = 120, rbac_wait: int = 0, quiet: bool = False,
         direct: bool = False) -> requests.Response:
    """Data-plane call (Foundry project, AI Search, APIM gateway) through c.proxies, the
    SOCKS tunnel: these endpoints are private. direct=True skips the tunnel for public data
    planes (Kudu, SRE Agent). content sends raw bytes instead of a JSON body.
    Raises ArmError on >= 400 unless the status is in ok. rbac_wait retries 401/403 that
    long: a new role takes minutes to apply."""
    deadline = time.time() + rbac_wait
    short = url.split("?")[0]
    attempt = -1
    while attempt < RETRIES - 1:
        attempt += 1
        h = {"Accept": "application/json", **({"Authorization": "Bearer " + token(c, scope)} if scope else {}),
             **(headers or {})}
        try:
            r = _session(c).request(method, url, json=body, data=content, files=files, headers=h,
                                    proxies=None if direct else c.proxies, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout):
            if attempt == RETRIES - 1 or method == "POST":
                raise
            _session(c, fresh=True)
            time.sleep(2 ** attempt)
            continue
        if not quiet:
            print(f"  {method} {short} -> {r.status_code}")
        if r.status_code in (401, 403) and time.time() < deadline:
            print("    ... waiting for the role assignment to apply")
            time.sleep(20)
            attempt -= 1
            continue
        if r.status_code in RETRY_STATUS and method != "POST" and attempt < RETRIES - 1:
            time.sleep(int(r.headers.get("Retry-After", min(2 ** (attempt + 2), 60))))
            continue
        if r.status_code >= 400 and r.status_code not in ok:
            record(c, "data", method, short, r.status_code)
            raise ArmError(f"{method} {short} -> {r.status_code}\n{r.text[:1500]}")
        record(c, "data", method, short, r.status_code)
        return r
    raise ArmError(f"{method} {short}: gave up")
