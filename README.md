# foundry-to-sre-agent

A private Foundry hosted agent, published as an MCP tool on a private AI gateway (APIM),
for Azure SRE Agent. Plain Python with raw ARM and data-plane REST: no Bicep, no `az`,
no SDK classes. `lab.yaml` is the single source of truth.

```
SRE Agent (snet-sre-agent, 10.41.0.0/24) --peering--> APIM PE 10.40.2.x (privatelink.azure-api.net)
  APIM /<aif>-sre-mcp/mcp  (tool ask_architect, Ocp-Apim-Subscription-Key)
    -> /<aif>-sre POST /ask -> hosted agent "architect" (APIM managed identity, Foundry Agent Consumer)
         -> search_architecture -> Foundry IQ knowledge base (AI Search PE, Entra)
         -> model via APIM /<aif>-entra/v1/responses (Entra token, no key) -> gpt model account PE

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
| `mcp` | REST and MCP APIs, product `sre-mcp`, subscription `sre-agent`, end-to-end MCP test |
| `app` | demo Flask web app (B1) in its own RG `rg-f2s-app`, App Insights, 5xx log alert and latency metric alert, zipdeploy |
| `sre` | SRE Agent in `snet-sre-agent` (egress AzureVNet), managing `rg-f2s-app`; MCP connector, response plan, skill |

The `knowledge`, `agent` and `mcp` layers call private data planes. `lab.py` opens an
`ssh -D` SOCKS tunnel through the jump host and uses `socks5h`, so names resolve on the
jump host against the private zones.

## Usage

```powershell
pip install -r requirements.txt
python lab.py up all --watch          # every layer; idempotent, re-run any time
python lab.py show
python lab.py ask "what's going on in the architecture?"
python lab.py mcp "which PE fronts the gateway?"   # same path as the SRE Agent
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
