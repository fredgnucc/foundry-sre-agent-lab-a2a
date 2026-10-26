"""Layer 4 - AI gateway: APIM Standard v2 flagged isAIGateway, outbound VNet integration
in snet-apim, inbound through a private endpoint, logs to Log Analytics. Then the
association with Foundry (backend signed by APIM's identity, key API + product for
the account connection, keyless Entra API for hosted agents)."""

from __future__ import annotations

from pathlib import Path

from labkit import arm, foundry, network
from labkit.config import Config, Names

API_APIM = "2024-06-01-preview"
API_DIAG = "2021-05-01-preview"
API_CONN = "2025-04-01-preview"
API_LINK = "2016-09-01"
APIM = "Microsoft.ApiManagement/service"
COG = "Microsoft.CognitiveServices/accounts"
OPENAI_USER = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"   # Cognitive Services OpenAI User
VERBS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"]
TEMPLATES = Path(__file__).with_name("apim")


def apim_id(c: arm.Client, cfg: Config, n: Names) -> str:
    return arm.res_id(c, network.network(cfg, cfg.foundry.network).resourceGroup, APIM, n.apim)


def gateway_url(n: Names) -> str:
    return f"https://{n.apim}.azure-api.net"


def entra_url(n: Names) -> str:
    """Keyless Responses API the hosted agent calls with its own Entra token."""
    return f"{gateway_url(n)}/{n.account}-entra/v1"


def template(name: str, **subs: str) -> str:
    text = (TEMPLATES / name).read_text(encoding="utf-8")
    for k, v in subs.items():
        text = text.replace(f"__{k}__", v)
    return text


def link(c: arm.Client, scope: str, name: str, target: str) -> None:
    """Presentation only: makes the gateway show up in the Foundry portal's AI Gateway list."""
    arm.put(c, f"{scope}/providers/Microsoft.Resources/links/{name}", API_LINK, {"properties": {"targetId": target}})


def _service(c: arm.Client, cfg: Config, n: Names, public: str) -> dict:
    g = cfg.gateway
    net = network.network(cfg, cfg.foundry.network)
    return arm.put(c, apim_id(c, cfg, n), API_APIM, {
        "location": net.location, "tags": cfg.tags, "sku": {"name": g.sku, "capacity": 1},
        "identity": {"type": "SystemAssigned"},
        "properties": {
            "publisherEmail": g.publisherEmail, "publisherName": g.publisherName,
            "isAIGateway": True,   # write-only: never returned on GET
            "apiVersionConstraint": {"minApiVersion": "2019-12-01"},
            # External on v2 = outbound integration only; inbound is the private endpoint.
            "virtualNetworkType": "External",
            "virtualNetworkConfiguration": {"subnetResourceId": network.subnet_id(c, cfg, net.name, g.subnet)},
            "publicNetworkAccess": public}}, timeout=5400)


