# AI gateway: KQL to inspect prompts and calls

Run these in **Log Analytics workspace `log-<prefix>-<suffix>` > Logs**. They were tested live on the lab.

Two tables, both filled by the APIM diagnostic setting `aigw-logs` (`labkit/gateway.py`):

- **`ApiManagementGatewayLogs`**: one row per HTTP request. Holds the API, method, URL, response code,
  `CallerIpAddress` and timings. It has no headers, so no keys.
- **`ApiManagementGatewayLlmLog`**: the model calls, on the `<account>` and `<account>-entra` APIs only.
  Rows are grouped by `CorrelationId`:
  - `SequenceNumber 0`: deployment and token counts;
  - the other rows: `RequestMessages` and `ResponseMessages`, cut into 32 KB pieces.

  This table is filled only when `gateway.logMessages` > 0 in `lab.yaml` (default `32768` bytes per message).

The message JSON differs by API:

| API | Caller | `RequestMessages` | `ResponseMessages` |
|---|---|---|---|
| `<account>` (chat completions) | prompt agent | array of `{role, content}` | one `{role: assistant, content: "..."}` object |
| `<account>-entra` (Responses API) | hosted agent | `{instructions, input: [...]}` | array of output items (`reasoning`, `function_call`, `message`) |

The queries below handle both.

## 1. Question and answer per model call

```kusto
let since = ago(1d);
let text = (m: dynamic) { iff(gettype(m.content) == "string", tostring(m.content), tostring(m.content[0].text)) };
let q = ApiManagementGatewayLlmLog
    | where TimeGenerated > since and isnotempty(RequestMessages)
    | extend msgs = iff(isnotnull(RequestMessages.input), RequestMessages.input, RequestMessages)
    | mv-expand m = msgs
    | where tostring(m.role) == "user"
    | summarize Question = take_any(text(m)) by CorrelationId;
let a = ApiManagementGatewayLlmLog
    | where TimeGenerated > since and isnotempty(ResponseMessages)
    | extend r = iff(gettype(ResponseMessages) == "array", ResponseMessages, pack_array(ResponseMessages))
    | mv-expand m = r
    | where tostring(m.role) == "assistant"
    | summarize Answer = take_any(text(m)) by CorrelationId;
ApiManagementGatewayLlmLog
| where TimeGenerated > since and SequenceNumber == 0
| project TimeGenerated, CorrelationId, DeploymentName, PromptTokens, CompletionTokens
| join kind=leftouter q on CorrelationId
| join kind=leftouter a on CorrelationId
| project TimeGenerated, DeploymentName, PromptTokens, CompletionTokens, Question, Answer
| order by TimeGenerated desc
```

One agent run makes several model calls. Calls with an empty `Answer` are tool-call turns: the model asked
for a tool (for the hosted agent, `search_architecture`), and the next call carries the result.

## 2. Full request and response of every call, with the caller

```kusto
ApiManagementGatewayLlmLog
| where TimeGenerated > ago(1d)
| summarize Time = min(TimeGenerated),
            Deployment = take_anyif(DeploymentName, isnotempty(DeploymentName)),
            PromptTokens = max(PromptTokens), CompletionTokens = max(CompletionTokens),
            Request = make_list_if(RequestMessages, isnotempty(RequestMessages)),
            Response = make_list_if(ResponseMessages, isnotempty(ResponseMessages))
    by CorrelationId
| join kind=leftouter (ApiManagementGatewayLogs | where TimeGenerated > ago(1d)
    | project CorrelationId, ApiId, CallerIpAddress, ResponseCode) on CorrelationId
| project Time, ApiId, CallerIpAddress, ResponseCode, Deployment, PromptTokens, CompletionTokens, Request, Response
| order by Time desc
```

Here you see the system prompt (`developer` or `instructions`), the tool calls and the reasoning items.

## 3. Find prompts that contain a word

```kusto
ApiManagementGatewayLlmLog
| where TimeGenerated > ago(7d)
| where tostring(RequestMessages) has "private endpoint" or tostring(ResponseMessages) has "private endpoint"
| project TimeGenerated, CorrelationId, SequenceNumber, RequestMessages, ResponseMessages
```

## 4. Tokens per API and caller

```kusto
ApiManagementGatewayLlmLog
| where TimeGenerated > ago(1d) and SequenceNumber == 0
| join kind=inner (ApiManagementGatewayLogs | where TimeGenerated > ago(1d)
    | project CorrelationId, ApiId, CallerIpAddress) on CorrelationId
| summarize Calls = count(), PromptTokens = sum(PromptTokens), CompletionTokens = sum(CompletionTokens)
    by ApiId, CallerIpAddress, DeploymentName
```

The caller IP shows who called:
- a `snet-agent` IP on the `<account>` API is the prompt agent;
- a `snet-agent` IP on `<account>-entra` is the hosted agent.

## 5. The agent-to-agent path (SRE Agent → MCP → Foundry agent)

```kusto
ApiManagementGatewayLogs
| where TimeGenerated > ago(1d)
| where ApiId endswith "-sre-mcp" or ApiId endswith "-sre" or ApiId endswith "-sre-prompt" or ApiId endswith "-entra"
| project TimeGenerated, ApiId, Method, Url, CallerIpAddress, ResponseCode, TotalTime, BackendTime
| order by TimeGenerated asc
```

How to read one tool call:

| ApiId | CallerIpAddress | Meaning |
|---|---|---|
| `-sre-mcp`, quick 200/202 responses | `snet-sre-agent` | MCP `initialize`, `notifications/initialized`, `tools/list` |
| `-sre-mcp`, long call (`TotalTime` ≈ the whole run) | `snet-sre-agent` | `tools/call` |
| `-sre` or `-sre-prompt` (`/ask`) | `snet-apim` | APIM's MCP server calls the REST operation behind the tool |
| `-entra` or `<account>` | `snet-agent` | The Foundry agent calls the model |

The MCP API logs no bodies, because a logged response body makes APIM buffer the stream. The question and
answer of a tool call are in query 1 (the model calls), not here.

## From the command line

The `az monitor log-analytics query` command needs the `log-analytics` extension. On Windows, `az.cmd` cuts a
query at the first newline, so pass the KQL **on one line**:

```powershell
$ws = az monitor log-analytics workspace show -g rg-<prefix>-foundry -n log-<prefix>-<suffix> --query customerId -o tsv
az monitor log-analytics query -w $ws --analytics-query "ApiManagementGatewayLogs | where TimeGenerated > ago(1h) | project TimeGenerated, ApiId, ResponseCode, CallerIpAddress" -o table
```
