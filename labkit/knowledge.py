"""Layer 6 - Foundry IQ knowledge base about the lab itself.

Documents are generated from lab.yaml plus live ARM state (private endpoint IPs, APIM
APIs, account settings), so the knowledge base describes what is deployed, not what was
planned. Pipeline: index (keyword + semantic) -> searchIndex knowledge source ->
knowledge base. GA API 2026-04-01: extractive retrieval, no LLM inside Search.

Search is private: everything here goes through the SOCKS tunnel."""

from __future__ import annotations

import json

from labkit import arm, foundry, gateway, network, runtime, tunnel, webapp
from labkit.config import Config, Names, vnet_name

API = "2026-04-01"
SCOPE = "https://search.azure.com/.default"
SEARCH_INDEX_DATA_CONTRIBUTOR = runtime.SEARCH_INDEX_DATA_CONTRIBUTOR
SEARCH_SERVICE_CONTRIBUTOR = runtime.SEARCH_SERVICE_CONTRIBUTOR

INDEX_FIELDS = [
    {"name": "id", "type": "Edm.String", "key": True, "filterable": True},
    {"name": "title", "type": "Edm.String", "searchable": True},
    {"name": "category", "type": "Edm.String", "searchable": True, "filterable": True, "facetable": True},
    {"name": "content", "type": "Edm.String", "searchable": True},
]
SEMANTIC = {"defaultConfiguration": "default", "configurations": [{"name": "default", "prioritizedFields": {
    "titleField": {"fieldName": "title"}, "prioritizedContentFields": [{"fieldName": "content"}],
    "prioritizedKeywordsFields": [{"fieldName": "category"}]}}]}


def _doc(id_: str, category: str, title: str, *lines: str) -> dict:
    return {"id": id_, "category": category, "title": title, "content": "\n".join(lines)}


def _overview(cfg: Config, n: Names) -> dict:
    f = cfg.foundry
    return _doc("overview", "overview", "Lab overview: foundry-to-sre-agent",
                f"This lab ({cfg.namePrefix}, seed {cfg.nameSeed}) connects an Azure SRE Agent to a private Microsoft Foundry "
                "agent through an AI gateway (Azure API Management). Everything on the Foundry side is private: "
                "public network access is disabled on the Foundry accounts, AI Search, Cosmos DB, Storage and APIM; "
                "callers reach them through private endpoints over VNet peering.",
                f"Networks: {', '.join(f'{x.name} ({x.addressSpace[0]}, {x.location}, RG {x.resourceGroup})' for x in cfg.networks)}.",
                f"Foundry: project account {n.account} (project {f.project}, agent compute injected into {f.agentSubnet}) "
                f"and model-only account {n.model_account} (deployment {f.model.name} {f.model.version}, {f.model.sku}).",
                f"AI gateway: {n.apim} ({cfg.gateway.sku}). The model account's only caller is the gateway's managed identity.",
                f"Hosted agent: {cfg.agent.name}, grounded on knowledge base {cfg.knowledge.base} in {n.search}.",
                f"Prompt agent: {cfg.promptAgent.name}, the same architect as a definition only, grounded on index "
                f"{cfg.knowledge.index}.",
                f"The SRE Agent calls them as MCP tools {cfg.mcp.tool} (hosted) and {cfg.promptAgent.tool} (prompt) at "
                f"{gateway.gateway_url(n)}/{n.account}-sre-mcp/mcp with a subscription key.",
                "Deployed layer by layer with python lab.py up <layer> (raw ARM REST, no Bicep, no az CLI).")


