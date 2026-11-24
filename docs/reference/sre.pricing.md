# SRE Agent pricing

Billed in **Azure Agent Units (AAU)**. Sources:
[pricing and billing](https://learn.microsoft.com/azure/sre-agent/pricing-billing),
[price page](https://azure.microsoft.com/pricing/details/sre-agent/). Checked November 2026; prices change.

The bill has two parts:

| Part | What it is | Rate |
|---|---|---|
| Always-on flow (fixed) | The agent exists, from creation until **delete** | 4 AAU per agent-hour |
| Active flow (variable) | LLM tokens used while the agent works: chat, incidents, scheduled tasks | AAU per 1M tokens, by model (below) |

## Price per AAU (pay as you go, USD)

From the Azure Retail Prices API (meter `SRE Agent Unit`):

```powershell
(Invoke-RestMethod "https://prices.azure.com/api/retail/prices?`$filter=meterName eq 'SRE Agent Unit' and type eq 'Consumption'").Items |
  select armRegionName, retailPrice | sort armRegionName
```

| Region | $ / AAU | Always-on per agent (4 AAU × 730 h) |
|---|---|---|
| eastus, eastus2 | 0.10 | ~$292 / month |
| swedencentral (this lab), francecentral, uksouth, australiaeast | 0.11 | ~$321 / month |
| switzerlandnorth, germanywestcentral | 0.12 | ~$350 / month |
| westeurope, northeurope | 0.14 | ~$409 / month |

## Active flow: AAU per 1M tokens

| Model | Input | Output | Cache read | Cache write |
|---|---|---|---|---|
| Claude Opus 5.5 | 80 | 400 | 4 | 100 |
| Claude Opus 4.6 and 5 | 100 | 500 | 10 | 125 |
| GPT-6 Sol, GPT-5.6 Terra | 40 | 200 | 4 | 50 |
| GPT 5.3 Codex, GPT 5.2 | 35 | 280 | 3.5 | 0 |

The model comes from the agent's `defaultModel` (this lab: `modelProvider: Anthropic` in `lab.yaml`).

## What a task costs (Microsoft's examples, swedencentral price)

| Task | Opus 4.6 | GPT 5.3 Codex |
|---|---|---|
| Quick question ("show me recent alerts") | ~3.8 AAU ≈ $0.42 | ~1.3 AAU ≈ $0.14 |
| Incident investigation (Azure Monitor alert) | ~35 AAU ≈ $3.90 | ~12 AAU ≈ $1.30 |
| Full remediation (diagnose and fix) | ~87 AAU ≈ $9.50 | ~30 AAU ≈ $3.30 |

The fault demo (`labtest.py fault --mode errors`) is an investigation plus a remediation: the
`incident-handler` acknowledges the alert, asks the architect over MCP and resets `FAULT_MODE`.
The real figure for each thread is in **sre.azure.com > Build + setup > Monitor > Agent consumption**.

## Controlling the bill

| Action | Active flow | Always-on |
|---|---|---|
| Monthly AAU limit (min 500, max 1,000,000) reached | Stops until next month | Still billed |
| Stop the agent (Settings > General) | Stops | **Still billed** |
| Delete the agent (`python lab.py down --yes`) | Stops | Stops |

- The always-on part dominates a lab: one idle agent in swedencentral costs about $10.60 a day.
  Delete the agent when you're not testing; stopping it doesn't save the fixed part.
- One agent can watch many resource groups. Map more groups to one agent instead of creating
  more agents.
- Filter alerts in the response plan (this lab matches only titles that contain `alert-<prefix>-`), so
  the agent doesn't investigate everything.
- New customers can [evaluate SRE Agent](https://learn.microsoft.com/azure/sre-agent/evaluate)
  without the always-on charge.
