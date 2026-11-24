# Foundry agent runtime: Cosmos DB, Storage and AI Search

Why Part 2 deploys three data services next to Foundry, what each one holds at run time, and who
reads or writes it. Deployed by `python lab.py up runtime` (`labkit/runtime.py`) and
`python lab.py up knowledge` (`labkit/knowledge.py`).

## Why they exist: standard agent setup

The Foundry agent service needs somewhere to keep conversations, files and vector indexes. Two options:

| Setup | Where the agent data lives | Fits this lab? |
|---|---|---|
| Basic | Storage managed by Microsoft, outside your network | No: the agent compute is injected into our VNet (`snet-agent`) and everything stays private |
| **Standard (this lab)** | **Your own** Cosmos DB, Storage and AI Search, behind private endpoints | Yes |

The link between the project and the three services is the project **capability host** `caphostproj`
(`PUT …/projects/<project>/capabilityHosts/caphostproj`, `capabilityHostKind: Agents`):

| Capability host field | Project connection | Service |
|---|---|---|
| `threadStorageConnections` | `cosmos-<prefix>-<suffix>` (`CosmosDB`) | Cosmos DB: conversations, runs, agent definitions |
| `storageConnections` | `st<prefix><suffix>` (`AzureStorageAccount`) | Blob Storage: files |
| `vectorStoreConnections` | `srch-<prefix>-<suffix>` (`CognitiveSearch`) | AI Search: vector stores for file search |

Order matters and `up runtime` follows it: services, then connections (`authType: AAD`), then the project
identity's roles, then the capability host, then the Cosmos data role. The database `enterprise_memory`
only exists once the capability host has created it. The capability host can't be changed after
creation, only deleted and recreated.

All three services: `publicNetworkAccess` disabled, one private endpoint each in the Foundry VNet,
Entra auth (Cosmos `disableLocalAuth: true`, Storage `allowSharedKeyAccess: false`).

## Cosmos DB: the agent's memory

A Cosmos DB "container" is a collection of JSON documents, like a table: nothing runs in it. The
code runs elsewhere: the hosted `architect` in a compute container that Foundry builds from our zip
(`remote_build`) and injects into `snet-agent`. The prompt agent has no container of its own: Foundry's
service runs its definition. Both keep their state here.

The capability host creates database `enterprise_memory` with six containers, all prefixed by the
project's `internalId` (a GUID, `GET …/projects/<project>` → `properties.internalId`):

| Container | Partition key | TTL | Holds (from the names; Microsoft manages the schema) |
|---|---|---|---|
| `<project-guid>-thread-message-store` | `/thread_id` | none | Conversation messages and items |
| `<project-guid>-system-thread-message-store` | `/thread_id` | none | The service's own system threads |
| `<project-guid>-agent-definitions-v1` | `/partition_id` | none | Agent definitions and versions (`architect`, `architect-prompt`) |
| `<project-guid>-run-state-v1` | `/partition_id` | 60 days | Run and response state |
| `<project-guid>-agent-entity-store` | `/partition_key_value` | none | Other agent entities |
| `<project-guid>-state-storage-v1` | `/object/partition_key` | none | Service state |

```powershell
az cosmosdb sql container list -g rg-<prefix>-foundry -a cosmos-<prefix>-<suffix> -d enterprise_memory `
  --query "[].{name:name, pk:resource.partitionKey.paths[0], ttl:resource.defaultTtl}" -o table
```

Only the **project identity** reads and writes it: Cosmos DB Operator on the account (control plane)
and the SQL built-in role `…0002` (data contributor) on `dbs/enterprise_memory`
([foundry.roles.md](foundry.roles.md)). Neither the agents' code nor the SRE Agent touches it.

## Blob Storage: files

The capability host creates two containers, also prefixed by the project GUID:

| Container | Holds |
|---|---|
| `<project-guid>-azureml-blobstore` | The project's default blob store: files uploaded to the agent service |
| `<project-guid>-<id>-azureml-agent` | The agent service's own artifacts |

```powershell
az storage container-rm list --storage-account st<prefix><suffix> -g rg-<prefix>-foundry --query "[].name" -o tsv
```

The project identity has Storage Blob Data Contributor on the account. The lab uploads no files
to agents. The containers come with the standard setup, whether you use them or not.

## AI Search: two jobs

| Job | Objects | Created by | Used at run time by |
|---|---|---|---|
| **Vector store** for the agents' file search tool (capability host) | an index per vector store, created on demand | the agent service | nobody in this lab: no agent uses file search, so no such index exists |
| **Knowledge** about the lab (Foundry IQ) | index `lab-architecture` (13 documents), knowledge source `lab-architecture-ks`, knowledge base `lab-architecture-kb` | `up knowledge`, with your identity | both architect agents |

The knowledge documents are generated from `lab.yaml` plus live ARM state (private endpoint IPs, APIM APIs,
account settings). `up knowledge` rebuilds them and deletes stale ones. The index is keyword plus semantic
ranking (`semanticSearch: free`), with no vectors: retrieval is extractive and no LLM runs inside Search.

How each agent reads it:

| Agent | Call | Identity |
|---|---|---|
| `architect` (hosted) | Its tool `search_architecture` sends `POST https://srch-<prefix>-<suffix>.search.windows.net/knowledgebases/lab-architecture-kb/retrieve?api-version=2026-04-01` (token for `https://search.azure.com`) | The hosted agent's instance identity (Search Index Data Reader) |
| `architect-prompt` (prompt) | Built-in tool `azure_ai_search` on index `lab-architecture`, through the project connection `srch-<prefix>-<suffix>` | Foundry, with the project identity (connection `authType: AAD`) |

```powershell
python lab.py retrieve "Which subnet hosts the agent compute?"   # the same retrieve call, through the tunnel
```

Search keeps `aadOrApiKey`: the Foundry connection is Entra, but `apiKeyOnly` would block it.

## At run time, one SRE question

```text
SRE Agent --MCP--> APIM -sre-mcp --> /ask --> Foundry agent endpoint (project)
                                                  |
                     agent definition + conversation state  <-->  Cosmos DB (enterprise_memory)
                     architect: retrieve  ------------------->  AI Search (lab-architecture-kb)
                     model call  ---------------------------->  APIM -entra / <account> --> model account
```

- Cosmos DB and Storage are infrastructure for the agent service. The architect's answers don't come from
  them.
- AI Search is the only one the agents query for content: without the knowledge base, the architect
  has nothing to ground its answer on.
- The hosted agent sets `store: False` on its model calls: the gateway's Responses API keeps
  no copy of them.