def _networks(cfg: Config) -> list[dict]:
    docs = []
    for net in cfg.networks:
        lines = [f"Network {net.name}: VNet {vnet_name(cfg, net)} in {net.location}, resource group {net.resourceGroup}, "
                 f"address space {', '.join(net.addressSpace)}."]
        for s in net.subnets:
            extra = [f"delegated to {s.delegation}"] if s.delegation else []
            extra += ["NAT gateway for outbound internet, default outbound access off"] if s.natGateway else []
            rules = [f"{r.direction} {r.access} {r.protocol} {r.src}->{r.dst}:{r.port} ({r.name}, priority {r.priority})"
                     for r in s.nsgRules]
            lines.append(f"Subnet {s.name} {s.prefix}" + (f", {', '.join(extra)}" if extra else "") +
                         f". NSG rules: {'; '.join(rules) if rules else 'none beyond Azure defaults'}.")
        if net.dnsZones:
            lines.append(f"Private DNS zones owned by this network (in {net.resourceGroup}, linked to its VNet): "
                         f"{', '.join(net.dnsZones)}.")
        docs.append(_doc(f"network-{net.name}", "network", f"Network {net.name}: subnets, NSGs, DNS", *lines))
    links = [f"Peering {p.between[0]} <-> {p.between[1]} (both directions, allowForwardedTraffic={p.allowForwardedTraffic}). "
             "Peering is non-transitive and carries TCP and UDP alike." for p in cfg.peerings]
    links += [f"DNS link: zone {d.zone} (owned by {network.zone_owner(cfg, d.zone).name}) is linked to network {d.network}, "
              f"so {d.network} resolves those private endpoint names to private IPs." for d in cfg.dnsLinks]
    docs.append(_doc("network-links", "network", "Peerings and cross-network DNS links", *links))
    return docs


def _private_endpoints(c: arm.Client, cfg: Config) -> dict:
    rg = network.network(cfg, cfg.foundry.network).resourceGroup
    pes = (arm.get(c, f"{arm.rg_id(c, rg)}/providers/Microsoft.Network/privateEndpoints", network.API_NET) or {}).get("value", [])
    lines = ["Private endpoints in the Foundry PE subnet. Each name resolves to its private IP through the "
             "privatelink zones; from the internet the same names are refused (public access disabled)."]
    for pe in sorted(pes, key=lambda x: x["name"]):
        p = pe["properties"]
        conn = (p.get("privateLinkServiceConnections") or [{}])[0].get("properties", {})
        target = conn.get("privateLinkServiceId", "?").rsplit("/", 1)[-1]
        # With DNS zone groups customDnsConfigs is empty: the IPs and FQDNs live on the PE's NIC.
        nics = [arm.get(c, nic["id"], network.API_NET) or {} for nic in p.get("networkInterfaces", [])]
        dns = "; ".join(
            f"{','.join(ip['properties'].get('privateLinkConnectionProperties', {}).get('fqdns', [])) or '?'}"
            f" -> {ip['properties'].get('privateIPAddress')}"
            for nic in nics for ip in nic.get("properties", {}).get("ipConfigurations", []))
        lines.append(f"{pe['name']}: target {target}, group {','.join(conn.get('groupIds', []))}, "
                     f"state {conn.get('privateLinkServiceConnectionState', {}).get('status', '?')}. DNS: {dns or 'n/a'}.")
    return _doc("private-endpoints", "network", "Private endpoints and their private IPs", *lines)


def _accounts(c: arm.Client, cfg: Config, n: Names) -> dict:
    rg = network.network(cfg, cfg.foundry.network).resourceGroup
    lines = []
    for name in (n.account, n.model_account):
        acc = arm.get(c, arm.res_id(c, rg, "Microsoft.CognitiveServices/accounts", name), foundry.API_COG) or {}
        p = acc.get("properties", {})
        inj = [x["subnetArmId"].rsplit("/", 1)[-1] for x in p.get("networkInjections") or []]
        lines.append(f"Account {name}: {p.get('provisioningState', 'not deployed')}, publicNetworkAccess "
                     f"{p.get('publicNetworkAccess')}, local auth {'disabled (Entra only)' if p.get('disableLocalAuth') else 'enabled'}, "
                     f"agent network injection {inj or 'none'}, endpoint https://{name}.services.ai.azure.com.")
    m = cfg.foundry.model
    lines += [f"Model deployment {m.name} version {m.version}, SKU {m.sku}, capacity {m.capacity}, lives on {n.model_account} only.",
              f"Project {cfg.foundry.project} endpoint: https://{n.account}.services.ai.azure.com/api/projects/{cfg.foundry.project}.",
              "Local auth is disabled (disableLocalAuth=true) on the Foundry accounts: keys never work, "
              "every caller uses Microsoft Entra tokens (audience https://ai.azure.com)."]
    return _doc("foundry-accounts", "foundry", "Foundry accounts, project and model", *lines)


