# foundry-to-sre-agent

A private Foundry hosted agent and a prompt agent side by side, published as MCP tools on a private AI gateway (APIM),
for Azure SRE Agent. Plain Python with raw ARM and data-plane REST: no Bicep, no `az`,
no SDK classes. `lab.yaml` is the single source of truth.

```
SRE Agent (snet-sre-agent, 10.41.0.0/24) --peering--> APIM PE 10.40.2.x (privatelink.azure-api.net)
  APIM /<aif>-sre-mcp/mcp  (tools ask_architect, ask_architect_prompt; Ocp-Apim-Subscription-Key)
    -> /<aif>-sre POST /ask -> hosted agent "architect" (APIM managed identity, Foundry Agent Consumer)
         -> search_architecture -> Foundry IQ knowledge base (AI Search PE, Entra)
         -> model via APIM /<aif>-entra/v1/responses (Entra token, no key) -> gpt model account PE
    -> /<aif>-sre-prompt POST /ask -> prompt agent "architect-prompt" (same identity, same endpoint shape)
         -> azure_ai_search tool -> index lab-architecture (project connection, Entra)
         -> model via connection <aif>/gpt-5.6-sol -> APIM /<aif>/deployments/.../chat/completions (api-key)

Azure Monitor alert (app in rg-f2s-app, the "customer" RG the agent manages)
  -> SRE Agent incident -> response plan -> skill consult-architect -> MCP tool foundry_ask_architect
  -> remediation on the app (Website Contributor on rg-f2s-app)
```

Every hop is HTTPS on TCP 443 and is initiated by the calling side. SRE to Foundry is private.
Foundry to SRE Agent is not: the SRE Agent has no private inbound endpoint.

## Portals

| Portal | URL | What you see there |
|---|---|---|
| Microsoft Foundry | https://ai.azure.com | the `aif-f2s-*` account and project: agents, model deployments, traces. Public access is off, so the data views (agents, playground) only load from a network that reaches the private endpoint (for example through the jump VM: `ssh -D` and a browser using SOCKS5 with remote DNS). |
| Azure SRE Agent | https://sre.azure.com | `sre-f2s-agent`: incident threads, response plans, connectors (MCP), mapped resource groups |
| Azure portal | https://portal.azure.com | the `rg-f2s-*` resource groups. Azure Monitor → Alerts; the `amw-f2s-*` workspace → Prometheus explorer; APIM → APIs and logs |

Sign in with the account in the tenant from `lab.local.yaml`.

## Layers

| layer | what |
|---|---|
| `providers` | registers the `providers` list from `lab.yaml` on the subscription (`GET`, then `POST .../register` only where needed, poll until `Registered`); `down` leaves them |
| `network` | RGs, NSGs, NAT, VNets (list-based), private DNS zones and links, peerings, Log Analytics |
| `foundry` | model account and model, Foundry account and project (agent subnet injection), PEs |
| `jump` | Ubuntu VM with sshd on 2222 (MCAPS strips inbound 22); key in `.ssh/` |
| `gateway` | APIM StandardV2 AI gateway, PE, logs, key API and Entra API to the model, then public access Disabled |
| `runtime` | storage, Cosmos, AI Search (all private) and project capability host |
| `knowledge` | index of the lab's own architecture (from `lab.yaml` and live ARM state), knowledge source, knowledge base |
| `agent` | hosted agent `agents/architect/` (deterministic zip, remote build), its identity can read Search |
| `prompt` | prompt agent `architect-prompt`: a definition only, new version only when it changed |
| `mcp` | REST and MCP APIs, product `sre-mcp`, subscription `sre-agent`, end-to-end MCP test |
| `app` | demo Flask web app (B1) in its own RG `rg-f2s-app`, App Insights, 5xx log alert and latency metric alert, zipdeploy |
| `external` | a resource outside Azure (see below): Azure Monitor workspace (managed Prometheus) in `rg-f2s-external`, collector identity, fake vendor + Prometheus agent on the jump VM (Run Command), Prometheus alert rules |
| `sre` | SRE Agent in `snet-sre-agent` (egress AzureVNet), managing `rg-f2s-app` and `rg-f2s-external` (created if missing), response plan; MCP connector and skill only once `mcp` is deployed (rerun `up sre` after it) |

