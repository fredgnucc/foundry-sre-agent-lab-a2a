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

## Layers

| layer | what |
|---|---|
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
| `sre` | SRE Agent in `snet-sre-agent` (egress AzureVNet), managing `rg-f2s-app`; MCP connector, response plan, skill |

The `knowledge`, `agent`, `prompt` and `mcp` layers call private data planes. `lab.py` opens an
`ssh -D` SOCKS tunnel through the jump host and uses `socks5h`, so names resolve on the
jump host against the private zones.

## Usage

```powershell
pip install -r requirements.txt
python lab.py up all --watch          # every layer; idempotent, re-run any time
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
python lab.py down --yes                            # teardown + purges + leftovers audit
```

After changing the infrastructure, re-run `up knowledge` so the agent's knowledge stays current.

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