def _gateway(c: arm.Client, cfg: Config, n: Names) -> dict:
    aid = gateway.apim_id(c, cfg, n)
    svc = arm.get(c, aid, gateway.API_APIM) or {}
    p = svc.get("properties", {})
    apis = (arm.get(c, f"{aid}/apis", gateway.API_APIM) or {}).get("value", [])
    lines = [f"APIM {n.apim}: SKU {svc.get('sku', {}).get('name', cfg.gateway.sku)}, state {p.get('provisioningState', 'not deployed')}, "
             f"publicNetworkAccess {p.get('publicNetworkAccess')}, outbound VNet integration in {cfg.gateway.subnet} "
             f"(virtualNetworkType {p.get('virtualNetworkType')}), inbound only through private endpoint pe-{n.apim}. "
             f"Gateway URL {gateway.gateway_url(n)}. Flagged isAIGateway.",
             f"APIM's system-assigned managed identity (principal {svc.get('identity', {}).get('principalId')}) holds "
             f"Cognitive Services OpenAI User on {n.model_account} and Foundry Agent Consumer on project {cfg.foundry.project}."]
    for a in sorted(apis, key=lambda x: x["name"]):
        ap = a["properties"]
        lines.append(f"API {a['name']}: path /{ap.get('path')}, type {ap.get('type') or 'http'}, "
                     f"subscription key {'required' if ap.get('subscriptionRequired') else 'not required'}.")
    lines += [f"/{n.account}: catch-all key API used by the account connection {n.account} (header api-key), backend "
              f"https://{n.model_account}.services.ai.azure.com/openai signed with APIM's identity.",
              f"/{n.account}-entra: POST /v1/responses only; validates a Microsoft Entra token (audience https://ai.azure.com, "
              "this tenant), deletes the caller's Authorization header and calls the model with APIM's identity. "
              "The hosted agent uses this path.",
              f"The prompt agent {cfg.promptAgent.name} uses the key API through that connection (chat/completions).",
              f"/{n.account}-sre: REST POST /ask {{question}} forwarding to the hosted agent's Responses endpoint with APIM's identity; "
              f"/{n.account}-sre-prompt: the same for the prompt agent {cfg.promptAgent.name}; "
              f"/{n.account}-sre-mcp: both operations exposed as MCP tools {cfg.mcp.tool} and {cfg.promptAgent.tool}. "
              f"Product {cfg.mcp.product}, "
              f"subscription {cfg.mcp.subscription} (header Ocp-Apim-Subscription-Key)."]
    return _doc("ai-gateway", "gateway", "AI gateway (APIM): configuration and APIs", *lines)


def _runtime(cfg: Config, n: Names) -> dict:
    return _doc("agent-runtime", "foundry", "Agent runtime: standard setup dependencies",
                f"Standard agent setup on project {cfg.foundry.project}: capability host {cfg.runtime.capabilityHost} "
                f"(kind Agents) with thread storage {n.cosmos} (Cosmos DB, database enterprise_memory), file storage "
                f"{n.storage} (blob, shared keys off), vector store {n.search} (AI Search {cfg.runtime.searchSku}, semantic ranker free).",
                "All three are private (public access disabled, private endpoints in the PE subnet) and reached with Entra "
                "through project connections of authType AAD. The project's managed identity holds Storage Blob Data "
                "Contributor, Cosmos DB Operator, Search Index Data Contributor, Search Service Contributor, and the Cosmos "
                "SQL built-in data contributor role on dbs/enterprise_memory.",
                "The capability host is immutable; the account-level host is created automatically by networkInjections.")


