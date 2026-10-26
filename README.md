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
python lab.py down --yes                            # teardown + purges + leftovers audit
```

After changing the infrastructure, re-run `up knowledge` so the agent's knowledge stays current.

## Verify the traffic is private

```kusto
ApiManagementGatewayLogs | where TimeGenerated > ago(1h) | summarize count() by ApiId, CallerIpAddress, ResponseCode
```

Expected callers: `10.40.1.x` (hosted agent, snet-agent), `10.40.3.x` (jump host),
and the SRE Agent's subnet once it is connected. Keep KQL to one line when using `az`
on Windows.

## Gotchas

- APIM v2: create with public access Enabled, then add the PE, then disable it. Use a full PUT; `az apim update` fails on v2.
- APIM returns some bodies with a UTF-8 BOM and policies as raw XML (`arm.decode` handles both). `products/{p}/apis/{a}` has no GET (405), so link it with a plain PUT.
- A new subscription key can return 401 for about a minute; `--watch` retries.
- APIM is soft-deleted on RG delete, and its name stays reserved until it is purged; `down` purges it.
- The capability host is immutable: delete it before changing its connections.
- If the jump VM is recreated, delete `.ssh/known_hosts`.
- Cost: APIM StandardV2, AI Search basic, Cosmos, the VM and NAT all bill while up.
