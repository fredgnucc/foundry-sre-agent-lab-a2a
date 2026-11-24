# AI gateway (APIM) roles and auth

The gateway `apim-<prefix>-<suffix>` has one system-assigned identity. Each call has two hops: the caller
authenticates to APIM, and then APIM calls the backend with **its own** identity. The caller's credential
never reaches the backend: the policies delete `Authorization` and `Ocp-Apim-Subscription-Key`.

## APIM identity roles

| Role | Scope | Assigned in | Lets APIM |
|---|---|---|---|
| Cognitive Services OpenAI User | model account `aim-<prefix>-<suffix>` | `labkit/gateway.py` | Call model deployments (`/openai/deployments/...`, `/openai/v1/responses`) |
| Foundry Agent Consumer | project `aif-<prefix>-<suffix>/projects/<project>` | `labkit/mcp.py` | Call the agents' Responses endpoint (`/agents/{name}/endpoint/protocols/openai/responses`) |

## APIs: who calls, and how they authenticate

| API | Caller (source subnet) | Hop 1: caller to APIM | Hop 2: APIM to backend |
|---|---|---|---|
| `<account>` | prompt agent (`snet-agent`) through the account connection `<account>` (`authType: ApiKey`) | `api-key` header (subscription key, product `foundry-gw`) | backend `<account>`: APIM identity, token for `https://ai.azure.com/`, to the model account |
| `<account>-entra` | hosted agent (`snet-agent`) | `validate-azure-ad-token`: tenant + audience `https://ai.azure.com`, no key | same backend, APIM identity |
| `<account>-sre-mcp` (type `mcp`) | SRE Agent (`snet-sre-agent`) | `Ocp-Apim-Subscription-Key` (subscription `mcp.subscription`) | APIM's MCP server maps `tools/call` to the REST operations below |
| `<account>-sre`, `<account>-sre-prompt` | APIM's MCP server itself (`snet-apim`) | subscription key | `authentication-managed-identity resource="https://ai.azure.com"` to the agent's Responses endpoint |

Check it live in `ApiManagementGatewayLogs.CallerIpAddress`; [gateway.kql.md](gateway.kql.md) has the queries.

## Things to know

- **The `-entra` API checks only the tenant and the audience.** Any identity in the tenant that can get a token
  for `https://ai.azure.com` can call it; there is no role or `appid` check. To restrict it, add a
  `<required-claims>` on `oid`/`appid` in `labkit/apim/policy-entra.xml`.
- **Subscription keys are the only secrets in Part 2.** `lab.py` reads them with
  `POST .../subscriptions/{sid}/listSecrets`. Headers are never logged
  (`labkit/gateway.py` `api_diagnostics`), because the key is one of them.
- **Only APIM holds a role on the model account.** Agents never call the model directly, so all prompts and
  tokens go through the gateway's logs.
- **The lab gives you no data-plane role on APIM.** You test with the subscription keys, which you read through
  your control-plane rights on the subscription.