def _association(c: arm.Client, cfg: Config, n: Names, principal: str) -> None:
    g, f = cfg.gateway, cfg.foundry
    aid = apim_id(c, cfg, n)
    rg = network.network(cfg, f.network).resourceGroup
    acc, model_acc = arm.res_id(c, rg, COG, n.account), arm.res_id(c, rg, COG, n.model_account)
    backend = n.account

    print("== gateway -> model: role, backend (signed by APIM's managed identity)")
    arm.role(c, model_acc, principal, OPENAI_USER)
    # /openai is mandatory: APIM strips its API prefix and forwards /deployments/...
    arm.put(c, f"{aid}/backends/{backend}", API_APIM, {"properties": {
        "url": f"https://{n.model_account}.services.ai.azure.com/openai", "protocol": "http",
        "credentials": {"managedIdentity": {"resource": "https://ai.azure.com/"}},
        "tls": {"validateCertificateChain": False, "validateCertificateName": False}}})

    print(f"== key API /{n.account} (catch-all) + product {g.product}")
    api = f"{aid}/apis/{n.account}"
    arm.put(c, api, API_APIM, {"properties": {
        "displayName": n.account, "path": n.account, "protocols": ["https"], "subscriptionRequired": True,
        "subscriptionKeyParameterNames": {"header": "api-key", "query": "subscription-key"}}})
    arm.each(lambda v: arm.put(c, f"{api}/operations/{v.lower()}-default", API_APIM, {"properties": {
        "displayName": f"{v.lower()}-default", "method": v, "urlTemplate": "/*"}}), VERBS)
    arm.put(c, f"{api}/policies/policy", API_APIM, {"properties": {
        "format": "rawxml", "value": template("policy-key.xml", BACKEND=backend)}})
    product = f"{aid}/products/{g.product}"
    arm.put(c, product, API_APIM, {"properties": {
        "displayName": g.product, "state": "published", "subscriptionRequired": True, "approvalRequired": False}})
    # A product-API link is a synchronous PUT with no GET (405), so no wait.
    arm.call(c, "PUT", f"{product}/apis/{n.account}", API_APIM, {})
    sub = f"{aid}/subscriptions/{g.product}"
    arm.put(c, sub, API_APIM, {"properties": {"scope": product, "displayName": g.product, "state": "active"}})
    if g.tokenLimitPerMinute > 0:
        # The product holds the limit value, the API policy holds the engine.
        arm.put(c, f"{product}/policies/policy", API_APIM, {"properties": {"format": "rawxml", "value": (
            f'<policies><inbound><base /><set-variable name="tokenlimit-{f.model.name}" value="{g.tokenLimitPerMinute}" />'
            '</inbound><backend><base /></backend><outbound><base /></outbound><on-error><base /></on-error></policies>')}})

    print(f"== account connection {n.account} (admin-connected model through the gateway)")
    key = arm.post(c, f"{sub}/listSecrets", API_APIM).get("primaryKey", "<key>")
    arm.put(c, f"{acc}/connections/{n.account}", API_CONN, {"properties": {
        "authType": "ApiKey", "category": "ApiManagement", "isSharedToAll": True,
        "target": f"{gateway_url(n)}/{n.account}", "credentials": {"key": key},
        "metadata": {
            "deploymentInPath": "true", "inferenceAPIVersion": g.inferenceApiVersion,
            "authConfig": '{"type":"api_key","name":"api-key","format":"{api_key}"}', "customHeaders": "{}",
            "models": f'[{{"name":"{f.model.name}","properties":{{"model":{{"name":"{f.model.name}",'
                      f'"version":"{f.model.version}","format":"OpenAI"}}}}}}]'}}})

    print(f"== Entra API /{n.account}-entra (POST /v1/responses, no key: the hosted agent's path)")
    entra = f"{aid}/apis/{n.account}-entra"
    arm.put(c, entra, API_APIM, {"properties": {
        "displayName": f"{n.account}-entra", "path": f"{n.account}-entra", "protocols": ["https"],
        "subscriptionRequired": False}})
    arm.put(c, f"{entra}/operations/post-responses", API_APIM, {"properties": {
        "displayName": "post-responses", "method": "POST", "urlTemplate": "/v1/responses"}})
    arm.put(c, f"{entra}/policies/policy", API_APIM, {"properties": {
        "format": "rawxml", "value": template("policy-entra.xml", TENANT=arm.claims(c)["tid"], BACKEND=backend)}})

    print("== portal links")
    link(c, acc, f"aigw-{n.apim}", aid)
    link(c, aid, f"aigw-{n.account}", acc)
    link(c, f"{acc}/projects/{f.project}", f"aigw-{g.product}", product)


def up(c: arm.Client, cfg: Config, n: Names) -> None:
    g = cfg.gateway
    net = network.network(cfg, cfg.foundry.network)
    aid = apim_id(c, cfg, n)

    live = arm.get(c, aid, API_APIM)
    state = (live or {}).get("properties", {}).get("provisioningState", "")
    if live and state.lower() not in ("succeeded", "failed"):
        print(f"== APIM {n.apim} is {state}, waiting")
        live = arm.wait(c, aid, API_APIM, timeout=5400)
    elif not live or state.lower() == "failed":
        # Creation must be Enabled when a private endpoint will follow; Disabled comes after.
        print(f"== APIM {n.apim} {g.sku} (create takes 15-45 min)")
        live = _service(c, cfg, n, "Enabled")
    else:
        print(f"  exists {n.apim}")
    principal = (live or {}).get("identity", {}).get("principalId", "<apim-principal>")

    def pe() -> None:
        print(f"== APIM private endpoint (zone privatelink.azure-api.net)")
        foundry.private_endpoint(c, cfg, n, f"pe-{n.apim}", aid, "Gateway", ["privatelink.azure-api.net"])

    def diagnostics() -> None:
        # Dedicated = ApiManagementGatewayLogs with CallerIpAddress, not the AzureDiagnostics table.
        print("== APIM diagnostics -> Log Analytics (GatewayLogs, GatewayLlmLogs)")
        law = arm.res_id(c, net.resourceGroup, "Microsoft.OperationalInsights/workspaces", n.log)
        arm.put(c, f"{aid}/providers/Microsoft.Insights/diagnosticSettings/aigw-logs", API_DIAG, {"properties": {
            "workspaceId": law, "logAnalyticsDestinationType": "Dedicated",
            "logs": [{"category": "GatewayLogs", "enabled": True}, {"category": "GatewayLlmLogs", "enabled": True}]}})

    arm.parallel(pe, diagnostics, lambda: _association(c, cfg, n, principal))

    want = "Enabled" if g.publicAccess else "Disabled"
    have = (live or {}).get("properties", {}).get("publicNetworkAccess", want)
    if have != want:
        print(f"== APIM public network access {have} -> {want}")
        _service(c, cfg, n, want)