def _agent(cfg: Config, n: Names) -> dict:
    a = cfg.agent
    return _doc("hosted-agent", "agent", f"Hosted agent {a.name}",
                f"{a.name} is a Foundry hosted agent (code deploy, remote build, python 3.13, Microsoft Agent Framework, "
                f"Responses protocol), {a.cpu} vCPU / {a.memory}. Each session runs in its own sandbox whose egress leaves "
                f"from the injected subnet {cfg.foundry.agentSubnet}.",
                f"Model calls go to {gateway.entra_url(n)}/responses with the agent's own Entra token; APIM then calls "
                f"{cfg.foundry.model.name} with its identity. The agent never talks to the model account directly.",
                f"Tool search_architecture calls the knowledge base {cfg.knowledge.base} retrieve action on "
                f"{runtime.search_url(n)} (private endpoint) with the agent identity's Search Index Data Reader role. "
                "Tool where_am_i reports the sandbox hostname, the gateway's resolved IP and the local source IP.",
                f"Direct endpoint: https://{n.account}.services.ai.azure.com/api/projects/{cfg.foundry.project}/agents/{a.name}"
                "/endpoint/protocols/openai/responses?api-version=v1 (needs Foundry Agent Consumer or Azure AI User).")


def _prompt_agent(cfg: Config, n: Names) -> dict:
    a = cfg.promptAgent
    return _doc("prompt-agent", "agent", f"Prompt agent {a.name} and how it differs from the hosted agent",
                f"{a.name} is a Foundry prompt agent: a definition only (kind prompt, model, instructions, tools), no code, "
                "no container, no build. Foundry runs the tool loop. A change is a new version in seconds.",
                f"Model {n.account}/{cfg.foundry.model.name}: the account connection {n.account} to the AI gateway, so Foundry "
                f"calls {gateway.gateway_url(n)}/{n.account}/deployments/{cfg.foundry.model.name}/chat/completions with the "
                "connection's api-key. The call leaves from the agent subnet through the project capability host's data proxy.",
                f"Tool azure_ai_search on project connection {n.search}, index {cfg.knowledge.index}, semantic query, top 5.",
                f"Hosted agent {cfg.agent.name} by contrast: own code and tools (knowledge base retrieve, where_am_i), its own "
                f"container, model calls on {n.account}-entra with its Entra token.",
                f"Direct endpoint: https://{n.account}.services.ai.azure.com/api/projects/{cfg.foundry.project}/agents/{a.name}"
                "/endpoint/protocols/openai/responses?api-version=v1, the same shape as the hosted agent.")


def _webapp(c: arm.Client, cfg: Config) -> dict:
    a, an, s = cfg.app, webapp.names(cfg), cfg.sre
    site = arm.get(c, webapp.site_id(c, cfg), webapp.API_WEB)
    state = (f"{site['properties'].get('state')}, URL {webapp.hosts(site)[0]}" if site
             else "not deployed (python lab.py up app)")
    return _doc("web-app", "app", f"Web app {an.site}: the app the SRE Agent manages",
                f"Web app {an.site} ({state}) is the customer's app that nobody operates. Its own resource group {a.resourceGroup} "
                f"in {a.location}: Linux plan {an.plan} (SKU {a.sku}), Python {a.pythonVersion}, Flask under gunicorn, "
                f"workspace {an.log} and App Insights {an.appi}. Public site, HTTPS only, health check /health.",
                "Code: our own app/app.py and requirements.txt, zip-deployed to Kudu (POST <scm>/api/zipdeploy with an Entra "
                "token); Oryx installs the requirements during the deployment (SCM_DO_BUILD_DURING_DEPLOYMENT=true).",
                "Routes: GET / and GET /api/orders (the business endpoints), GET /health (always 200, keeps the instance in rotation).",
                "App setting FAULT_MODE simulates an unmanaged config change: none = healthy; errors = / and /api/orders "
                "return HTTP 500; slow = they take about 5 s. Changing an app setting restarts the app. "
                "Fix for both faults: set FAULT_MODE back to none (PUT config/appsettings).",
                f"Alerts, no action group: {an.alert5xx} (log search on AppServiceHTTPLogs, ScStatus >= 500, more than "
                f"{a.http5xxThreshold} in 5 min, Sev{a.http5xxSeverity}) and {an.alertLatency} (metric HttpResponseTime average "
                f"above {a.responseTimeSeconds} s, Sev{a.responseTimeSeverity}). Logs: AppServiceHTTPLogs, AppServiceConsoleLogs, "
                f"AppServiceAppLogs, AppServicePlatformLogs in {an.log}.",
                f"SRE Agent {s.name} has {a.resourceGroup} as a managed resource group: Reader, Monitoring Reader and Log Analytics Reader to "
                f"investigate, plus {', '.join(s.remediationRoles)} on that group only (access level {s.accessLevel}) to fix. "
                f"Response plan {s.responsePlan.id} takes alerts {', '.join(s.responsePlan.priorities)} whose title starts with "
                f"{an.alertPrefix}.")


