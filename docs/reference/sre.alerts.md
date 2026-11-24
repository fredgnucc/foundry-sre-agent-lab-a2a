# SRE Agent alerts

Which alert rules exist, who creates them, and how a fired alert becomes an SRE Agent incident.
Checked live with:

```powershell
az resource list --resource-type Microsoft.Insights/scheduledQueryRules -o table        # same for metricAlerts,
az resource list --resource-type Microsoft.AlertsManagement/smartDetectorAlertRules -o table   # prometheusRuleGroups
```

## Alert rules

| Rule | Type | Signal | Fires when | Sev | Created by |
|---|---|---|---|---|---|
| `alert-<prefix>-http-5xx` | Log search alert (`scheduledQueryRules`) | `AppServiceHTTPLogs \| where ScStatus >= 500` in the app's workspace | more than `http5xxThreshold` (10) rows in 5 min, evaluated every 1 min | 1 | lab: `labkit/webapp.py` `_alerts()` |
| `alert-<prefix>-latency` | Metric alert (`metricAlerts`) | `HttpResponseTime` (Average) on the site | above `responseTimeSeconds` (3 s) over 5 min | 2 | lab: `labkit/webapp.py` `_alerts()` |
| `prg-<prefix>-vendor` / `alert-<prefix>-vendor-latency`, `-errors`, `-down` | Prometheus rule group (`prometheusRuleGroups`) | `vendor_api_*` and `up` in the Azure Monitor workspace | breach lasts `alertFor` (2 min) | 2 | lab: `labkit/external.py` (optional part) |
| `Failure Anomalies - appi-<prefix>-app-…` and `appi-<prefix>-sre-…` | Smart detection (`smartDetectorAlertRules`) | App Insights failure rate | anomalous failure rate | 3 | **Azure**, automatically when App Insights is created |

- The thresholds and severities are in `lab.yaml` (`app:` and `external:`).
- `autoMitigate` / `autoResolved` is on: a rule resolves its alert when the signal goes back to normal.
- The 5xx alert is a log alert on purpose: a metric alert on `Http5xx` didn't fire reliably in an
  earlier lab, and the log rows give the agent the failing requests as evidence.
- On a fresh workspace, `AppServiceHTTPLogs` only exists a few minutes after the diagnostic setting;
  until then the `PUT scheduledQueryRules` returns 400 and `up app` retries.

## No action group

None of the lab rules has an action group. The SRE Agent doesn't need one: it polls Azure Monitor
alerts for the subscriptions of the resource groups mapped to it
(`incidentManagementConfiguration.type: AzMonitor`).

## Which alerts the agent handles: response plan `webapp-alerts`

Polling sees every alert of the subscription. The response plan (`labkit/sre.py` `_response_plan()`,
`PUT {agent endpoint}/api/v1/incidentPlayground/filters/webapp-alerts`) picks the ones to handle:

| Field | Value | Effect |
|---|---|---|
| `priorities` | `Sev0`, `Sev1`, `Sev2` | Failure Anomalies (Sev3) is ignored |
| `titleContainsAny` | `alert-<prefix>-` | only the lab's rules, not the other alerts of the subscription |
| `handlingAgent` | `incident-handler` after `up mcp` (default agent before) | who runs the investigation |
| `agentMode` | `autonomous` | acts without asking (here: resets `FAULT_MODE`) |
| `maxAutomatedInvestigationAttempts` | 3 | |
| `mergeEnabled` | `false` | each fired alert gets its own thread (lab); `true` merges repeats within 3 h |

## From fault to fix (measured 24 Nov 2026)

`python labtest.py fault --mode errors`:

| UTC | Event | Where to see it |
|---|---|---|
| 11:22:11 | `PUT sites/<app>/config/appsettings` sets `FAULT_MODE=errors` (caller: you) | Activity Log |
| 11:22-11:28 | GET `/` and `/api/orders`: 294 × 500, 209 × 200 (after the fix) | `fault` output |
| 11:24:20 | `alert-<prefix>-http-5xx` (Sev1) fires | Azure Monitor > Alerts |
| 11:25:10 | `incident-handler` acknowledges it | alert state `Acknowledged` |
| 11:25:14 | 4 × `POST` to the `-sre-mcp` API from 10.41.0.5, then `POST /ask` to the architect | `ApiManagementGatewayLogs` |
| 11:26:10 | `config/write` resets `FAULT_MODE=none`, caller `id-sre-<prefix>-agent` | Activity Log |

About 4 minutes from fault to fix, 2 of them waiting for the alert.

## Things to know

- An alert whose rule is deleted before it resolves stays "Fired" in the alert list (seen with
  `prg-<prefix>-vendor` after the external part was removed). It isn't a live issue.
- A customer who renames rules, changes severities or adds alert processing rules changes what the
  response plan matches: check `titleContainsAny` and `priorities` against their names.
