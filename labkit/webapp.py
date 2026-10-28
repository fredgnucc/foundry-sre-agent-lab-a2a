"""Layer 9 - the "customer" web app the SRE Agent is mapped to, in its own RG.

  Log Analytics + App Insights   the app's own telemetry
  plan + site                    Linux, Python, FAULT_MODE app setting = the demo's drift knob
  diagnostic settings            AppServiceHTTPLogs & co -> Log Analytics (the 5xx alert reads them)
  alerts                         5xx = log search alert, latency = metric alert; no action group:
                                 the SRE Agent picks up fired alerts in the RGs it manages
  publish                        zip app/ -> POST https://<scm>/api/zipdeploy (Kudu, Entra bearer)

A site PUT carries the app settings, so `up app` resets FAULT_MODE to none."""

from __future__ import annotations

import hashlib
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import requests

from labkit import agent, arm, network
from labkit.config import Config, suffix

API_WEB = "2024-04-01"
API_APPI = "2020-02-02"
API_DIAG = "2021-05-01-preview"
API_SQR = "2023-12-01"
API_METRIC = "2018-03-01"
API_TAGS = "2021-04-01"
SHA_TAG = "lab-app-sha"   # sha of the last published zip: unchanged code -> no publish
LOGS = ["AppServiceHTTPLogs", "AppServiceConsoleLogs", "AppServiceAppLogs", "AppServicePlatformLogs"]


@dataclass
class AppNames:
    plan: str
    site: str
    log: str
    appi: str
    alert5xx: str
    alertLatency: str
    alertPrefix: str   # what the response plan matches in alert titles


def names(cfg: Config) -> AppNames:
    p, s = cfg.namePrefix.lower(), suffix(cfg, cfg.app.resourceGroup)
    return AppNames(plan=f"asp-{p}-{s}", site=f"app-{p}-{s}", log=f"log-{p}-app-{s}", appi=f"appi-{p}-app-{s}",
                    alert5xx=f"alert-{p}-http-5xx", alertLatency=f"alert-{p}-latency", alertPrefix=f"alert-{p}-")


def site_id(c: arm.Client, cfg: Config) -> str:
    return arm.res_id(c, cfg.app.resourceGroup, "Microsoft.Web/sites", names(cfg).site)


def hosts(site: dict) -> tuple[str, str]:
    """(app URL, Kudu URL). New sites can get regional host names, so read both back."""
    p = site["properties"]
    scm = next(h["name"] for h in p["hostNameSslStates"] if h["hostType"] == "Repository")
    return f"https://{p['defaultHostName']}", f"https://{scm}"


def monitoring(c: arm.Client, cfg: Config, rg: str, location: str, log: str, appi: str) -> dict:
    """Workspace + workspace-based App Insights; returns the App Insights resource."""
    law = arm.res_id(c, rg, "Microsoft.OperationalInsights/workspaces", log)
    arm.put(c, law, network.API_LAW, {
        "location": location, "tags": cfg.tags,
        "properties": {"sku": {"name": "PerGB2018"}, "retentionInDays": 30}})
    return arm.put(c, arm.res_id(c, rg, "Microsoft.Insights/components", appi), API_APPI, {
        "location": location, "tags": cfg.tags, "kind": "web",
        "properties": {"Application_Type": "web", "WorkspaceResourceId": law}})


def _alerts(c: arm.Client, cfg: Config, an: AppNames, site: str) -> None:
    a, rg = cfg.app, cfg.app.resourceGroup
    # A metric alert on Http5xx (Total) never fired in the earlier lab; the log rows fire
    # reliably and give the agent the failing requests as evidence. On a fresh workspace the
    # table only exists minutes after the diagnostic setting: the rule PUT is a 400 until then.
    sqr = arm.res_id(c, rg, "Microsoft.Insights/scheduledQueryRules", an.alert5xx)
    body = {
        "location": a.location, "tags": cfg.tags, "properties": {
            "displayName": an.alert5xx, "description": "Web app is returning HTTP 5xx errors.",
            "severity": a.http5xxSeverity, "enabled": True, "scopes": [site],
            "evaluationFrequency": "PT1M", "windowSize": "PT5M", "autoMitigate": True,
            "criteria": {"allOf": [{
                "query": "AppServiceHTTPLogs | where ScStatus >= 500", "timeAggregation": "Count",
                "operator": "GreaterThan", "threshold": a.http5xxThreshold,
                "failingPeriods": {"numberOfEvaluationPeriods": 1, "minFailingPeriodsToAlert": 1}}]}}}
    deadline = time.time() + 900
    while True:
        try:
            arm.put(c, sqr, API_SQR, body)
            break
        except arm.ArmError as e:
            if "-> 400" not in str(e).splitlines()[0] or time.time() > deadline:
                raise
            print("    ... waiting for AppServiceHTTPLogs to exist in the workspace")
            time.sleep(30)
    arm.put(c, arm.res_id(c, rg, "Microsoft.Insights/metricAlerts", an.alertLatency), API_METRIC, {
        "location": "global", "tags": cfg.tags, "properties": {
            "description": "Web app average response time is too high.", "severity": a.responseTimeSeverity,
            "enabled": True, "scopes": [site], "evaluationFrequency": "PT1M", "windowSize": "PT5M",
            "autoMitigate": True, "criteria": {
                "odata.type": "Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria",
                "allOf": [{"criterionType": "StaticThresholdCriterion", "name": "ResponseTime",
                           "metricNamespace": "Microsoft.Web/sites", "metricName": "HttpResponseTime",
                           "operator": "GreaterThan", "threshold": a.responseTimeSeconds,
                           "timeAggregation": "Average"}]}}})


