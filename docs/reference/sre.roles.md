# SRE Agent roles

What the agent identity can do, checked live with:

```powershell
az role assignment list --assignee <principalId of id-sre-<prefix>-agent> --all -o table
```

The agent has two identities:

- **System-assigned**: created by the platform, gets no roles.
- **User-assigned** `id-sre-<prefix>-agent`: the identity the agent acts with
  (`actionConfiguration.identity` on the `Microsoft.App/agents` resource). All roles below go to it.

| Role | Scope | Assigned by | What the agent can do with it |
|---|---|---|---|
| Reader | `app.resourceGroup`, `external.resourceGroup` | `labkit/sre.py` `READ_ROLES` | ARM `GET` on every resource: site config, plan, metric definitions |
| Monitoring Reader | both RGs | `READ_ROLES` | Read metrics, alerts, diagnostic settings and the activity log |
| Log Analytics Reader | both RGs | `READ_ROLES` | KQL queries on the workspaces and Application Insights in those RGs |
| Monitoring Data Reader | both RGs | `READ_ROLES` | PromQL on the Azure Monitor workspace (the external resource) |
| Website Contributor | `app.resourceGroup` only | `lab.yaml` `sre.remediationRoles`, only when `sre.accessLevel: High` | `Microsoft.Web/sites/*` writes: restart, app settings, `PUT sites/config` (the fix) |
| Monitoring Contributor | **whole subscription** | the SRE Agent service, with your token, when Azure Monitor becomes the incident platform (not lab code) | Read and acknowledge or close **any** alert in the subscription (`Microsoft.AlertsManagement/alerts/changestate/action`) |

You (the deployer) get **SRE Agent Administrator** on the agent resource: chat, settings, and approval of actions.

## Things to know

- Monitoring Contributor is the widest role the agent holds. It reaches past the mapped resource groups
  to every alert in the subscription. Its `createdBy` is your account, about 30 minutes after the read roles.
- The agent has no write access to `external.resourceGroup` or the Foundry resource group, and no rights to change RBAC.
- With `sre.accessLevel: Low`, Website Contributor is not assigned, so the agent can investigate but not fix.