The `knowledge`, `agent`, `prompt` and `mcp` layers call private data planes. `lab.py` opens an
`ssh -D` SOCKS tunnel through the jump host and uses `socks5h`, so names resolve on the
jump host against the private zones.

`up` runs layers in the table order, whatever order you type them in. Before any PUT it checks
each layer's prerequisites (`gateway` needs `foundry`, `sre` needs only `network`, and so on;
see `NEEDS` in `lab.py`). The check is one ARM GET per layer and must find `provisioningState`
Succeeded. A prerequisite you list in the same command counts as present. If one is missing, the
run stops with `<layer> needs <dep> (<resource> not found) -> python lab.py up <dep>` and changes nothing.
`knowledge`, `agent` and `prompt` are data-plane only, so their own prerequisites are checked
instead. `jump` also reads the live NSG on `snet-jump` (GET subnet, then GET NSG). It stops if
inbound TCP `sshPort` from the internet loses to a higher-priority rule, for example a tenant
policy Deny or a removed rule. Without this check, the VM reports Succeeded but the tunnel never connects.

**Region.** `location` at the top of `lab.yaml` places the whole lab. A network or the `app` entry
may set its own `location` to override it (for example where SRE Agent isn't offered); overrides
show in the `status` header. A VNet can't change region, so `up` first compares each deployed VNet
and the web app with `lab.yaml` and stops on a mismatch, changing nothing:
`network foundry is deployed in francecentral but lab.yaml says swedencentral -> python lab.py down --yes`.
`down` purges soft-deleted Foundry accounts and APIM in the region they actually lived in.

**Where am I?** `python lab.py status` prints one line per layer in deploy order (`ok`, `--` with
the missing resource, or `?` for the data-plane layers it can't probe from outside the tunnel),
the next command to run, and any region mismatch. It only does ARM GETs.

## Network security: NSG rules and the traffic they match

Each subnet gets its own NSG `nsg-<vnet>-<subnet>`, created by `up network` from `nsgRules` in
`lab.yaml`. A PUT replaces the whole rule list, so `up network` also removes any drift. The
subnets with no rules still get an NSG, because a tenant policy otherwise creates an empty one and
the two then overwrite each other on every run.

**Explicit rules** (live on Contoso, identical to `lab.yaml`):

| Subnet | Rule | Prio | Dir | Match | Traffic it is for |
|---|---|---|---|---|---|
| `snet-jump` 10.40.3.0/24 | `AllowSshInbound` | 100 | In | TCP `*` → `*`:2222 | laptop → jump public IP, `ssh -D` SOCKS tunnel. Not 22: a tenant policy strips internet-facing 22 rules |
| `snet-apim` 10.40.0.0/24 | `AllowKeyVaultOutbound` | 100 | Out | TCP VirtualNetwork → `AzureKeyVault`:443 | APIM Standard v2 outbound integration; Key Vault is a documented APIM dependency ([Learn](https://learn.microsoft.com/azure/api-management/integrate-vnet-outbound#network-security-group)) |
| `snet-agent` 10.40.1.0/24 | `AllowEntraIdOutbound` | 100 | Out | TCP VirtualNetwork → `AzureActiveDirectory`:443 | Foundry agent compute (Container Apps underneath) gets Entra tokens; without it, agent provisioning fails |
| `snet-pe` 10.40.2.0/24 | none | | | | private endpoints |
| `snet-sre-agent` 10.41.0.0/27 | none | | | | SRE Agent sandbox (Sweden), NAT gateway for internet |

**Default rules, present in every NSG.** Most lab traffic is allowed by these:

| Prio | Inbound | Outbound |
|---|---|---|
| 65000 | `AllowVnetInBound`: VirtualNetwork → VirtualNetwork | `AllowVnetOutBound`: VirtualNetwork → VirtualNetwork |
| 65001 | `AllowAzureLoadBalancerInBound` | `AllowInternetOutBound`: → Internet |
| 65500 | `DenyAllInBound` | `DenyAllOutBound` |

`VirtualNetwork` covers the VNet, the peered VNet (10.40.0.0/16 and 10.41.0.0/24) and on-prem
prefixes. DNS to 168.63.129.16 is platform traffic, and NSGs never filter it.

**Flows and the rule that matches each:**

| # | Flow | Port | Matched by |
|---|---|---|---|
| 1 | laptop → jump public IP | TCP 2222 | `AllowSshInbound` (jump NSG, inbound) |
| 2 | jump → Foundry, Search, Cosmos, Storage, APIM private endpoints in `snet-pe` (SOCKS-forwarded) | 443 | `AllowVnetOutBound`. PE side: not evaluated, see below |
| 3 | jump → internet (cloud-init `apt`) | 80/443 | `AllowInternetOutBound`, via its public IP |
| 4 | APIM (`snet-apim`) → model account private endpoint | 443 | `AllowVnetOutBound` |
| 5 | APIM → Key Vault | 443 | `AllowKeyVaultOutbound` |
| 6 | Foundry agent (`snet-agent`) → APIM private endpoint (chat/completions), Search/Cosmos/Storage private endpoints | 443 | `AllowVnetOutBound` |
| 7 | Foundry agent → Entra ID | 443 | `AllowEntraIdOutbound` |
| 8 | SRE Agent (`snet-sre-agent`) → peering → APIM private endpoint (MCP) | 443 | `AllowVnetOutBound`. Name resolves through the `privatelink.azure-api.net` link on the sre VNet |
| 9 | SRE Agent → ARM, Azure Monitor, web app (public) | 443 | `AllowInternetOutBound`, via the NAT gateway |

Notes from the review:
- `snet-pe` has `privateEndpointNetworkPolicies = Disabled`, so NSGs are **not evaluated** for
  traffic to private endpoints. What keeps the data planes private is `publicNetworkAccess =
  Disabled` on every resource, plus the private endpoints and private DNS. To filter private
  endpoint traffic with NSG rules, set the subnet property to `Enabled` and add rules on the `snet-pe` NSG.
- The two outbound Allow rules (5 and 7) are already covered today by `AllowInternetOutBound`.
  They are pinned at 100 so the lab keeps working if you (or a policy) add a Deny-Internet outbound rule above 100.
- APIM v2 outbound integration ignores inbound NSG rules on `snet-apim`. Inbound reaches APIM
  only through its private endpoint.
- The peering puts both VNets inside `VirtualNetwork`. The SRE subnet can therefore reach every
  10.40.x subnet, not only `snet-pe`. To restrict it, add an outbound Deny for 10.40.0.0/16 except
  10.40.2.0/24 on `nsg-…-sre-agent`.
- `AllowSshInbound` accepts any source (`sshSource: "*"`), so only the SSH key protects the jump
  host. Set `sshSource` to your `/32` to lock it down.

## Logs

Every command writes two things under `logs\` (git-ignored, it contains your IDs):

| file | what |
|---|---|
| `logs\<time>-<command>.log` | this run's console: target line, each request with its status, retries, errors |
| `logs\changes.jsonl` | appended by every run: one JSON line per change (`PUT`, `PATCH`, `DELETE`, and `POST`s that change something; reads, `listKeys`, agent `responses`, MCP calls and dry-runs are left out) with `ts`, `run`, `tenant`, `subscription`, `plane` (`arm` or `data`), `method`, `target` (resource ID or data-plane URL), `api`, `status`, `result` (`created` / `updated` / `accepted` / `deleted` / `absent` / `done` / `failed`) |

```powershell
Get-Content logs\changes.jsonl | ConvertFrom-Json | Where-Object result -eq created | Format-Table ts, method, target
```

## Usage

The tenant and subscription are not in git. Put them in `lab.local.yaml` (git-ignored), or set
`AZURE_TENANT_ID` and `AZURE_SUBSCRIPTION_ID`. `azureConfigDir` in that file picks the az profile
(`AZURE_CONFIG_DIR`) that is logged in to the tenant, so switching tenant means rewriting one file:

```powershell
Copy-Item lab.local.example.yaml lab.local.yaml   # then fill in tenantId, subscriptionId, azureConfigDir
# or generate it from the az profiles on this machine (Documents\yolo\tools\azctx.py):
python ..\tools\azctx.py --find contoso --write lab.local.yaml
az login --tenant <tenantId>                       # lab.py uses the az CLI login
```

Every command first checks the target: it requests the token for `tenantId` (whatever the az default
subscription is), checks the token's `tid` claim and `GET /subscriptions/{id}`, and prints
`target: tenant … | subscription … | az profile …`. If you have several tenants, each with its own
az profile (`AZURE_CONFIG_DIR`), a wrong profile stops the run before any change, with the
`az login --tenant` command that fixes it.

```powershell
pip install -r requirements.txt
python lab.py up all --watch          # every layer; idempotent, re-run any time
python lab.py status                  # which layers are up, and the next command
python lab.py show
python lab.py ask "what's going on in the architecture?"
python lab.py ask --prompt "..."                   # the prompt agent instead
python lab.py mcp "which PE fronts the gateway?"   # same path as the SRE Agent
python lab.py mcp --prompt "..."                   # tool ask_architect_prompt
python lab.py retrieve "flows"                      # raw knowledge base hits
python lab.py mcp-key                               # URL + key for the SRE Agent MCP connector
python lab.py tunnel                                # hold the SOCKS tunnel for other tools
python lab.py tunnel --edge [url]                   # + Edge through it (throwaway profile, DNS on the jump host)
python lab.py publish [--force]                     # redeploy app/ (skipped when unchanged)
python lab.py fault --mode errors                   # FAULT_MODE + 6 min traffic -> 5xx alert -> incident
python lab.py fault --mode none                     # manual reset (the agent normally does it)
python lab.py sre-test                              # the SRE Agent's own MCP test call to the gateway
python lab.py vendor slow                           # external vendor: ok|slow|errors|down (no arg: read) -> Prometheus alert
python lab.py promql 'vendor_api_latency_seconds'   # PromQL on the Azure Monitor workspace
python lab.py down --yes                            # teardown + purges + leftovers audit (parallel; ~20 min, the capability host is the long pole)
python lab.py down external --yes                   # only the external layer, keeps rg-f2s-external (SRE Agent mapping); then up external
```

After changing the infrastructure, re-run `up knowledge` so the agent's knowledge stays current.

## External resource: a vendor outside Azure, through managed Prometheus

A customer's dependency is a third-party API they don't run. The only signal is its metrics.
The SRE Agent is triggered by Azure Monitor alerts in a resource group it manages, so the
external metrics are brought into Azure Monitor first. This lab shows the **pull** model: the
vendor exposes `/metrics`, and our collector scrapes it.

```
fake vendor (external/vendor_sim.py, 127.0.0.1:9105 on the jump VM)
  <- GET /metrics every 15s   Prometheus 3.x --agent (same VM, systemd)
  -> POST <DCE>/dataCollectionRules/<dcr-immutable-id>/streams/Microsoft-PrometheusMetrics/api/v1/write
       Entra token from IMDS for id-f2s-collector (aud https://monitor.azure.com, no key on disk)
       role Monitoring Metrics Publisher on the workspace's DCR (managed RG MA_<amw>_<region>_managed)
  -> Azure Monitor workspace amw-f2s-* (rg-f2s-external)
  -> rule group prg-f2s-vendor, evaluated every minute:
       alert-f2s-vendor-latency  vendor_api_latency_seconds > 0.5
       alert-f2s-vendor-errors   rate(errors[5m]) / rate(requests[5m]) > 0.05
       alert-f2s-vendor-down     up{job="vendor"} == 0
  -> Azure Monitor alert (Sev2, after 2 min of breach) on the workspace in rg-f2s-external
  -> SRE Agent (rg-f2s-external is mapped; response plan matches "alert-f2s-") -> incident thread
       it queries PromQL itself: Monitoring Data Reader on rg-f2s-external
```

```powershell
python lab.py up providers                  # Microsoft.Monitor, Microsoft.AlertsManagement
python lab.py up external                   # ~3 min; prints the remote-write and query endpoints
python lab.py up sre                        # maps rg-f2s-external + its roles
python lab.py promql 'up{job="vendor"}'     # GET {query endpoint}/api/v1/query, aud https://prometheus.monitor.azure.com
python lab.py vendor slow                   # ssh to the jump VM, writes /var/lib/vendor-sim/mode
python lab.py vendor ok                     # recover; the alert auto-resolves after 10 min
python lab.py down external --yes           # stop + remove the VM services, detach the identity, delete the rule group,
                                            #   workspace (its MA_ RG, DCE, DCR) and identity; rg-f2s-external stays
```

Measured: `vendor slow` at 09:30, alert `Fired` at 09:32:40, SRE Agent thread
`[Sev2] prg-f2s-vendor` at 09:33:07.

The **push** model is the same Prometheus agent running at the vendor (or on any machine outside
Azure), with an Entra app registration instead of a managed identity, remote-writing to the same
public ingestion endpoint. Private options: a private endpoint on the workspace for queries
(`prometheusMetrics`, zone `privatelink.<region>.prometheus.monitor.azure.com`) and the DCE in
an AMPLS for ingestion.

## Hosted agent vs prompt agent

Both answer the same questions from the same knowledge, so a customer can compare them.
Both are called the same way: `POST {project}/agents/{name}/endpoint/protocols/openai/responses?api-version=v1`.
The SRE Agent connector exposes both tools. The incident skill uses the hosted one.

| | hosted `architect` | prompt `architect-prompt` |
|---|---|---|
| what you deploy | code: `agents/architect/` zip, remote build, container (0.5 vCPU / 1Gi) | JSON: `POST {project}/agents` `{definition: {kind: prompt, model, instructions, tools}}` |
| who runs the loop | your code (Agent Framework) | Foundry |
| tools | any Python: KB retrieve, `where_am_i` (sandbox network facts) | catalog tools: `azure_ai_search` on the project's search connection |
| model call | `POST {gw}/<aif>-entra/v1/responses`, agent's Entra token | `POST {gw}/<aif>/deployments/gpt-5.6-sol/chat/completions`, account connection key |
| egress | sandbox in the agent subnet | Foundry data proxy in the agent subnet (needs the project capability host) |
| change | new zip, rebuild (minutes) | new version (seconds) |
| cold `/ask` measured | ~21 s (container start) | ~4 s |

Measured with `ApiManagementGatewayLogs`, CallerIpAddress for both model APIs was in `snet-agent` (10.40.1.0/24).

## Publish the architect to Teams and Microsoft 365 Copilot (opt-in)

The portal's Publish button doesn't work when the project's public access is off, so
`python lab.py m365` runs the [REST flow](https://learn.microsoft.com/azure/foundry/agents/how-to/publish-copilot-virtual-network)
(tunnel opened automatically). It isn't part of `up all`, because it opens a public route.

| step | call | result |
|---|---|---|
| 1 | `GET {project}/agents/architect` (tunnel) | `instance_identity.client_id`, the bot's app ID |
| 2 | ARM `PUT Microsoft.BotService/botServices/bot-f2s-architect-<sfx>` + `channels/MsTeamsChannel` | Azure Bot F0, SingleTenant, public access off, endpoint = the agent's `activityprotocol` route |
| 3 | `PATCH {project}/agents/architect` (merge-patch, tunnel) | adds protocol `activity` with `enable_m365_public_endpoint` and scheme `BotServiceRbac` (Shared) or `BotServiceTenant` (Tenant); keeps `responses` and `Entra` |
| 4 | `POST {project}/agents/architect/microsoft365/publish` (tunnel) | Teams app (`titleId`, `teamsAppId`); 409 means that app version already exists |

```powershell
python lab.py m365                              # Shared: only you, Teams/Copilot > Apps > Your agents
python lab.py m365 --scope Tenant --app-version 1.0.1   # org-wide after admin.cloud.microsoft approval
```

Only the Activity Protocol route becomes public, and only from Bot Service / M365 IP ranges
with a valid Bot Service token for this agent. Everything else stays private, and the agent's
outbound path doesn't change. The bot goes with `rg-f2s-foundry` at teardown; remove the
Teams app by hand.

## Verify the traffic is private

```kusto
ApiManagementGatewayLogs | where TimeGenerated > ago(1h) | summarize count() by ApiId, CallerIpAddress, ResponseCode
```

Expected callers: `10.40.1.x` (hosted agent, snet-agent), `10.40.3.x` (jump host),
`10.40.0.x` (the MCP bridge to `/sre`) and `10.41.0.x` (SRE Agent, `sre-mcp`). Keep KQL to one line when using `az`
on Windows.

## See inside the HTTP traffic (prompt and body logging)

APIM terminates TLS, so it can log what is inside each request. Each API gets an `apis/{api}/diagnostics/azuremonitor`
resource. You set the sizes with `gateway.logMessages` and `gateway.logBodies` in `lab.yaml` (0 turns logging off):

| API | What gets logged | Table |
|---|---|---|
| `{account}`, `{account}-entra` (model) | prompt and completion messages + tokens | `ApiManagementGatewayLlmLog` |
| `{account}-sre` (REST /ask) | request and response body | `ApiManagementGatewayLogs` (`RequestBody`/`ResponseBody`) |
| `{account}-sre-mcp` (MCP) | request body only (JSON-RPC `tools/call`) | `ApiManagementGatewayLogs` |

```kusto
ApiManagementGatewayLlmLog | where TimeGenerated > ago(1h) | summarize Model=take_anyif(ModelName, SequenceNumber==0), Prompt=sum(PromptTokens), Completion=sum(CompletionTokens), Req=make_list_if(RequestMessages, SequenceNumber>0), Resp=make_list_if(ResponseMessages, SequenceNumber>0) by CorrelationId
ApiManagementGatewayLogs | where TimeGenerated > ago(1h) and ApiId contains '-sre' | project TimeGenerated, ApiId, CallerIpAddress, RequestBody, ResponseBody
```

Each model call writes several rows that share one `CorrelationId`:
- `SequenceNumber` 0 holds the model name and token counts.
- 1 and up hold the prompt (`instructions` + input) and the completion (messages or `function_call`). Anything over 32 KB is split into more rows.

This also works on these plain HTTP APIs: an Azure OpenAI import is not needed, and the Responses API is parsed.

Headers are never logged, because the subscription key is one, and query strings are masked. The MCP response body is not logged, because logging it would make APIM buffer the stream.

Prompts and answers are stored in Log Analytics in clear text, so set the values to `0` for anything sensitive.

## Gotchas

- APIM v2: create with public access Enabled, then add the PE, then disable it. Use a full PUT; `az apim update` fails on v2.
- APIM returns some bodies with a UTF-8 BOM and policies as raw XML (`arm.decode` handles both). `products/{p}/apis/{a}` has no GET (405), so link it with a plain PUT.
- A new subscription key can return 401 for about a minute; `--watch` retries.
- APIM is soft-deleted on RG delete, and its name stays reserved until it is purged; `down` purges it.
- The capability host is immutable: delete it before changing its connections.
- If the jump VM is recreated, delete `.ssh/known_hosts`.
- The jump VM gets deallocated by a policy/automation (seen at 23:50 UTC); `tunnel` and every private-plane command start it again (`POST .../start`) before ssh.
- SRE Agent data plane (`azuresre.dev` audience): `testconnection` needs the full connector body including the key. An empty body returns 400 InvalidConnectorType, and ARM GETs redact `extendedProperties`.
- SRE Agent Administrator can take minutes to apply on the data plane (403); response plan PUT returns 409 when the plan exists, so update with POST.
- First zipdeploy to a new site can fail with "Could not detect any platform"; `publish` retries.
- The 5xx log alert returns 400 until `AppServiceHTTPLogs` exists in a new workspace; `up app` waits.
- A site PUT resets `FAULT_MODE` to `none`.
- Cost: the SRE Agent (~$288/month baseline), APIM StandardV2, AI Search basic, Cosmos, the VM and NAT all bill while up.
- AI Search `runtime.searchSku: serverless` (preview, GA expected soon) bills per compute unit and stored GB instead of a fixed unit, and avoids the Basic capacity errors seen in some regions. `up runtime` then uses API `2026-09-01-preview` without replicas or partitions. Only some regions offer it (Sweden Central yes, France Central no); check with `POST https://management.azure.com/providers/Microsoft.Search/fetchOfferings?api-version=2026-09-01-preview`. Not yet validated with this lab's private endpoint and semantic ranker.
