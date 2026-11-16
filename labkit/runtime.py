"""Layer 5 - Standard agent runtime: BYO Storage, Cosmos DB and AI Search (all private,
Entra only), project connections, the project identity's roles, the capability host,
then the Cosmos data-plane role on the containers the host just created.

Order is load-bearing: connections -> roles -> capability host -> Cosmos data role.
No account-level capability host: one is auto-created with networkInjections."""

from __future__ import annotations

import uuid

from labkit import arm, foundry, network
from labkit.config import Config, Names

API_ST = "2023-05-01"
API_COSMOS = "2024-11-15"
API_SRCH = "2025-05-01"
API_SRCH_SERVERLESS = "2026-09-01-preview"   # serverless SKU: preview API, no replicas/partitions
API_CONN = "2025-04-01-preview"
COG = "Microsoft.CognitiveServices/accounts"

STORAGE_BLOB_DATA_CONTRIBUTOR = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"
COSMOS_DB_OPERATOR = "230815da-be43-4aae-9cb4-875f7bd000aa"
SEARCH_INDEX_DATA_CONTRIBUTOR = "8ebe5a00-799e-43f5-93ac-243d3dce84a7"
SEARCH_SERVICE_CONTRIBUTOR = "7ca78c08-252a-4471-8644-bb5ff32d4ba0"
COSMOS_DATA_CONTRIBUTOR = "00000000-0000-0000-0000-000000000002"   # Cosmos SQL built-in role


def ids(c: arm.Client, cfg: Config, n: Names) -> dict[str, str]:
    rg = network.network(cfg, cfg.foundry.network).resourceGroup
    return {
        "storage": arm.res_id(c, rg, "Microsoft.Storage/storageAccounts", n.storage),
        "cosmos": arm.res_id(c, rg, "Microsoft.DocumentDB/databaseAccounts", n.cosmos),
        "search": arm.res_id(c, rg, "Microsoft.Search/searchServices", n.search),
        "project": arm.res_id(c, rg, COG, f"{n.account}/projects/{cfg.foundry.project}"),
    }


def search_url(n: Names) -> str:
    return f"https://{n.search}.search.windows.net"


def up(c: arm.Client, cfg: Config, n: Names) -> None:
    net = network.network(cfg, cfg.foundry.network)
    loc, tags = net.location, cfg.tags
    r = ids(c, cfg, n)

    def storage() -> None:
        print(f"== storage {n.storage} (files)")
        arm.put(c, r["storage"], API_ST, {"location": loc, "tags": tags, "kind": "StorageV2",
                                          "sku": {"name": "Standard_ZRS"}, "properties": {
            "minimumTlsVersion": "TLS1_2", "allowBlobPublicAccess": False, "publicNetworkAccess": "Disabled",
            "allowSharedKeyAccess": False,
            "networkAcls": {"bypass": "AzureServices", "defaultAction": "Deny", "virtualNetworkRules": []}}})
        foundry.private_endpoint(c, cfg, n, f"pe-{n.storage}-blob", r["storage"], "blob",
                                 ["privatelink.blob.core.windows.net"])

    def cosmos() -> None:
        print(f"== cosmos {n.cosmos} (threads)")
        # ensure: a re-PUT of an existing Cosmos account is a multi-minute no-op update.
        arm.ensure(c, r["cosmos"], API_COSMOS, {"location": loc, "tags": tags, "kind": "GlobalDocumentDB", "properties": {
            "databaseAccountOfferType": "Standard", "consistencyPolicy": {"defaultConsistencyLevel": "Session"},
            "disableLocalAuth": True, "publicNetworkAccess": "Disabled",
            "locations": [{"locationName": loc, "failoverPriority": 0, "isZoneRedundant": False}]}})
        foundry.private_endpoint(c, cfg, n, f"pe-{n.cosmos}-sql", r["cosmos"], "Sql",
                                 ["privatelink.documents.azure.com"])

    def search() -> None:
        sku = cfg.runtime.searchSku
        serverless = sku == "serverless"
        print(f"== search {n.search} ({sku}: vectors + the Foundry IQ knowledge base)")
        props = {
            # aadOrApiKey is what lets Foundry's AAD connection in; apiKeyOnly would 403 the agent.
            "disableLocalAuth": False,
            "authOptions": {"aadOrApiKey": {"aadAuthFailureMode": "http401WithBearerChallenge"}},
            "semanticSearch": "free",   # knowledge base retrieval ranks with the semantic ranker
            "publicNetworkAccess": "disabled", "networkRuleSet": {"bypass": "None", "ipRules": []}}
        if not serverless:
            props |= {"hostingMode": "default", "partitionCount": 1, "replicaCount": 1}
        arm.put(c, r["search"], API_SRCH_SERVERLESS if serverless else API_SRCH,
                {"location": loc, "tags": tags, "sku": {"name": sku},
                 "identity": {"type": "SystemAssigned"}, "properties": props})
        foundry.private_endpoint(c, cfg, n, f"pe-{n.search}", r["search"], "searchService",
                                 ["privatelink.search.windows.net"])

    arm.parallel(storage, cosmos, search)

    project = arm.get(c, r["project"], foundry.API_COG) or {}
    principal = project.get("identity", {}).get("principalId", "<project-principal>")

    print("== project connections (Entra auth)")
    conns = [(n.cosmos, "CosmosDB", f"https://{n.cosmos}.documents.azure.com:443/", r["cosmos"]),
             (n.storage, "AzureStorageAccount", f"https://{n.storage}.blob.core.windows.net/", r["storage"]),
             (n.search, "CognitiveSearch", search_url(n), r["search"])]
    arm.each(lambda x: arm.put(c, f"{r['project']}/connections/{x[0]}", API_CONN, {"properties": {
        "category": x[1], "target": x[2], "authType": "AAD",
        "metadata": {"ApiType": "Azure", "ResourceId": x[3], "location": loc}}}), conns)

    print("== project identity roles")
    arm.each(lambda x: arm.role(c, r[x[0]], principal, x[1]), [
        ("storage", STORAGE_BLOB_DATA_CONTRIBUTOR), ("cosmos", COSMOS_DB_OPERATOR),
        ("search", SEARCH_INDEX_DATA_CONTRIBUTOR), ("search", SEARCH_SERVICE_CONTRIBUTOR)])

    print(f"== capability host {cfg.runtime.capabilityHost} (immutable: created once)")
    arm.ensure(c, f"{r['project']}/capabilityHosts/{cfg.runtime.capabilityHost}", API_CONN, {"properties": {
        "capabilityHostKind": "Agents", "threadStorageConnections": [n.cosmos],
        "storageConnections": [n.storage], "vectorStoreConnections": [n.search]}}, timeout=3600)

    print("== Cosmos data role on enterprise_memory (exists only after the capability host)")
    name = uuid.uuid5(uuid.NAMESPACE_URL, f"{r['project']}|{r['cosmos']}|{COSMOS_DATA_CONTRIBUTOR}")
    arm.put(c, f"{r['cosmos']}/sqlRoleAssignments/{name}", API_COSMOS, {"properties": {
        "principalId": principal, "scope": f"{r['cosmos']}/dbs/enterprise_memory",
        "roleDefinitionId": f"{r['cosmos']}/sqlRoleDefinitions/{COSMOS_DATA_CONTRIBUTOR}"}})