def _flows(cfg: Config, n: Names) -> dict:
    sre = next((x for x in cfg.networks if x.name != cfg.foundry.network), None)
    return _doc("traffic-flows", "flows", "Traffic flows, who initiates, and what stays private",
                "All flows are HTTPS over TCP 443; nothing in this lab uses UDP. Each flow is initiated by the caller side; "
                "responses return on the same TCP connection, so no inbound path to the caller is needed.",
                f"1. SRE Agent -> APIM: the SRE Agent sandbox ({sre.name if sre else 'sre'} network, delegated subnet) resolves "
                f"{n.apim}.azure-api.net through the linked privatelink.azure-api.net zone to pe-{n.apim}, crosses the "
                "VNet peering, and calls the MCP endpoint with the subscription key. Private, Microsoft backbone.",
                f"2. APIM -> hosted agent: APIM's outbound VNet integration ({cfg.gateway.subnet}) reaches the project account's "
                "private endpoint with APIM's managed identity token (Foundry Agent Consumer).",
                f"3. Hosted agent -> APIM -> model: sandbox egress from {cfg.foundry.agentSubnet} to pe-{n.apim}, Entra API, then "
                f"APIM -> {n.model_account} private endpoint.",
                f"4. Hosted agent -> AI Search: knowledge base retrieve over pe-{n.search}.",
                f"4b. Prompt agent {cfg.promptAgent.name}: Foundry's data proxy in {cfg.foundry.agentSubnet} -> pe-{n.apim}, key "
                f"API /{n.account} (chat/completions), and -> pe-{n.search} for its azure_ai_search tool.",
                "5. Operator laptop -> private data planes: SSH dynamic forward (SOCKS5, socks5h so DNS resolves on the jump "
                f"host) to {n.jump_vm} on port {cfg.foundry.jump.sshPort}.",
                "Reverse direction: Foundry cannot initiate a connection to the SRE Agent privately; the SRE Agent has no "
                "private inbound endpoint. Keep the SRE Agent as the initiator (SRE -> Foundry) so the path stays private.",
                "Proof: in Log Analytics, ApiManagementGatewayLogs CallerIpAddress shows private 10.x addresses for these callers.")


def _operations(cfg: Config, n: Names) -> dict:
    return _doc("operations", "operations", "Operations: logs, KQL, tunnel, teardown caveats",
                f"Gateway logs: workspace {n.log}, tables ApiManagementGatewayLogs and ApiManagementGatewayLlmLog "
                "(diagnostic setting aigw-logs, destination Dedicated).",
                "KQL, who called the gateway: ApiManagementGatewayLogs | where TimeGenerated > ago(1h) | "
                "project TimeGenerated, ApiId, OperationId, CallerIpAddress, ResponseCode, BackendResponseCode, TotalTime | order by TimeGenerated desc",
                "KQL, private vs public callers: ApiManagementGatewayLogs | where TimeGenerated > ago(1d) | "
                "summarize calls=count() by ApiId, CallerIpAddress, private=ipv4_is_private(CallerIpAddress)",
                "Pass KQL to az on Windows as a single line: az.cmd cuts multi-line arguments at the first newline.",
                f"Tunnel: python lab.py tunnel opens ssh -D 127.0.0.1:{cfg.foundry.jump.socksPort} through {n.jump_vm}; "
                "lab layers that use data planes start it automatically.",
                "Teardown (python lab.py down --yes): delete diagnostic settings and capability hosts first, delete then purge "
                "Foundry accounts (otherwise names are held 48h) and APIM (soft-deleted APIM holds the name and its subnet), "
                "force-delete Log Analytics (skip 14-day soft delete), then the resource groups. "
                "python lab.py leftovers lists anything that survived.")


