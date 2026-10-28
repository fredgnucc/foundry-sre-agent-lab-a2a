# Consult the lab architect

Use this skill when you investigate an incident, an alert, or a question about this lab. That includes the web app, its alerts, the networks, the AI gateway, the Foundry agents, and the private endpoints.

The tool `foundry_ask_architect` reaches the lab's Foundry hosted agent `architect` over MCP, through the private APIM AI gateway. It answers from the lab's own architecture knowledge base, which covers networks, subnets, private endpoints, peerings, DNS zones, the gateway APIs, traffic flows, KQL queries, and teardown caveats.

## When to call it

- At the start of an investigation, to get the topology around the affected resource: what it depends on, and which network path the traffic takes.
- Before a remediation that touches networking, DNS, or the gateway, to check what else depends on it.
- When you need a KQL query for this lab's Log Analytics tables (for example `ApiManagementGatewayLogs` or `AppServiceHTTPLogs`).

## How to call it

Pass one self-contained question in plain English that names the resource. For example:

- "What depends on the web app app-f2s-... and which resource group is it in?"
- "Which private endpoint fronts the AI gateway, and what is its IP?"
- "Give me a KQL query to see 5xx errors for the web app over the last hour."

## How to use the answer

- Treat the answer as design context, not as live state. Confirm it against Azure Resource Graph, metrics, and logs before you act.
- Cite in the incident summary what the architect said and what you verified.
- If the tool fails or times out, continue the investigation without it and note that the architect was unavailable.