def publish(c: arm.Client, cfg: Config, force: bool = False) -> None:
    """Kudu zip deploy with an Entra token (works with basic auth disabled), async, then
    poll the deployment until complete. Skipped when the zip matches the site's sha tag."""
    sid = site_id(c, cfg)
    site = arm.get(c, sid, API_WEB)
    if not site:
        raise arm.ArmError(f"{sid.rsplit('/', 1)[-1]} not deployed: python lab.py up app")
    code = agent.build_zip(cfg.root / cfg.app.source)
    sha = hashlib.sha256(code).hexdigest()[:16]
    url, scm = hosts(site)
    if site.get("tags", {}).get(SHA_TAG) == sha and not force:
        print(f"  app code unchanged ({sha}), not publishing")
        return
    print(f"== publish {cfg.app.source}/ ({len(code)} bytes) -> {scm}/api/zipdeploy")
    for attempt in range(1, 4):
        d = _zipdeploy(c, scm, code)
        if d.get("status") == 4:   # Kudu DeployStatus: 3 Failed, 4 Success
            break
        # On a brand-new site the first build can race the site's own start and find an
        # empty source folder ("Could not detect any platform"); a retry succeeds.
        print(f"    zipdeploy failed (status {d.get('status')}, attempt {attempt}): {d.get('log_url')}")
        if attempt == 3:
            raise arm.ArmError(f"zipdeploy failed 3 times: {d.get('log_url')}")
        time.sleep(30)
    arm.call(c, "PATCH", f"{sid}/providers/Microsoft.Resources/tags/default", API_TAGS,
             {"operation": "Merge", "properties": {"tags": {SHA_TAG: sha}}})
    print(f"  GET {url}/ -> {probe(url + '/')}")


def _zipdeploy(c: arm.Client, scm: str, code: bytes, timeout: int = 900) -> dict:
    """POST the zip (async), then GET the deployment until complete; returns its record."""
    r = arm.data(c, "POST", f"{scm}/api/zipdeploy?isAsync=true", f"{arm.ARM}/.default", content=code,
                 headers={"Content-Type": "application/zip"}, direct=True, timeout=300)
    status = r.headers.get("Location") or f"{scm}/api/deployments/latest"
    deadline, last = time.time() + timeout, None
    while True:
        r = arm.data(c, "GET", status, f"{arm.ARM}/.default", direct=True, quiet=True, ok=(404,))
        d = r.json() if r.ok and r.content else {}
        if d.get("complete"):
            return d
        if d.get("progress") != last:
            last = d.get("progress")
            print(f"    ... {last or 'deploying'}")
        if time.time() > deadline:
            raise arm.ArmError(f"zipdeploy not complete after {timeout}s: {d}")
        time.sleep(10)


def probe(url: str, timeout: float = 30) -> str:
    """Status code (or 'timeout') of an anonymous GET on the public site."""
    try:
        return str(requests.get(url, timeout=timeout).status_code)
    except requests.RequestException:
        return "timeout"