def documents(c: arm.Client, cfg: Config, n: Names) -> list[dict]:
    return [_overview(cfg, n), *_networks(cfg), _private_endpoints(c, cfg), _accounts(c, cfg, n),
            _gateway(c, cfg, n), _runtime(cfg, n), _agent(cfg, n), _prompt_agent(cfg, n), _webapp(c, cfg), _flows(cfg, n), _operations(cfg, n)]


def retrieve(c: arm.Client, cfg: Config, n: Names, question: str) -> list[dict]:
    """Knowledge base retrieve (GA, extractive): returns the grounding chunks."""
    k = cfg.knowledge
    r = arm.data(c, "POST", f"{runtime.search_url(n)}/knowledgebases/{k.base}/retrieve?api-version={API}", SCOPE, {
        "intents": [{"type": "semantic", "search": question}],
        "knowledgeSourceParams": [{"knowledgeSourceName": k.source, "kind": "searchIndex"}]})
    text = "".join(part.get("text", "") for m in arm.decode(r).get("response", []) for part in m.get("content", []))
    try:
        return json.loads(text)
    except ValueError:
        return [{"content": text}]


def up(c: arm.Client, cfg: Config, n: Names) -> None:
    if c.dry_run:
        print("== knowledge: data plane, skipped in dry-run")
        return
    k = cfg.knowledge
    base = runtime.search_url(n)
    url = lambda path: f"{base}{path}?api-version={API}"
    tunnel.up(c, cfg, n)

    print("== my roles on Search (create the index, knowledge source and base)")
    srch = runtime.ids(c, cfg, n)["search"]
    me = arm.claims(c)["oid"]
    arm.role(c, srch, me, SEARCH_SERVICE_CONTRIBUTOR, "User")
    arm.role(c, srch, me, SEARCH_INDEX_DATA_CONTRIBUTOR, "User")

    print(f"== index {k.index}")
    arm.data(c, "PUT", url(f"/indexes/{k.index}"), SCOPE, {"name": k.index, "fields": INDEX_FIELDS, "semantic": SEMANTIC},
             rbac_wait=900)

    docs = documents(c, cfg, n)
    print(f"== {len(docs)} architecture documents (lab.yaml + live ARM state)")
    arm.data(c, "POST", url(f"/indexes/{k.index}/docs/index"), SCOPE,
             {"value": [{"@search.action": "mergeOrUpload", **d} for d in docs]}, rbac_wait=900)
    live = arm.decode(arm.data(c, "POST", url(f"/indexes/{k.index}/docs/search"), SCOPE,
                    {"search": "*", "select": "id", "top": 1000})).get("value", [])
    stale = {d["id"] for d in live} - {d["id"] for d in docs}
    if stale:
        arm.data(c, "POST", url(f"/indexes/{k.index}/docs/index"), SCOPE,
                 {"value": [{"@search.action": "delete", "id": i} for i in sorted(stale)]})

    print(f"== knowledge source {k.source} -> knowledge base {k.base}")
    arm.data(c, "PUT", url(f"/knowledgesources/{k.source}"), SCOPE, {
        "name": k.source, "kind": "searchIndex", "description": "Architecture of the foundry-to-sre-agent lab.",
        "searchIndexParameters": {"searchIndexName": k.index, "semanticConfigurationName": "default",
                                  "sourceDataFields": [{"name": "title"}, {"name": "category"}]}})
    arm.data(c, "PUT", url(f"/knowledgebases/{k.base}"), SCOPE, {
        "name": k.base, "description": "Answers questions about the foundry-to-sre-agent lab: networks, private "
                                       "endpoints, Foundry, the AI gateway, the agents, the managed web app and the traffic flows.",
        "knowledgeSources": [{"name": k.source}]})

    q = "Which subnet hosts the agent compute and how does the SRE Agent reach APIM?"
    hits = retrieve(c, cfg, n, q)
    print(f"  retrieve '{q}': {len(hits)} chunk(s)")
    for h in hits[:3]:
        print(f"    - {h.get('title', '')}: {str(h.get('content', ''))[:120]!r}")
