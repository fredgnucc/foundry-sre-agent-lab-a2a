# Foundry roles

Who can do what on the Foundry side, checked live with:

```powershell
az role assignment list --all --query "[?contains(scope,'rg-<prefix>-foundry')].{role:roleDefinitionName,who:principalName,type:principalType,scope:scope}" -o table
az cosmosdb sql role assignment list -g rg-<prefix>-foundry -a cosmos-<prefix>-<suffix> -o table
```

## Identities

| Identity | Type | Created by | Holds roles |
|---|---|---|---|
| Project `aif-<prefix>-<suffix>/<project>` | system-assigned | the project | yes: the agent runtime (storage, Cosmos DB, Search) |
| Hosted agent instance (`instance_identity` on the agent version) | Entra agent identity | the hosted agent service, on each deploy | yes: read the knowledge index |
| Foundry account `aif-<prefix>-<suffix>` | system-assigned | the account | none |
| Model account `aim-<prefix>-<suffix>` | system-assigned | the account | none |
| Search service `srch-<prefix>-<suffix>` | system-assigned | the service | none |
| You (the deployer) | user | | yes: create and call agents, fill the index |

The gateway's own identity is in [gateway.roles.md](gateway.roles.md).

## Roles

| Principal | Role | Scope | Assigned in | Why |
|---|---|---|---|---|
| Project identity | Storage Blob Data Contributor | storage account | `labkit/runtime.py` | Agent files (capability host `storageConnections`) |
| Project identity | Cosmos DB Operator | Cosmos DB account | `labkit/runtime.py` | Control plane on the thread store |
| Project identity | Cosmos DB Built-in Data Contributor (SQL role `…0002`) | database `enterprise_memory` | `labkit/runtime.py` | Read and write threads; the database exists only after the capability host |
| Project identity | Search Index Data Contributor | search service | `labkit/runtime.py` | Vector store for agent file search |
| Project identity | Search Service Contributor | search service | `labkit/runtime.py` | Create indexes for the vector store |
| Hosted agent identity | Search Index Data Reader | search service | `labkit/agent.py` | The architect queries the knowledge index (read only) |
| You | Foundry User | Foundry account | `labkit/foundry.py`, `labkit/agent.py` | Data plane: create, deploy and call agents |
| You | Search Service Contributor | search service | `labkit/knowledge.py` | Create the knowledge index |
| You | Search Index Data Contributor | search service | `labkit/knowledge.py` | Upload the knowledge documents |

## Things to know

- **No keys anywhere:** the MCAPS policy forces `disableLocalAuth: true` on the Foundry account, so every call
  uses an Entra token. Each project connection (Cosmos DB, storage, Search) uses `authType: AAD`.
- **The hosted agent identity is new on every deploy.** `labkit/agent.py` reads `instance_identity.principal_id`
  from the active version and assigns the role again, so redeploying keeps search access.
- **The hosted agent has no role on the model account.** It reaches the model through the gateway's `-entra` API
  (token for `https://ai.azure.com`), and only the gateway's identity holds a role on the model. See
  [gateway.roles.md](gateway.roles.md).
- **The prompt agent doesn't use its own identity for the model:** its model is `<account>/<deployment>`, so it
  calls the gateway through the account connection `<account>` (`authType: ApiKey`, the APIM subscription key).
- **Nothing in Part 2 can write to the app's resource group.** Only the SRE Agent can
  ([sre.roles.md](sre.roles.md)).