def up(c: arm.Client, cfg: Config, n=None) -> None:
    a, an = cfg.app, names(cfg)
    rg = a.resourceGroup
    print(f"== resource group {rg} (the app's owner is not the SRE team)")
    arm.put(c, arm.rg_id(c, rg), network.API_RG, {"location": a.location, "tags": cfg.tags})

    print(f"== monitoring {an.log} + {an.appi}")
    appi = monitoring(c, cfg, rg, a.location, an.log, an.appi)
    appi_id = appi.get("id", "<appi-id>")
    conn = appi.get("properties", {}).get("ConnectionString", "<connection-string>")

    print(f"== plan {an.plan} ({a.sku}) + site {an.site}")
    plan = arm.put(c, arm.res_id(c, rg, "Microsoft.Web/serverfarms", an.plan), API_WEB, {
        "location": a.location, "tags": cfg.tags, "kind": "linux", "sku": {"name": a.sku},
        "properties": {"reserved": True}})
    sid = site_id(c, cfg)
    sha = ((arm.get(c, sid, API_WEB) or {}).get("tags") or {}).get(SHA_TAG)
    arm.put(c, sid, API_WEB, {
        "location": a.location, "kind": "app,linux",
        # hidden-link lets the portal and the SRE Agent pair the site with its telemetry.
        "tags": {**cfg.tags, "hidden-link: /app-insights-resource-id": appi_id, **({SHA_TAG: sha} if sha else {})},
        "properties": {"serverFarmId": plan.get("id", "<plan-id>"), "httpsOnly": True, "siteConfig": {
            "linuxFxVersion": f"PYTHON|{a.pythonVersion}", "alwaysOn": True, "healthCheckPath": "/health",
            "ftpsState": "Disabled", "minTlsVersion": "1.2", "appSettings": [
                {"name": "APPLICATIONINSIGHTS_CONNECTION_STRING", "value": conn},
                {"name": "SCM_DO_BUILD_DURING_DEPLOYMENT", "value": "true"},
                {"name": "FAULT_MODE", "value": "none"}]}}})

    print("== diagnostic settings + alerts")
    law = arm.res_id(c, rg, "Microsoft.OperationalInsights/workspaces", an.log)
    arm.put(c, f"{sid}/providers/Microsoft.Insights/diagnosticSettings/to-log-analytics", API_DIAG, {"properties": {
        "workspaceId": law, "logs": [{"category": x, "enabled": True} for x in LOGS],
        "metrics": [{"category": "AllMetrics", "enabled": True}]}})
    _alerts(c, cfg, an, sid)

    if not c.dry_run:
        publish(c, cfg)


# ---------- fault injection ----------

def _settings(c: arm.Client, sid: str) -> dict:
    return arm.post(c, f"{sid}/config/appsettings/list", API_WEB).get("properties", {})


def _set_mode(c: arm.Client, sid: str, url: str, mode: str) -> None:
    """POST config/appsettings/list, then PUT config/appsettings: the same two calls the
    portal makes, so the Activity Log shows a config write followed by a restart."""
    settings = _settings(c, sid)
    arm.call(c, "PUT", f"{sid}/config/appsettings", API_WEB, {"properties": {**settings, "FAULT_MODE": mode}})
    print(f"  FAULT_MODE={mode}; waiting for the restart")
    deadline = time.time() + 240
    while time.time() < deadline:
        time.sleep(10)
        try:
            r = requests.get(url + "/", timeout=15)
            current = r.json().get("fault_mode") if r.ok else ("errors" if r.status_code == 500 else None)
        except (requests.RequestException, ValueError):
            current = None
        if current == mode:
            return
    print("  warning: the app did not report the new mode within 4 min; continuing")


def traffic(url: str, minutes: float, workers: int) -> Counter:
    print(f"== GET / and /api/orders for {minutes} min with {workers} workers -> {url}")
    end = time.monotonic() + minutes * 60
    paths = ["/", "/api/orders"]

    def worker(i: int) -> Counter:
        codes, k = Counter(), i
        while time.monotonic() < end:
            codes[probe(url + paths[k % 2], timeout=15)] += 1
            k += 1
        return codes

    with ThreadPoolExecutor(workers) as pool:
        return sum(pool.map(worker, range(workers)), Counter())


def fault(c: arm.Client, cfg: Config, mode: str, minutes: float, workers: int, traffic_only: bool) -> None:
    sid = site_id(c, cfg)
    site = arm.get(c, sid, API_WEB)
    if not site:
        raise arm.ArmError(f"{names(cfg).site} not deployed: python lab.py up app")
    url, _ = hosts(site)
    if not traffic_only:
        _set_mode(c, sid, url, mode)
        if mode == "none":
            print(f"  healthy: {url}")
            return
    codes = traffic(url, minutes, workers)
    print("  " + ", ".join(f"{k}: {v}" for k, v in sorted(codes.items())))
    if any(k != "200" for k in codes):
        print("  alerts fire within ~5 min; the incident shows up at https://sre.azure.com")
